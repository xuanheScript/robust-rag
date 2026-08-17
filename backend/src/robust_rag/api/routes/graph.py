"""Constrained knowledge-graph browse, review, and rebuild APIs."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from robust_rag.core.errors import AppError
from robust_rag.core.settings import get_settings
from robust_rag.db.enums import GraphConflictStatus
from robust_rag.db.models import (
    Document,
    GraphConflictRecord,
    GraphEntityRecord,
    GraphExtractionRun,
    GraphFactRecord,
)
from robust_rag.db.session import get_db
from robust_rag.graph.admin import GraphAdminError, GraphAdminService
from robust_rag.graph.factory import get_graph_extraction_service, graph_is_configured
from robust_rag.graph.schema import get_graph_schema
from robust_rag.graph.schemas import (
    GraphConflictRead,
    GraphConflictResolveRequest,
    GraphEntityCreate,
    GraphEntityMergeRequest,
    GraphEntityRead,
    GraphEntitySplitRequest,
    GraphEntityUpdate,
    GraphExtractionRunRead,
    GraphFactCreate,
    GraphFactRead,
    GraphFactUpdate,
    GraphReviewRequest,
)

router = APIRouter(tags=["knowledge-graph"])
DatabaseSession = Annotated[Session, Depends(get_db)]


def _service(db: Session) -> GraphAdminService:
    settings = get_settings()
    return GraphAdminService(db, get_graph_schema(settings.graph_schema_version))


def _handle(exc: GraphAdminError) -> AppError:
    status_code = 404 if exc.code.endswith("NOT_FOUND") else 409
    return AppError(code=exc.code, message=exc.message, status_code=status_code)


@router.get("/graph/search", response_model=list[GraphEntityRead])
def search_graph(
    db: DatabaseSession,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    entity_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[GraphEntityRecord]:
    try:
        return _service(db).search(q, entity_type=entity_type, limit=limit)
    except GraphAdminError as exc:
        raise _handle(exc) from exc


@router.post("/graph/entities/merge", response_model=GraphEntityRead)
def merge_graph_entities(
    request: GraphEntityMergeRequest, db: DatabaseSession
) -> GraphEntityRecord:
    try:
        return _service(db).merge_entities(request)
    except GraphAdminError as exc:
        raise _handle(exc) from exc


@router.get("/graph/entities/{entity_id}", response_model=GraphEntityRead)
def get_graph_entity(entity_id: uuid.UUID, db: DatabaseSession) -> GraphEntityRecord:
    entity = db.get(GraphEntityRecord, entity_id)
    if entity is None:
        raise AppError(
            code="GRAPH_ENTITY_NOT_FOUND", message="Graph entity was not found", status_code=404
        )
    return entity


@router.get("/graph/entities/{entity_id}/neighborhood")
def graph_neighborhood(
    entity_id: uuid.UUID,
    db: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    try:
        return _service(db).neighborhood(entity_id, limit=limit)
    except GraphAdminError as exc:
        raise _handle(exc) from exc


@router.post("/graph/entities", response_model=GraphEntityRead, status_code=status.HTTP_201_CREATED)
def create_graph_entity(request: GraphEntityCreate, db: DatabaseSession) -> GraphEntityRecord:
    try:
        return _service(db).create_entity(request)
    except GraphAdminError as exc:
        raise _handle(exc) from exc


@router.patch("/graph/entities/{entity_id}", response_model=GraphEntityRead)
def update_graph_entity(
    entity_id: uuid.UUID, request: GraphEntityUpdate, db: DatabaseSession
) -> GraphEntityRecord:
    entity = get_graph_entity(entity_id, db)
    try:
        return _service(db).update_entity(entity, request)
    except GraphAdminError as exc:
        raise _handle(exc) from exc


@router.post("/graph/entities/{entity_id}/split", response_model=GraphEntityRead)
def split_graph_entity(
    entity_id: uuid.UUID, request: GraphEntitySplitRequest, db: DatabaseSession
) -> GraphEntityRecord:
    entity = get_graph_entity(entity_id, db)
    try:
        return _service(db).split_entity(entity, request)
    except GraphAdminError as exc:
        raise _handle(exc) from exc


@router.post("/graph/relations", response_model=GraphFactRead, status_code=status.HTTP_201_CREATED)
def create_graph_relation(request: GraphFactCreate, db: DatabaseSession) -> GraphFactRecord:
    try:
        return _service(db).create_fact(request)
    except GraphAdminError as exc:
        raise _handle(exc) from exc


@router.patch("/graph/relations/{relation_id}", response_model=GraphFactRead)
def update_graph_relation(
    relation_id: uuid.UUID, request: GraphFactUpdate, db: DatabaseSession
) -> GraphFactRecord:
    fact = db.get(GraphFactRecord, relation_id)
    if fact is None:
        raise AppError(
            code="GRAPH_FACT_NOT_FOUND", message="Graph fact was not found", status_code=404
        )
    try:
        return _service(db).update_fact(fact, request)
    except GraphAdminError as exc:
        raise _handle(exc) from exc


def _review_fact(
    fact_id: uuid.UUID, request: GraphReviewRequest, db: Session, *, approve: bool
) -> GraphFactRecord:
    fact = db.get(GraphFactRecord, fact_id)
    if fact is None:
        raise AppError(
            code="GRAPH_FACT_NOT_FOUND", message="Graph fact was not found", status_code=404
        )
    return _service(db).review_fact(fact, approve=approve, reason=request.reason)


@router.post("/graph/facts/{fact_id}/approve", response_model=GraphFactRead)
def approve_graph_fact(
    fact_id: uuid.UUID, request: GraphReviewRequest, db: DatabaseSession
) -> GraphFactRecord:
    return _review_fact(fact_id, request, db, approve=True)


@router.post("/graph/facts/{fact_id}/reject", response_model=GraphFactRead)
def reject_graph_fact(
    fact_id: uuid.UUID, request: GraphReviewRequest, db: DatabaseSession
) -> GraphFactRecord:
    return _review_fact(fact_id, request, db, approve=False)


@router.get("/graph/conflicts", response_model=list[GraphConflictRead])
def list_graph_conflicts(
    db: DatabaseSession,
    conflict_status: GraphConflictStatus = GraphConflictStatus.PENDING,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[GraphConflictRecord]:
    return list(
        db.scalars(
            select(GraphConflictRecord)
            .where(GraphConflictRecord.status == conflict_status)
            .order_by(GraphConflictRecord.created_at.desc())
            .limit(limit)
        )
    )


def _resolve_conflict(
    conflict_id: uuid.UUID,
    request: GraphConflictResolveRequest,
    db: Session,
    *,
    dismiss: bool,
) -> GraphConflictRecord:
    conflict = db.get(GraphConflictRecord, conflict_id)
    if conflict is None:
        raise AppError(
            code="GRAPH_CONFLICT_NOT_FOUND",
            message="Graph conflict was not found",
            status_code=404,
        )
    try:
        return _service(db).resolve_conflict(conflict, request, dismiss=dismiss)
    except GraphAdminError as exc:
        raise _handle(exc) from exc


@router.post("/graph/conflicts/{conflict_id}/resolve", response_model=GraphConflictRead)
def resolve_graph_conflict(
    conflict_id: uuid.UUID,
    request: GraphConflictResolveRequest,
    db: DatabaseSession,
) -> GraphConflictRecord:
    return _resolve_conflict(conflict_id, request, db, dismiss=False)


@router.post("/graph/conflicts/{conflict_id}/dismiss", response_model=GraphConflictRead)
def dismiss_graph_conflict(
    conflict_id: uuid.UUID,
    request: GraphConflictResolveRequest,
    db: DatabaseSession,
) -> GraphConflictRecord:
    return _resolve_conflict(conflict_id, request, db, dismiss=True)


@router.get(
    "/documents/{document_id}/versions/{version_id}/graph-runs",
    response_model=list[GraphExtractionRunRead],
)
def list_graph_runs(
    document_id: uuid.UUID, version_id: uuid.UUID, db: DatabaseSession
) -> list[GraphExtractionRun]:
    document = db.get(Document, document_id)
    if document is None:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="Document was not found", status_code=404)
    return list(
        db.scalars(
            select(GraphExtractionRun)
            .where(GraphExtractionRun.document_version_id == version_id)
            .order_by(GraphExtractionRun.started_at.desc())
        )
    )


@router.post("/documents/{document_id}/graph/rebuild")
def rebuild_document_graph(document_id: uuid.UUID, db: DatabaseSession) -> dict[str, object]:
    document = db.get(Document, document_id)
    if document is None:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="Document was not found", status_code=404)
    settings = get_settings()
    if not graph_is_configured(settings):
        raise AppError(
            code="GRAPH_NOT_CONFIGURED",
            message="Neo4j graph projection is not configured",
            status_code=409,
        )
    counts = get_graph_extraction_service().rebuild()
    return {"document_id": str(document_id), "status": "succeeded", **counts}
