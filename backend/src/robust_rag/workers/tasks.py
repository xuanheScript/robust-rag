"""Durable ingestion orchestration tasks."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from math import ceil
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
from robust_rag.db.enums import (
    DocumentStatus,
    GraphBuildRequestStatus,
    GraphProjectionStatus,
    GraphRunStatus,
    JobStatus,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import (
    Document,
    DocumentVersion,
    GraphBuildRequest,
    GraphExtractionRun,
    IngestionJob,
    StageRun,
)
from robust_rag.db.session import SessionLocal
from robust_rag.graph.factory import (
    get_graph_extraction_service,
    get_graph_lifecycle_service,
    graph_is_configured,
)
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
def extract_graph(graph_build_request_id: str) -> dict[str, str]:
    """Execute one explicitly authorized graph build request."""

    with _task_context(
        "graph.extract",
        trace_seed=f"graph-build:{graph_build_request_id}",
        metadata={"graph_build_request_id": graph_build_request_id},
    ) as observation:
        request_id = uuid.UUID(graph_build_request_id)
        prepared = _start_graph_build_request(request_id)
        if prepared is None:
            result = {"graph_build_request_id": graph_build_request_id, "status": "cancelled"}
            observation.update(output=result)
            return result
        version_id, force = prepared
        try:
            status = get_graph_extraction_service().execute(
                version_id,
                force=force,
                build_request_id=request_id,
            )
        except BaseException as exc:
            cleanup_status = _finish_graph_build_request(
                request_id, succeeded=False, error=exc
            )
            _cleanup_cancelled_graph_projection(version_id, cleanup_status)
            raise
        succeeded = status == "succeeded"
        cleanup_status = _finish_graph_build_request(request_id, succeeded=succeeded)
        _cleanup_cancelled_graph_projection(version_id, cleanup_status)
        result = {
            "graph_build_request_id": graph_build_request_id,
            "document_version_id": str(version_id),
            "status": "cancelled" if cleanup_status is not None else status,
        }
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


@celery_app.task(name="graph.recover_stale")  # type: ignore[untyped-decorator]
def recover_stale_graph_runs() -> dict[str, int]:
    """Retry interrupted authorized builds within their persisted attempt budget."""

    with _task_context("graph.recover_stale", trace_seed="graph:recovery") as observation:
        settings = get_settings()
        if not graph_is_configured(settings):
            result_payload = {"requeued": 0}
            observation.update(output=result_payload, metadata={"configured": False})
            return result_payload
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=settings.graph_run_stale_seconds)
        request_ids: set[uuid.UUID] = set()
        with SessionLocal.begin() as db:
            runs = list(
                db.scalars(
                    select(GraphExtractionRun)
                    .where(
                        GraphExtractionRun.status == GraphRunStatus.RUNNING,
                        GraphExtractionRun.started_at <= cutoff,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for run in runs:
                run.status = GraphRunStatus.FAILED
                run.finished_at = now
                run.error = {
                    "type": "GraphRunStaleError",
                    "code": "GRAPH_RUN_STALE",
                    "message": "Graph extraction was interrupted and automatically requeued",
                }
                version = db.get(DocumentVersion, run.document_version_id)
                request = (
                    db.get(GraphBuildRequest, run.build_request_id)
                    if run.build_request_id is not None
                    else None
                )
                if request is None:
                    if version is not None:
                        version.graph_status = GraphProjectionStatus.FAILED
                    continue
                if request.attempt >= request.max_attempts:
                    request.status = GraphBuildRequestStatus.FAILED
                    request.finished_at = now
                    request.error = run.error
                    if version is not None:
                        version.graph_status = GraphProjectionStatus.FAILED
                    continue
                request.attempt += 1
                request.status = GraphBuildRequestStatus.PENDING
                request.started_at = None
                request.error = None
                if version is not None:
                    version.graph_status = GraphProjectionStatus.PENDING
                request_ids.add(request.id)
        for request_id in request_ids:
            extract_graph.delay(str(request_id))
        result_payload = {"requeued": len(request_ids)}
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
    embedding_service = get_embedding_service(SessionLocal)
    embedding_status = embedding_service.execute(parsed_job_id)
    if embedding_status == "rate_limited":
        retry_after = ceil(
            embedding_service.retry_after_seconds
            or get_settings().voyage_embedding_rate_limit_fallback_seconds
        )
        scheduled = advance_ingestion.apply_async(args=[job_id], countdown=retry_after)
        with SessionLocal.begin() as db:
            waiting_job = db.get(IngestionJob, parsed_job_id)
            if waiting_job is not None:
                waiting_job.celery_task_id = str(scheduled.id)
                waiting_job.status = JobStatus.PENDING
                waiting_job.updated_at = datetime.now(UTC)
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
    return {"job_id": job_id, "status": indexing_status, "current_stage": updated_stage}


def _start_graph_build_request(
    request_id: uuid.UUID,
) -> tuple[uuid.UUID, bool] | None:
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        request = db.scalar(
            select(GraphBuildRequest)
            .where(GraphBuildRequest.id == request_id)
            .with_for_update()
        )
        if request is None or request.status is not GraphBuildRequestStatus.PENDING:
            return None
        version = db.get(DocumentVersion, request.document_version_id)
        document = db.get(Document, request.document_id)
        valid = (
            version is not None
            and document is not None
            and document.status is DocumentStatus.ACTIVE
            and document.current_version_id == request.document_version_id
            and version.status is VersionStatus.READY
        )
        if not valid:
            request.status = GraphBuildRequestStatus.CANCELLED
            request.finished_at = now
            request.error = {
                "code": "GRAPH_BUILD_TARGET_CHANGED",
                "message": "The selected document version is no longer the active ready version",
            }
            if version is not None and version.graph_status in {
                GraphProjectionStatus.PENDING,
                GraphProjectionStatus.RUNNING,
            }:
                if document is not None and document.status is DocumentStatus.DELETED:
                    version.graph_status = GraphProjectionStatus.HIDDEN
                    version.graph_active = False
                elif version.graph_active:
                    version.graph_status = GraphProjectionStatus.STALE
                else:
                    version.graph_status = GraphProjectionStatus.NOT_REQUESTED
            return None
        assert version is not None
        request.status = GraphBuildRequestStatus.RUNNING
        request.started_at = now
        request.error = None
        version.graph_status = GraphProjectionStatus.RUNNING
        return version.id, request.force


def _finish_graph_build_request(
    request_id: uuid.UUID,
    *,
    succeeded: bool,
    error: BaseException | None = None,
) -> GraphProjectionStatus | None:
    settings = get_settings()
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        request = db.get(GraphBuildRequest, request_id)
        if request is None:
            return None
        run = db.scalar(
            select(GraphExtractionRun)
            .where(GraphExtractionRun.build_request_id == request_id)
            .order_by(GraphExtractionRun.started_at.desc())
            .limit(1)
        )
        usage = run.usage_json if run is not None else {}
        request.actual_input_tokens = _optional_int(usage.get("input_tokens"))
        request.actual_output_tokens = _optional_int(usage.get("output_tokens"))
        request.actual_total_tokens = _optional_int(usage.get("total_tokens"))
        request.actual_cost_usd = _llm_cost(
            request.actual_input_tokens,
            request.actual_output_tokens,
            input_price=settings.llm_price_per_million_input_tokens,
            output_price=settings.llm_price_per_million_output_tokens,
        )
        request.finished_at = now
        version = db.get(DocumentVersion, request.document_version_id)
        document = db.get(Document, request.document_id)
        target_is_valid = (
            request.status is GraphBuildRequestStatus.RUNNING
            and version is not None
            and document is not None
            and document.status is DocumentStatus.ACTIVE
            and document.current_version_id == request.document_version_id
            and version.status is VersionStatus.READY
        )
        if not target_is_valid:
            if request.status is not GraphBuildRequestStatus.CANCELLED:
                request.status = GraphBuildRequestStatus.CANCELLED
                request.error = {
                    "code": "GRAPH_BUILD_TARGET_CHANGED",
                    "message": (
                        "The selected document version changed while graph generation was running"
                    ),
                }
            if version is None:
                return None
            cleanup_status = (
                GraphProjectionStatus.HIDDEN
                if document is not None and document.status is DocumentStatus.DELETED
                else (
                    GraphProjectionStatus.STALE
                    if request.projection_was_active
                    or version.graph_active
                    or version.graph_projected_at is not None
                    else GraphProjectionStatus.NOT_REQUESTED
                )
            )
            version.graph_active = False
            version.graph_status = cleanup_status
            return cleanup_status
        if succeeded:
            request.status = GraphBuildRequestStatus.SUCCEEDED
            request.error = None
            if version is not None:
                version.graph_status = GraphProjectionStatus.SUCCEEDED
                version.graph_active = True
        else:
            request.status = GraphBuildRequestStatus.FAILED
            request.error = {
                "code": getattr(
                    error,
                    "code",
                    type(error).__name__ if error else "GRAPH_BUILD_FAILED",
                ),
                "type": type(error).__name__ if error else "GraphBuildError",
                "message": str(error) if error else "Graph extraction did not succeed",
            }
            if version is not None:
                version.graph_status = GraphProjectionStatus.FAILED
        return None


def _cleanup_cancelled_graph_projection(
    version_id: uuid.UUID,
    status: GraphProjectionStatus | None,
) -> None:
    if status is None:
        return
    lifecycle = get_graph_lifecycle_service()
    if lifecycle is not None:
        lifecycle.invalidate_version(version_id, status=status)


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _llm_cost(
    input_tokens: int | None,
    output_tokens: int | None,
    *,
    input_price: float | None,
    output_price: float | None,
) -> float | None:
    if input_price is None and output_price is None:
        return None
    return (
        (input_tokens or 0) * (input_price or 0)
        + (output_tokens or 0) * (output_price or 0)
    ) / 1_000_000
