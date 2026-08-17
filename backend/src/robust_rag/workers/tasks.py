"""Durable ingestion orchestration tasks."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from sqlalchemy import select

from robust_rag.core.settings import get_settings
from robust_rag.db.enums import JobStatus, StageName, StageRunStatus
from robust_rag.db.models import IngestionJob, StageRun
from robust_rag.db.session import SessionLocal
from robust_rag.workers.celery_app import celery_app


class AdvanceResult(TypedDict):
    job_id: str
    status: str
    current_stage: str


@celery_app.task(name="ingestion.advance")  # type: ignore[untyped-decorator]
def advance_ingestion(job_id: str) -> AdvanceResult:
    """Advance a job idempotently as far as implemented stages allow."""

    parsed_job_id = uuid.UUID(job_id)
    with SessionLocal.begin() as db:
        job = db.scalar(
            select(IngestionJob).where(IngestionJob.id == parsed_job_id).with_for_update()
        )
        if job is None:
            return {"job_id": job_id, "status": "not_found", "current_stage": "unknown"}
        if job.status in {JobStatus.SUCCEEDED, JobStatus.CANCELLED, JobStatus.QUARANTINED}:
            return {
                "job_id": job_id,
                "status": job.status.value,
                "current_stage": job.current_stage.value,
            }

        if job.current_stage is StageName.UPLOAD:
            _complete_upload_stage(job)

        # Parsing is intentionally introduced in stage 2. Keeping this durable PENDING
        # state makes deployment/restart recovery explicit rather than reporting false success.
        job.status = JobStatus.PENDING
        job.updated_at = datetime.now(UTC)
        return {
            "job_id": job_id,
            "status": "deferred",
            "current_stage": job.current_stage.value,
        }


@celery_app.task(name="ingestion.recover_pending")  # type: ignore[untyped-decorator]
def recover_pending_jobs() -> dict[str, int]:
    """Requeue stale non-terminal jobs from PostgreSQL after worker or broker loss."""

    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.job_recovery_age_seconds)
    with SessionLocal.begin() as db:
        jobs = list(
            db.scalars(
                select(IngestionJob).where(
                    IngestionJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                    IngestionJob.updated_at <= cutoff,
                )
            )
        )
        for job in jobs:
            result = advance_ingestion.delay(str(job.id))
            job.celery_task_id = str(result.id)
            job.status = JobStatus.PENDING
            job.updated_at = datetime.now(UTC)
    return {"requeued": len(jobs)}


def _complete_upload_stage(job: IngestionJob) -> None:
    existing = next(
        (run for run in job.stage_runs if run.stage_name is StageName.UPLOAD),
        None,
    )
    if existing is None:
        job.stage_runs.append(
            StageRun(
                stage_name=StageName.UPLOAD,
                implementation_name="LocalFileStorage",
                implementation_version="1.0.0",
                config_version="stage1-v1",
                config_snapshot={},
                status=StageRunStatus.SUCCEEDED,
                attempt=1,
                output_artifact_uri=job.document_version.storage_uri,
                finished_at=datetime.now(UTC),
            )
        )
    job.current_stage = StageName.PARSING
    job.progress_current = max(job.progress_current, 1)
