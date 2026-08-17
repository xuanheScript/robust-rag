"""Controlled OpenSearch capability, rebuild, deletion, and alias APIs."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from robust_rag.core.errors import AppError
from robust_rag.db.enums import DocumentStatus
from robust_rag.db.models import (
    CanonicalDocumentRecord,
    ChunkingRun,
    CleaningRun,
    Document,
    DocumentVersion,
    GraphExtractionRun,
    ParseRun,
    QualityAssessment,
)
from robust_rag.db.session import SessionLocal, get_db
from robust_rag.indexing.opensearch import OpenSearchAdapterError
from robust_rag.indexing.schemas import (
    AliasSwitchRequest,
    AliasSwitchResponse,
    DocumentDeletionResponse,
    DocumentPurgeRequest,
    DocumentPurgeResponse,
    DocumentRestoreResponse,
    ProjectionMutationResponse,
    ProjectionRebuildRequest,
    SearchCapabilitiesRead,
)
from robust_rag.indexing.service import IndexingService, get_indexing_service
from robust_rag.storage.base import FileStorage
from robust_rag.storage.local import get_file_storage

router = APIRouter(tags=["search-projection"])


def get_search_admin_service() -> IndexingService:
    return get_indexing_service(SessionLocal)


SearchAdminService = Annotated[IndexingService, Depends(get_search_admin_service)]
DatabaseSession = Annotated[Session, Depends(get_db)]
StorageDependency = Annotated[FileStorage, Depends(get_file_storage)]


@router.get("/system/search-capabilities", response_model=SearchCapabilitiesRead)
def search_capabilities(service: SearchAdminService) -> SearchCapabilitiesRead:
    try:
        return SearchCapabilitiesRead.model_validate(service.adapter.capabilities().snapshot())
    except OpenSearchAdapterError as exc:
        raise _app_error(exc) from exc


@router.post("/system/search-indexes/rebuild", response_model=ProjectionMutationResponse)
def rebuild_search_indexes(
    request: ProjectionRebuildRequest, service: SearchAdminService
) -> ProjectionMutationResponse:
    try:
        return ProjectionMutationResponse.model_validate(service.rebuild_ready(request.document_id))
    except OpenSearchAdapterError as exc:
        raise _app_error(exc) from exc


@router.post("/system/search-indexes/switch", response_model=AliasSwitchResponse)
def switch_search_aliases(
    request: AliasSwitchRequest, service: SearchAdminService
) -> AliasSwitchResponse:
    try:
        service.switch_aliases(request.documents_index, request.chunks_index)
    except OpenSearchAdapterError as exc:
        raise _app_error(exc) from exc
    return AliasSwitchResponse(
        documents_index=request.documents_index, chunks_index=request.chunks_index
    )


@router.post(
    "/documents/{document_id}/search-projection/rebuild",
    response_model=ProjectionMutationResponse,
)
def rebuild_document_projection(
    document_id: uuid.UUID, service: SearchAdminService
) -> ProjectionMutationResponse:
    try:
        return ProjectionMutationResponse.model_validate(service.rebuild_ready(document_id))
    except OpenSearchAdapterError as exc:
        raise _app_error(exc) from exc


@router.delete(
    "/documents/{document_id}/search-projection",
    response_model=ProjectionMutationResponse,
)
def delete_document_projection(
    document_id: uuid.UUID, service: SearchAdminService
) -> ProjectionMutationResponse:
    try:
        return ProjectionMutationResponse.model_validate(
            service.delete_document_projection(document_id)
        )
    except OpenSearchAdapterError as exc:
        raise _app_error(exc) from exc


@router.delete("/documents/{document_id}", response_model=DocumentDeletionResponse)
def delete_document(
    document_id: uuid.UUID, service: SearchAdminService
) -> DocumentDeletionResponse:
    try:
        return DocumentDeletionResponse.model_validate(service.delete_document(document_id))
    except OpenSearchAdapterError as exc:
        raise _app_error(exc) from exc


@router.post("/documents/{document_id}/restore", response_model=DocumentRestoreResponse)
def restore_document(
    document_id: uuid.UUID, service: SearchAdminService
) -> DocumentRestoreResponse:
    try:
        return DocumentRestoreResponse.model_validate(service.restore_document(document_id))
    except OpenSearchAdapterError as exc:
        raise _app_error(exc) from exc


@router.delete("/documents/{document_id}/purge", response_model=DocumentPurgeResponse)
def purge_document(
    document_id: uuid.UUID,
    request: DocumentPurgeRequest,
    db: DatabaseSession,
    storage: StorageDependency,
    service: SearchAdminService,
) -> DocumentPurgeResponse:
    document = db.get(Document, document_id)
    if document is None:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="Document was not found", status_code=404)
    if document.status is not DocumentStatus.DELETED:
        raise AppError(
            code="DOCUMENT_NOT_DELETED",
            message="A document must be soft deleted before it can be permanently deleted",
            status_code=409,
        )
    if request.confirmation != document.display_name:
        raise AppError(
            code="PURGE_CONFIRMATION_MISMATCH",
            message="Permanent deletion confirmation does not match the document name",
            status_code=409,
        )
    artifact_uris = _artifact_uris(db, document_id)
    try:
        result = service.purge_document(document_id)
    except OpenSearchAdapterError as exc:
        raise _app_error(exc) from exc
    db.delete(document)
    db.commit()
    artifact_errors: list[str] = []
    artifacts_deleted = 0
    for uri in artifact_uris:
        try:
            storage.delete(uri)
            artifacts_deleted += 1
        except Exception as exc:
            artifact_errors.append(f"{uri}: {type(exc).__name__}")
    return DocumentPurgeResponse(
        document_id=document_id,
        status="purged",
        artifacts_deleted=artifacts_deleted,
        artifact_errors=artifact_errors,
        **result,
    )


def _artifact_uris(db: Session, document_id: uuid.UUID) -> list[str]:
    version_ids = list(
        db.scalars(select(DocumentVersion.id).where(DocumentVersion.document_id == document_id))
    )
    if not version_ids:
        return []
    values: list[str | None] = list(
        db.scalars(select(DocumentVersion.storage_uri).where(DocumentVersion.id.in_(version_ids)))
    )
    for model, columns in (
        (ParseRun, (ParseRun.artifact_uri,)),
        (CanonicalDocumentRecord, (CanonicalDocumentRecord.artifact_uri,)),
        (CleaningRun, (CleaningRun.output_artifact_uri, CleaningRun.report_artifact_uri)),
        (QualityAssessment, (QualityAssessment.raw_result_uri,)),
        (ChunkingRun, (ChunkingRun.artifact_uri, ChunkingRun.report_artifact_uri)),
        (GraphExtractionRun, (GraphExtractionRun.artifact_uri,)),
    ):
        for column in columns:
            values.extend(
                db.scalars(select(column).where(model.document_version_id.in_(version_ids)))
            )
    return list(dict.fromkeys(value for value in values if value))


def _app_error(error: OpenSearchAdapterError) -> AppError:
    return AppError(
        code=error.code,
        message=error.message,
        status_code=503 if error.retryable else 409,
        details={"retryable": error.retryable, "status_code": error.status_code},
    )
