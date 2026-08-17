"""Durable ingestion orchestration tasks."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from sqlalchemy import select

from robust_rag.chunking.service import get_chunking_service
from robust_rag.cleaning.service import get_cleaning_service
from robust_rag.core.settings import get_settings
from robust_rag.db.enums import JobStatus, StageName, StageRunStatus
from robust_rag.db.models import IngestionJob, StageRun
from robust_rag.db.session import SessionLocal
from robust_rag.graph.factory import get_graph_extraction_service, graph_is_configured
from robust_rag.indexing.embedding_service import get_embedding_service
from robust_rag.indexing.gate import RetrievalNodeGateService
from robust_rag.indexing.service import get_indexing_service
from robust_rag.parsing.service import get_parsing_service
from robust_rag.quality.service import get_quality_service
from robust_rag.workers.celery_app import celery_app


class AdvanceResult(TypedDict):
    job_id: str
    status: str
    current_stage: str


@celery_app.task(name="graph.extract")  # type: ignore[untyped-decorator]
def extract_graph(document_version_id: str) -> dict[str, str]:
    """Build the optional graph projection without changing ingestion readiness."""

    status = get_graph_extraction_service().execute(uuid.UUID(document_version_id))
    return {"document_version_id": document_version_id, "status": status}


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

        current_stage = job.current_stage
        if current_stage is not StageName.PARSING:
            job.status = JobStatus.PENDING
            job.updated_at = datetime.now(UTC)

    if current_stage is StageName.PARSING:
        parsing_status = get_parsing_service(SessionLocal).execute(parsed_job_id)
        with SessionLocal() as db:
            updated_job = db.get(IngestionJob, parsed_job_id)
            updated_stage = updated_job.current_stage.value if updated_job else "unknown"
        if parsing_status == "deferred" and updated_stage == StageName.CLEANING.value:
            return _execute_cleaning_then_quality(job_id, parsed_job_id)
        return {"job_id": job_id, "status": parsing_status, "current_stage": updated_stage}
    if current_stage is StageName.CLEANING:
        return _execute_cleaning_then_quality(job_id, parsed_job_id)
    if current_stage is StageName.DOCUMENT_EVALUATING:
        return _execute_quality_then_chunking(job_id, parsed_job_id)
    if current_stage is StageName.CHUNKING:
        return _execute_chunking(job_id, parsed_job_id)
    if current_stage is StageName.CHUNK_EVALUATING:
        return _execute_node_gate_then_embedding(job_id, parsed_job_id)
    if current_stage is StageName.EMBEDDING:
        return _execute_embedding_then_indexing(job_id, parsed_job_id)
    if current_stage is StageName.INDEXING:
        return _execute_indexing(job_id, parsed_job_id)
    return {"job_id": job_id, "status": "deferred", "current_stage": current_stage.value}


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


def _execute_cleaning_then_quality(job_id: str, parsed_job_id: uuid.UUID) -> AdvanceResult:
    cleaning_status = get_cleaning_service(SessionLocal).execute(parsed_job_id)
    with SessionLocal() as db:
        updated_job = db.get(IngestionJob, parsed_job_id)
        updated_stage = updated_job.current_stage.value if updated_job else "unknown"
    if cleaning_status == "deferred" and updated_stage == StageName.DOCUMENT_EVALUATING.value:
        return _execute_quality_then_chunking(job_id, parsed_job_id)
    return {"job_id": job_id, "status": cleaning_status, "current_stage": updated_stage}


def _execute_quality_then_chunking(job_id: str, parsed_job_id: uuid.UUID) -> AdvanceResult:
    quality_status = get_quality_service(SessionLocal).execute(parsed_job_id)
    with SessionLocal() as db:
        updated_job = db.get(IngestionJob, parsed_job_id)
        updated_stage = updated_job.current_stage.value if updated_job else "unknown"
    if quality_status == "deferred" and updated_stage == StageName.CHUNKING.value:
        return _execute_chunking(job_id, parsed_job_id)
    return {"job_id": job_id, "status": quality_status, "current_stage": updated_stage}


def _execute_chunking(job_id: str, parsed_job_id: uuid.UUID) -> AdvanceResult:
    chunking_status = get_chunking_service(SessionLocal).execute(parsed_job_id)
    with SessionLocal() as db:
        updated_job = db.get(IngestionJob, parsed_job_id)
        updated_stage = updated_job.current_stage.value if updated_job else "unknown"
    if chunking_status == "deferred" and updated_stage == StageName.CHUNK_EVALUATING.value:
        return _execute_node_gate_then_embedding(job_id, parsed_job_id)
    return {"job_id": job_id, "status": chunking_status, "current_stage": updated_stage}


def _execute_node_gate_then_embedding(job_id: str, parsed_job_id: uuid.UUID) -> AdvanceResult:
    gate_status = RetrievalNodeGateService(SessionLocal).execute(parsed_job_id)
    with SessionLocal() as db:
        updated_job = db.get(IngestionJob, parsed_job_id)
        updated_stage = updated_job.current_stage.value if updated_job else "unknown"
    if gate_status == "deferred" and updated_stage == StageName.EMBEDDING.value:
        return _execute_embedding_then_indexing(job_id, parsed_job_id)
    return {"job_id": job_id, "status": gate_status, "current_stage": updated_stage}


def _execute_embedding_then_indexing(job_id: str, parsed_job_id: uuid.UUID) -> AdvanceResult:
    embedding_status = get_embedding_service(SessionLocal).execute(parsed_job_id)
    with SessionLocal() as db:
        updated_job = db.get(IngestionJob, parsed_job_id)
        updated_stage = updated_job.current_stage.value if updated_job else "unknown"
    if embedding_status == "deferred" and updated_stage == StageName.INDEXING.value:
        return _execute_indexing(job_id, parsed_job_id)
    return {"job_id": job_id, "status": embedding_status, "current_stage": updated_stage}


def _execute_indexing(job_id: str, parsed_job_id: uuid.UUID) -> AdvanceResult:
    indexing_status = get_indexing_service(SessionLocal).execute(parsed_job_id)
    with SessionLocal() as db:
        updated_job = db.get(IngestionJob, parsed_job_id)
        updated_stage = updated_job.current_stage.value if updated_job else "unknown"
        version_id = updated_job.document_version_id if updated_job else None
    if (
        indexing_status == "succeeded"
        and version_id is not None
        and graph_is_configured(get_settings())
    ):
        extract_graph.delay(str(version_id))
    return {"job_id": job_id, "status": indexing_status, "current_stage": updated_stage}
