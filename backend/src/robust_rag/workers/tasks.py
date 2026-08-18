"""Durable ingestion orchestration tasks."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import structlog
from celery import current_task
from sqlalchemy import select

from robust_rag.chunking.service import get_chunking_service
from robust_rag.cleaning.service import get_cleaning_service
from robust_rag.core.observability import (
    Observation,
    bind_trace_id,
    observe,
    reset_trace_id,
    trace_id_from_seed,
)
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

logger = structlog.get_logger(__name__)


class AdvanceResult(TypedDict):
    job_id: str
    status: str
    current_stage: str


@celery_app.task(name="graph.extract")  # type: ignore[untyped-decorator]
def extract_graph(document_version_id: str, *, force: bool = False) -> dict[str, str]:
    """Build the optional graph projection without changing ingestion readiness."""

    with _task_context(
        "graph.extract",
        trace_seed=f"graph:{document_version_id}",
        metadata={"document_version_id": document_version_id, "force": force},
    ) as observation:
        status = get_graph_extraction_service().execute(uuid.UUID(document_version_id), force=force)
        result = {"document_version_id": document_version_id, "status": status}
        observation.update(output=result)
        return result


@celery_app.task(name="ingestion.advance")  # type: ignore[untyped-decorator]
def advance_ingestion(job_id: str) -> AdvanceResult:
    """Advance a job idempotently as far as implemented stages allow."""

    with _task_context(
        "ingestion.advance",
        trace_seed=f"ingestion:{job_id}",
        metadata={"job_id": job_id},
    ) as observation:
        result = _advance_ingestion(job_id)
        observation.update(output=result, metadata={"stage": result["current_stage"]})
        return result


def _advance_ingestion(job_id: str) -> AdvanceResult:
    """Execute the durable ingestion transition inside a traced task boundary."""

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

    with _task_context("ingestion.recover_pending", trace_seed="ingestion:recovery") as observation:
        settings = get_settings()
        cutoff = datetime.now(UTC) - timedelta(seconds=settings.job_recovery_age_seconds)
        with SessionLocal.begin() as db:
            jobs = list(
                db.scalars(
                    select(IngestionJob)
                    .where(
                        IngestionJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                        IngestionJob.updated_at <= cutoff,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for job in jobs:
                result = advance_ingestion.delay(str(job.id))
                job.celery_task_id = str(result.id)
                job.status = JobStatus.PENDING
                job.updated_at = datetime.now(UTC)
        result_payload = {"requeued": len(jobs)}
        observation.update(output=result_payload, metadata={"cutoff": cutoff.isoformat()})
        return result_payload


@contextmanager
def _task_context(
    name: str,
    *,
    trace_seed: str,
    metadata: dict[str, object] | None = None,
) -> Iterator[Observation]:
    trace_id = trace_id_from_seed(trace_seed)
    task_id = str(getattr(getattr(current_task, "request", None), "id", "") or "direct")
    token = bind_trace_id(trace_id)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(trace_id=trace_id, task_id=task_id)
    started = datetime.now(UTC)
    logger.info("worker_task_started", task=name, **(metadata or {}))
    try:
        with observe(
            name,
            trace_id=trace_id,
            metadata={"task_id": task_id, **(metadata or {})},
        ) as obs:
            yield obs
        logger.info(
            "worker_task_completed",
            task=name,
            duration_ms=round((datetime.now(UTC) - started).total_seconds() * 1000, 3),
        )
    except BaseException as exc:
        logger.exception(
            "worker_task_failed",
            task=name,
            error_type=type(exc).__name__,
            duration_ms=round((datetime.now(UTC) - started).total_seconds() * 1000, 3),
        )
        raise
    finally:
        structlog.contextvars.clear_contextvars()
        reset_trace_id(token)


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
