"""Document upload and metadata APIs."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from robust_rag.api.schemas.documents import (
    DocumentListResponse,
    DocumentRead,
    DocumentVersionRead,
    UploadResponse,
)
from robust_rag.core.errors import AppError
from robust_rag.db.enums import DocumentStatus
from robust_rag.db.models import Document, DocumentVersion
from robust_rag.db.session import get_db
from robust_rag.services.dispatcher import JobDispatcher, get_job_dispatcher
from robust_rag.services.ingestion import create_document_upload
from robust_rag.storage.base import FileStorage
from robust_rag.storage.local import get_file_storage

router = APIRouter(prefix="/documents", tags=["documents"])
DatabaseSession = Annotated[Session, Depends(get_db)]
StorageDependency = Annotated[FileStorage, Depends(get_file_storage)]
DispatcherDependency = Annotated[JobDispatcher, Depends(get_job_dispatcher)]


@router.post("/uploads", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    file: Annotated[UploadFile, File(...)],
    db: DatabaseSession,
    storage: StorageDependency,
    dispatcher: DispatcherDependency,
    document_id: Annotated[uuid.UUID | None, Form()] = None,
    display_name: Annotated[str | None, Form(max_length=500)] = None,
    allow_duplicate_content: Annotated[bool, Form()] = False,
) -> UploadResponse:
    result = await create_document_upload(
        db=db,
        storage=storage,
        dispatcher=dispatcher,
        upload=file,
        document_id=document_id,
        display_name=display_name,
        allow_duplicate_content=allow_duplicate_content,
    )
    return UploadResponse(
        document=DocumentRead.model_validate(result.document),
        version=DocumentVersionRead.model_validate(result.version),
        job=result.job,
        warnings=result.warnings,
    )


@router.get("", response_model=DocumentListResponse)
def list_documents(
    db: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_deleted: bool = False,
) -> DocumentListResponse:
    filters = [] if include_deleted else [Document.status != DocumentStatus.DELETED]
    total = db.scalar(select(func.count()).select_from(Document).where(*filters)) or 0
    documents = list(
        db.scalars(
            select(Document)
            .where(*filters)
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return DocumentListResponse(items=documents, total=total)


@router.get("/{document_id}", response_model=DocumentRead)
def get_document(document_id: uuid.UUID, db: DatabaseSession) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="Document was not found", status_code=404)
    return document


@router.get("/{document_id}/versions", response_model=list[DocumentVersionRead])
def list_document_versions(document_id: uuid.UUID, db: DatabaseSession) -> list[DocumentVersion]:
    if db.get(Document, document_id) is None:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="Document was not found", status_code=404)
    return list(
        db.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version_number.desc())
        )
    )
