"""Document upload and metadata APIs."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from robust_rag.api.schemas.documents import (
    CanonicalDocumentRecordRead,
    ChunkingRunRead,
    CleaningRunRead,
    DocumentListResponse,
    DocumentRead,
    DocumentVersionRead,
    JobRead,
    ParseRunRead,
    QualityAssessmentRead,
    QualityReviewActionRead,
    QualityReviewResponse,
    RetrievalNodeRead,
    UploadResponse,
)
from robust_rag.chunking.schemas import ChunkingArtifact, ChunkingReport
from robust_rag.cleaning.schemas import CleaningComparison, CleaningReport
from robust_rag.core.errors import AppError
from robust_rag.db.enums import CleaningRunStatus, DocumentStatus, RetrievalNodeLevel
from robust_rag.db.models import (
    CanonicalDocumentRecord,
    ChunkingRun,
    CleaningRun,
    Document,
    DocumentVersion,
    EmbeddingRun,
    IndexingRun,
    IngestionJob,
    ParseRun,
    QualityAssessment,
    QualityReviewAction,
    RetrievalNode,
)
from robust_rag.db.session import get_db
from robust_rag.indexing.schemas import EmbeddingRunRead, IndexingRunRead
from robust_rag.parsing.schemas import CanonicalDocument
from robust_rag.quality.review import (
    reevaluate_document_quality,
    reject_quarantined_document,
    release_quarantined_document,
)
from robust_rag.quality.schemas import QualityReport, QualityReviewRequest
from robust_rag.services.dispatcher import JobDispatcher, get_job_dispatcher
from robust_rag.services.ingestion import create_document_upload, create_reprocess_job
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
    q: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
    document_status: Annotated[DocumentStatus | None, Query(alias="status")] = None,
) -> DocumentListResponse:
    filters = [] if include_deleted else [Document.status != DocumentStatus.DELETED]
    if q is not None:
        filters.append(Document.display_name.ilike(f"%{q.strip()}%"))
    if document_status is not None:
        filters.append(Document.status == document_status)
    total = db.scalar(select(func.count()).select_from(Document).where(*filters)) or 0
    documents = list(
        db.scalars(
            select(Document)
            .options(selectinload(Document.current_version))
            .where(*filters)
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return DocumentListResponse(items=documents, total=total)


@router.post("/{document_id}/reprocess", response_model=JobRead, status_code=202)
def reprocess_document(
    document_id: uuid.UUID,
    db: DatabaseSession,
    dispatcher: DispatcherDependency,
) -> IngestionJob:
    return create_reprocess_job(db, dispatcher, document_id)


@router.get("/{document_id}", response_model=DocumentRead)
def get_document(document_id: uuid.UUID, db: DatabaseSession) -> Document:
    document = db.scalar(
        select(Document)
        .options(selectinload(Document.current_version))
        .where(Document.id == document_id)
    )
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


@router.get("/{document_id}/versions/{version_id}/parse-runs", response_model=list[ParseRunRead])
def list_parse_runs(
    document_id: uuid.UUID, version_id: uuid.UUID, db: DatabaseSession
) -> list[ParseRun]:
    _require_version(db, document_id, version_id)
    return list(
        db.scalars(
            select(ParseRun)
            .where(ParseRun.document_version_id == version_id)
            .order_by(ParseRun.started_at.desc())
        )
    )


@router.get(
    "/{document_id}/versions/{version_id}/canonical/metadata",
    response_model=CanonicalDocumentRecordRead,
)
def get_canonical_metadata(
    document_id: uuid.UUID, version_id: uuid.UUID, db: DatabaseSession
) -> CanonicalDocumentRecord:
    _require_version(db, document_id, version_id)
    record = db.scalar(
        select(CanonicalDocumentRecord)
        .where(CanonicalDocumentRecord.document_version_id == version_id)
        .order_by(CanonicalDocumentRecord.created_at.desc())
        .limit(1)
    )
    if record is None:
        raise AppError(
            code="CANONICAL_DOCUMENT_NOT_FOUND",
            message="Canonical document is not available",
            status_code=404,
        )
    return record


@router.get("/{document_id}/versions/{version_id}/canonical", response_model=CanonicalDocument)
def get_canonical_document(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    db: DatabaseSession,
    storage: StorageDependency,
) -> CanonicalDocument:
    record = get_canonical_metadata(document_id, version_id, db)
    return CanonicalDocument.model_validate(storage.read_json(record.artifact_uri))


@router.get(
    "/{document_id}/versions/{version_id}/cleaning-runs",
    response_model=list[CleaningRunRead],
)
def list_cleaning_runs(
    document_id: uuid.UUID, version_id: uuid.UUID, db: DatabaseSession
) -> list[CleaningRun]:
    _require_version(db, document_id, version_id)
    return list(
        db.scalars(
            select(CleaningRun)
            .where(CleaningRun.document_version_id == version_id)
            .order_by(CleaningRun.started_at.desc())
        )
    )


@router.get(
    "/{document_id}/versions/{version_id}/cleaning-runs/{run_id}/document",
    response_model=CanonicalDocument,
)
def get_cleaned_document(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    run_id: uuid.UUID,
    db: DatabaseSession,
    storage: StorageDependency,
) -> CanonicalDocument:
    run = _require_cleaning_run(db, document_id, version_id, run_id)
    if run.output_artifact_uri is None:
        raise AppError(
            code="CLEANED_DOCUMENT_NOT_AVAILABLE",
            message="The cleaning run has no completed document artifact",
            status_code=409,
        )
    return CanonicalDocument.model_validate(storage.read_json(run.output_artifact_uri))


@router.get(
    "/{document_id}/versions/{version_id}/cleaning-runs/{run_id}/report",
    response_model=CleaningReport,
)
def get_cleaning_report(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    run_id: uuid.UUID,
    db: DatabaseSession,
    storage: StorageDependency,
) -> CleaningReport:
    run = _require_cleaning_run(db, document_id, version_id, run_id)
    if run.report_artifact_uri is None:
        raise AppError(
            code="CLEANING_REPORT_NOT_AVAILABLE",
            message="The cleaning run has no completed report artifact",
            status_code=409,
        )
    return CleaningReport.model_validate(storage.read_json(run.report_artifact_uri))


@router.get(
    "/{document_id}/versions/{version_id}/cleaning-runs/{run_id}/compare",
    response_model=CleaningComparison,
)
def compare_cleaning_runs(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    run_id: uuid.UUID,
    against_run_id: uuid.UUID,
    db: DatabaseSession,
    storage: StorageDependency,
) -> CleaningComparison:
    base = _require_cleaning_run(db, document_id, version_id, run_id)
    compared = _require_cleaning_run(db, document_id, version_id, against_run_id)
    if (
        base.status is not CleaningRunStatus.SUCCEEDED
        or compared.status is not CleaningRunStatus.SUCCEEDED
        or base.output_artifact_uri is None
        or compared.output_artifact_uri is None
        or base.output_content_hash is None
        or compared.output_content_hash is None
    ):
        raise AppError(
            code="CLEANING_RUN_NOT_COMPARABLE",
            message="Both cleaning runs must have completed artifacts",
            status_code=409,
        )
    base_document = CanonicalDocument.model_validate(storage.read_json(base.output_artifact_uri))
    compared_document = CanonicalDocument.model_validate(
        storage.read_json(compared.output_artifact_uri)
    )
    base_blocks = {block.id: block for block in base_document.blocks}
    compared_blocks = {block.id: block for block in compared_document.blocks}
    shared = set(base_blocks) & set(compared_blocks)
    return CleaningComparison(
        base_run_id=str(base.id),
        compared_run_id=str(compared.id),
        same_output=base.output_content_hash == compared.output_content_hash,
        base_output_hash=base.output_content_hash,
        compared_output_hash=compared.output_content_hash,
        added_block_ids=sorted(set(compared_blocks) - set(base_blocks)),
        removed_block_ids=sorted(set(base_blocks) - set(compared_blocks)),
        normalized_text_changed_block_ids=sorted(
            block_id
            for block_id in shared
            if base_blocks[block_id].normalized_text != compared_blocks[block_id].normalized_text
        ),
        base_issue_count=base.issue_count or 0,
        compared_issue_count=compared.issue_count or 0,
    )


@router.get("/{document_id}/quality", response_model=list[QualityAssessmentRead])
def list_document_quality_assessments(
    document_id: uuid.UUID, db: DatabaseSession
) -> list[QualityAssessment]:
    if db.get(Document, document_id) is None:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="Document was not found", status_code=404)
    return list(
        db.scalars(
            select(QualityAssessment)
            .join(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(QualityAssessment.started_at.desc())
        )
    )


@router.get(
    "/{document_id}/versions/{version_id}/quality-assessments",
    response_model=list[QualityAssessmentRead],
)
def list_version_quality_assessments(
    document_id: uuid.UUID, version_id: uuid.UUID, db: DatabaseSession
) -> list[QualityAssessment]:
    _require_version(db, document_id, version_id)
    return list(
        db.scalars(
            select(QualityAssessment)
            .where(QualityAssessment.document_version_id == version_id)
            .order_by(QualityAssessment.started_at.desc())
        )
    )


@router.get(
    "/{document_id}/versions/{version_id}/quality-assessments/{assessment_id}/report",
    response_model=QualityReport,
)
def get_quality_report(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    assessment_id: uuid.UUID,
    db: DatabaseSession,
    storage: StorageDependency,
) -> QualityReport:
    assessment = _require_quality_assessment(db, document_id, version_id, assessment_id)
    if assessment.raw_result_uri is None:
        raise AppError(
            code="QUALITY_REPORT_NOT_AVAILABLE",
            message="The quality assessment has no completed report artifact",
            status_code=409,
        )
    return QualityReport.model_validate(storage.read_json(assessment.raw_result_uri))


@router.get(
    "/{document_id}/quality/review-actions",
    response_model=list[QualityReviewActionRead],
)
def list_quality_review_actions(
    document_id: uuid.UUID, db: DatabaseSession
) -> list[QualityReviewAction]:
    if db.get(Document, document_id) is None:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="Document was not found", status_code=404)
    return list(
        db.scalars(
            select(QualityReviewAction)
            .join(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(QualityReviewAction.created_at.desc())
        )
    )


@router.post("/{document_id}/release", response_model=QualityReviewResponse)
def release_document(
    document_id: uuid.UUID,
    request: QualityReviewRequest,
    db: DatabaseSession,
    dispatcher: DispatcherDependency,
) -> QualityReviewResponse:
    action, job = release_quarantined_document(
        db=db,
        dispatcher=dispatcher,
        document_id=document_id,
        actor=request.actor,
        reason=request.reason,
    )
    return QualityReviewResponse(
        action=QualityReviewActionRead.model_validate(action),
        job=job,
    )


@router.post("/{document_id}/reject", response_model=QualityReviewResponse)
def reject_document(
    document_id: uuid.UUID,
    request: QualityReviewRequest,
    db: DatabaseSession,
) -> QualityReviewResponse:
    action, job = reject_quarantined_document(
        db=db,
        document_id=document_id,
        actor=request.actor,
        reason=request.reason,
    )
    return QualityReviewResponse(
        action=QualityReviewActionRead.model_validate(action),
        job=job,
    )


@router.post("/{document_id}/quality/re-evaluate", response_model=QualityReviewResponse)
def reevaluate_quality(
    document_id: uuid.UUID,
    request: QualityReviewRequest,
    db: DatabaseSession,
    dispatcher: DispatcherDependency,
) -> QualityReviewResponse:
    action, job = reevaluate_document_quality(
        db=db,
        dispatcher=dispatcher,
        document_id=document_id,
        actor=request.actor,
        reason=request.reason,
    )
    return QualityReviewResponse(
        action=QualityReviewActionRead.model_validate(action),
        job=job,
    )


@router.get(
    "/{document_id}/versions/{version_id}/chunking-runs",
    response_model=list[ChunkingRunRead],
)
def list_chunking_runs(
    document_id: uuid.UUID, version_id: uuid.UUID, db: DatabaseSession
) -> list[ChunkingRun]:
    _require_version(db, document_id, version_id)
    return list(
        db.scalars(
            select(ChunkingRun)
            .where(ChunkingRun.document_version_id == version_id)
            .order_by(ChunkingRun.started_at.desc())
        )
    )


@router.get(
    "/{document_id}/versions/{version_id}/chunking-runs/{run_id}/artifact",
    response_model=ChunkingArtifact,
)
def get_chunking_artifact(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    run_id: uuid.UUID,
    db: DatabaseSession,
    storage: StorageDependency,
) -> ChunkingArtifact:
    run = _require_chunking_run(db, document_id, version_id, run_id)
    if run.artifact_uri is None:
        raise AppError(
            code="CHUNKING_ARTIFACT_NOT_AVAILABLE",
            message="The chunking run has no completed node artifact",
            status_code=409,
        )
    return ChunkingArtifact.model_validate(storage.read_json(run.artifact_uri))


@router.get(
    "/{document_id}/versions/{version_id}/chunking-runs/{run_id}/report",
    response_model=ChunkingReport,
)
def get_chunking_report(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    run_id: uuid.UUID,
    db: DatabaseSession,
    storage: StorageDependency,
) -> ChunkingReport:
    run = _require_chunking_run(db, document_id, version_id, run_id)
    if run.report_artifact_uri is None:
        raise AppError(
            code="CHUNKING_REPORT_NOT_AVAILABLE",
            message="The chunking run has no completed report artifact",
            status_code=409,
        )
    return ChunkingReport.model_validate(storage.read_json(run.report_artifact_uri))


@router.get(
    "/{document_id}/versions/{version_id}/retrieval-nodes",
    response_model=list[RetrievalNodeRead],
)
def list_retrieval_nodes(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    db: DatabaseSession,
    node_level: RetrievalNodeLevel | None = None,
    parent_node_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RetrievalNode]:
    _require_version(db, document_id, version_id)
    filters = [RetrievalNode.document_version_id == version_id]
    if node_level is not None:
        filters.append(RetrievalNode.node_level == node_level)
    if parent_node_id is not None:
        filters.append(RetrievalNode.parent_node_id == parent_node_id)
    return list(
        db.scalars(
            select(RetrievalNode)
            .where(*filters)
            .order_by(RetrievalNode.node_level.desc(), RetrievalNode.created_at, RetrievalNode.id)
            .limit(limit)
            .offset(offset)
        )
    )


@router.get(
    "/{document_id}/versions/{version_id}/retrieval-nodes/{node_id}",
    response_model=RetrievalNodeRead,
)
def get_retrieval_node(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    node_id: uuid.UUID,
    db: DatabaseSession,
) -> RetrievalNode:
    _require_version(db, document_id, version_id)
    node = db.scalar(
        select(RetrievalNode).where(
            RetrievalNode.id == node_id,
            RetrievalNode.document_version_id == version_id,
        )
    )
    if node is None:
        raise AppError(
            code="RETRIEVAL_NODE_NOT_FOUND",
            message="Retrieval node was not found",
            status_code=404,
        )
    return node


@router.get(
    "/{document_id}/versions/{version_id}/embedding-runs",
    response_model=list[EmbeddingRunRead],
)
def list_embedding_runs(
    document_id: uuid.UUID, version_id: uuid.UUID, db: DatabaseSession
) -> list[EmbeddingRun]:
    _require_version(db, document_id, version_id)
    return list(
        db.scalars(
            select(EmbeddingRun)
            .where(EmbeddingRun.document_version_id == version_id)
            .options(selectinload(EmbeddingRun.batches))
            .order_by(EmbeddingRun.started_at.desc())
        )
    )


@router.get(
    "/{document_id}/versions/{version_id}/indexing-runs",
    response_model=list[IndexingRunRead],
)
def list_indexing_runs(
    document_id: uuid.UUID, version_id: uuid.UUID, db: DatabaseSession
) -> list[IndexingRun]:
    _require_version(db, document_id, version_id)
    return list(
        db.scalars(
            select(IndexingRun)
            .where(IndexingRun.document_version_id == version_id)
            .order_by(IndexingRun.started_at.desc())
        )
    )


def _require_version(db: Session, document_id: uuid.UUID, version_id: uuid.UUID) -> DocumentVersion:
    version = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.id == version_id, DocumentVersion.document_id == document_id
        )
    )
    if version is None:
        raise AppError(
            code="DOCUMENT_VERSION_NOT_FOUND",
            message="Document version was not found",
            status_code=404,
        )
    return version


def _require_cleaning_run(
    db: Session,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    run_id: uuid.UUID,
) -> CleaningRun:
    _require_version(db, document_id, version_id)
    run = db.scalar(
        select(CleaningRun).where(
            CleaningRun.id == run_id,
            CleaningRun.document_version_id == version_id,
        )
    )
    if run is None:
        raise AppError(
            code="CLEANING_RUN_NOT_FOUND",
            message="Cleaning run was not found",
            status_code=404,
        )
    return run


def _require_quality_assessment(
    db: Session,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    assessment_id: uuid.UUID,
) -> QualityAssessment:
    _require_version(db, document_id, version_id)
    assessment = db.scalar(
        select(QualityAssessment).where(
            QualityAssessment.id == assessment_id,
            QualityAssessment.document_version_id == version_id,
        )
    )
    if assessment is None:
        raise AppError(
            code="QUALITY_ASSESSMENT_NOT_FOUND",
            message="Quality assessment was not found",
            status_code=404,
        )
    return assessment


def _require_chunking_run(
    db: Session,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    run_id: uuid.UUID,
) -> ChunkingRun:
    _require_version(db, document_id, version_id)
    run = db.scalar(
        select(ChunkingRun).where(
            ChunkingRun.id == run_id,
            ChunkingRun.document_version_id == version_id,
        )
    )
    if run is None:
        raise AppError(
            code="CHUNKING_RUN_NOT_FOUND",
            message="Chunking run was not found",
            status_code=404,
        )
    return run
