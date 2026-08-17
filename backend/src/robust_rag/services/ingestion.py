"""Transactional document upload and ingestion job creation."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from robust_rag.core.errors import AppError
from robust_rag.db.enums import (
    DocumentStatus,
    JobStatus,
    JobType,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import Document, DocumentVersion, IngestionJob, StageRun
from robust_rag.services.dispatcher import JobDispatcher
from robust_rag.storage.base import FileStorage

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class UploadResult:
    document: Document
    version: DocumentVersion
    job: IngestionJob
    warnings: list[str] = field(default_factory=list)


async def create_document_upload(
    *,
    db: Session,
    storage: FileStorage,
    dispatcher: JobDispatcher,
    upload: UploadFile,
    document_id: uuid.UUID | None,
    display_name: str | None,
    allow_duplicate_content: bool,
) -> UploadResult:
    """Persist one immutable source version and its durable ingestion job."""

    prepared = await storage.prepare(upload)
    storage_uri: str | None = None
    warnings: list[str] = []

    try:
        document = _resolve_document(db, document_id, display_name, prepared.safe_filename)
        _reject_same_document_duplicate(db, document, prepared.sha256)
        duplicate_documents = _find_duplicate_content(db, prepared.sha256, document.id)
        if duplicate_documents and not allow_duplicate_content:
            raise AppError(
                code="DUPLICATE_CONTENT",
                message="The same content already exists in another document",
                status_code=409,
                details={
                    "document_ids": [str(existing_id) for existing_id in duplicate_documents],
                    "confirmation_field": "allow_duplicate_content",
                },
            )
        if duplicate_documents:
            warnings.append("duplicate_content_allowed")

        version_number = db.scalar(
            select(func.coalesce(func.max(DocumentVersion.version_number), 0)).where(
                DocumentVersion.document_id == document.id
            )
        )
        version_id = uuid.uuid4()
        storage_uri = storage.commit(prepared, document.id, version_id)
        document.updated_at = datetime.now(UTC)
        version = DocumentVersion(
            id=version_id,
            document_id=document.id,
            version_number=int(version_number or 0) + 1,
            original_filename=prepared.safe_filename,
            mime_type=prepared.mime_type,
            file_size=prepared.file_size,
            sha256=prepared.sha256,
            storage_uri=storage_uri,
            status=VersionStatus.UPLOADED,
        )
        job = IngestionJob(
            document_version=version,
            job_type=JobType.INGESTION,
            status=JobStatus.PENDING,
            current_stage=StageName.PARSING,
            progress_current=1,
            progress_total=8,
        )
        upload_run = StageRun(
            job=job,
            stage_name=StageName.UPLOAD,
            implementation_name="LocalFileStorage",
            implementation_version="1.0.0",
            config_version="stage1-v1",
            config_snapshot={
                "mime_type": prepared.mime_type,
                "file_size": prepared.file_size,
                "sha256": prepared.sha256,
            },
            status=StageRunStatus.SUCCEEDED,
            attempt=1,
            output_artifact_uri=storage_uri,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        db.add_all([document, version, job, upload_run])
        db.commit()
    except AppError:
        db.rollback()
        if storage_uri is not None:
            storage.delete(storage_uri)
        else:
            storage.discard(prepared)
        raise
    except IntegrityError as exc:
        db.rollback()
        if storage_uri is not None:
            storage.delete(storage_uri)
        else:
            storage.discard(prepared)
        raise AppError(
            code="UPLOAD_CONFLICT",
            message="A concurrent upload created the same document version",
            status_code=409,
        ) from exc
    except SQLAlchemyError:
        db.rollback()
        if storage_uri is not None:
            storage.delete(storage_uri)
        else:
            storage.discard(prepared)
        raise
    except Exception:
        db.rollback()
        if storage_uri is not None:
            storage.delete(storage_uri)
        else:
            storage.discard(prepared)
        raise

    try:
        job.celery_task_id = dispatcher.dispatch(job.id)
        db.add(job)
        db.commit()
    except Exception as exc:
        db.rollback()
        warnings.append("job_dispatch_deferred")
        logger.warning("job_dispatch_deferred", job_id=str(job.id), error=str(exc))

    return UploadResult(document=document, version=version, job=job, warnings=warnings)


def retry_failed_job(db: Session, dispatcher: JobDispatcher, job_id: uuid.UUID) -> IngestionJob:
    job = db.get(IngestionJob, job_id)
    if job is None:
        raise AppError(code="JOB_NOT_FOUND", message="Job was not found", status_code=404)
    if job.status is not JobStatus.FAILED:
        raise AppError(
            code="JOB_NOT_RETRYABLE",
            message="Only failed jobs can be retried",
            status_code=409,
            details={"status": job.status.value},
        )

    job.status = JobStatus.PENDING
    job.attempt += 1
    job.error_code = None
    job.error_message = None
    job.finished_at = None
    db.commit()
    try:
        job.celery_task_id = dispatcher.dispatch(job.id)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("job_retry_dispatch_deferred", job_id=str(job.id), error=str(exc))
    return job


def create_reprocess_job(
    db: Session, dispatcher: JobDispatcher, document_id: uuid.UUID
) -> IngestionJob:
    document = db.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if document is None or document.status is DocumentStatus.DELETED:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="Document was not found", status_code=404)
    if document.current_version_id is None:
        raise AppError(
            code="DOCUMENT_VERSION_NOT_READY",
            message="The document has no current version to reprocess",
            status_code=409,
        )
    active_job = db.scalar(
        select(IngestionJob)
        .where(
            IngestionJob.document_version_id == document.current_version_id,
            IngestionJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
        )
        .order_by(IngestionJob.created_at.desc())
        .limit(1)
    )
    if active_job is not None:
        raise AppError(
            code="DOCUMENT_REPROCESS_IN_PROGRESS",
            message="This document already has an active processing job",
            status_code=409,
            details={"job_id": str(active_job.id)},
        )

    job = IngestionJob(
        document_version_id=document.current_version_id,
        job_type=JobType.REPROCESS,
        status=JobStatus.PENDING,
        current_stage=StageName.PARSING,
        progress_current=1,
        progress_total=8,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        job.celery_task_id = dispatcher.dispatch(job.id)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("job_reprocess_dispatch_deferred", job_id=str(job.id), error=str(exc))
    return job


def _resolve_document(
    db: Session,
    document_id: uuid.UUID | None,
    display_name: str | None,
    safe_filename: str,
) -> Document:
    if document_id is not None:
        document = db.scalar(select(Document).where(Document.id == document_id).with_for_update())
        if document is None or document.status is DocumentStatus.DELETED:
            raise AppError(
                code="DOCUMENT_NOT_FOUND", message="Document was not found", status_code=404
            )
        return document

    resolved_name = (display_name or safe_filename).strip()
    if not resolved_name:
        raise AppError(code="INVALID_DISPLAY_NAME", message="Display name cannot be empty")
    existing = db.scalar(
        select(Document)
        .where(
            func.lower(Document.display_name) == resolved_name.lower(),
            Document.status == DocumentStatus.ACTIVE,
        )
        .order_by(Document.created_at)
        .limit(1)
        .with_for_update()
    )
    if existing is not None:
        return existing

    document = Document(display_name=resolved_name, status=DocumentStatus.ACTIVE)
    db.add(document)
    db.flush()
    return document


def _reject_same_document_duplicate(db: Session, document: Document, sha256: str) -> None:
    duplicate = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.sha256 == sha256,
        )
    )
    if duplicate is not None:
        raise AppError(
            code="DUPLICATE_VERSION",
            message="This document already has a version with identical content",
            status_code=409,
            details={
                "document_id": str(document.id),
                "document_version_id": str(duplicate.id),
            },
        )


def _find_duplicate_content(
    db: Session, sha256: str, current_document_id: uuid.UUID
) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(DocumentVersion.document_id)
            .where(
                DocumentVersion.sha256 == sha256,
                DocumentVersion.document_id != current_document_id,
            )
            .distinct()
        )
    )
