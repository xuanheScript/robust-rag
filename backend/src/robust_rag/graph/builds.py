"""Manual, auditable knowledge-graph build requests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from robust_rag.core.settings import Settings
from robust_rag.db.enums import (
    DocumentStatus,
    GraphBuildRequestStatus,
    GraphBuildRequestType,
    GraphProjectionStatus,
    QualityDecisionValue,
    RetrievalNodeLevel,
    VersionStatus,
)
from robust_rag.db.models import Document, DocumentVersion, GraphBuildRequest, RetrievalNode
from robust_rag.graph.schemas import GraphBuildPreviewItem, GraphBuildPreviewResponse
from robust_rag.services.dispatcher import GraphExtractionDispatcher

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GraphBuildValidationError(Exception):
    code: str
    message: str
    details: dict[str, object]

    def __str__(self) -> str:
        return self.message


def preview_graph_builds(
    db: Session,
    *,
    document_ids: list[uuid.UUID],
    force: bool,
    settings: Settings,
) -> GraphBuildPreviewResponse:
    unique_ids = list(dict.fromkeys(document_ids))
    documents = {
        value.id: value
        for value in db.scalars(select(Document).where(Document.id.in_(unique_ids)))
    }
    items = [
        _preview_item(db, documents.get(document_id), document_id, force=force)
        for document_id in unique_ids
    ]
    eligible = [value for value in items if value.eligible]
    estimated_input_tokens = sum(value.estimated_input_tokens for value in eligible)
    input_price = settings.llm_price_per_million_input_tokens
    estimated_cost = (
        estimated_input_tokens * input_price / 1_000_000
        if input_price is not None
        else None
    )
    return GraphBuildPreviewResponse(
        items=items,
        eligible_count=len(eligible),
        parent_count=sum(value.parent_count for value in eligible),
        estimated_calls=sum(value.parent_count for value in eligible),
        estimated_input_tokens=estimated_input_tokens,
        estimated_input_cost_usd=estimated_cost,
    )


def create_graph_builds(
    db: Session,
    *,
    dispatcher: GraphExtractionDispatcher,
    document_ids: list[uuid.UUID],
    requested_by: str,
    force: bool,
    settings: Settings,
) -> tuple[uuid.UUID, list[GraphBuildRequest]]:
    unique_ids = list(dict.fromkeys(document_ids))
    locked_documents = list(
        db.scalars(
            select(Document)
            .where(Document.id.in_(unique_ids))
            .order_by(Document.id)
            .with_for_update()
        )
    )
    documents = {value.id: value for value in locked_documents}
    preview = GraphBuildPreviewResponse(
        **preview_graph_builds(
            db,
            document_ids=unique_ids,
            force=force,
            settings=settings,
        ).model_dump()
    )
    ineligible = [value for value in preview.items if not value.eligible]
    if len(documents) != len(unique_ids) or ineligible:
        raise GraphBuildValidationError(
            code="GRAPH_BUILD_SELECTION_INVALID",
            message="One or more documents cannot start graph generation",
            details={"items": [value.model_dump(mode="json") for value in preview.items]},
        )

    batch_id = uuid.uuid4()
    now = datetime.now(UTC)
    requests: list[GraphBuildRequest] = []
    for item in preview.items:
        assert item.document_version_id is not None
        version = db.get(DocumentVersion, item.document_version_id)
        assert version is not None
        effective_force = bool(
            force
            or version.graph_active
            or version.graph_projected_at is not None
            or version.graph_status
            in {
                GraphProjectionStatus.SUCCEEDED,
                GraphProjectionStatus.STALE,
                GraphProjectionStatus.HIDDEN,
            }
        )
        request_type = _request_type(version)
        request_id = uuid.uuid4()
        request = GraphBuildRequest(
            id=request_id,
            batch_id=batch_id,
            document_id=item.document_id,
            document_version_id=version.id,
            request_type=request_type,
            status=GraphBuildRequestStatus.PENDING,
            requested_by=requested_by,
            idempotency_key=f"manual:{version.id}:{request_id}",
            force=effective_force,
            projection_was_active=version.graph_active,
            parent_count=item.parent_count,
            estimated_input_tokens=item.estimated_input_tokens,
            estimated_input_cost_usd=(
                item.estimated_input_tokens
                * settings.llm_price_per_million_input_tokens
                / 1_000_000
                if settings.llm_price_per_million_input_tokens is not None
                else None
            ),
            max_attempts=settings.graph_build_max_attempts,
            previous_graph_status=version.graph_status.value,
            requested_at=now,
        )
        db.add(request)
        version.graph_status = GraphProjectionStatus.PENDING
        requests.append(request)
    db.commit()

    for request in requests:
        try:
            request.celery_task_id = dispatcher.dispatch(request.id)
            db.add(request)
            db.commit()
        except Exception as exc:
            db.rollback()
            stored = db.get(GraphBuildRequest, request.id)
            if stored is not None:
                stored.status = GraphBuildRequestStatus.FAILED
                stored.error = {
                    "code": "GRAPH_BUILD_DISPATCH_FAILED",
                    "type": type(exc).__name__,
                    "message": "Graph extraction could not be queued",
                }
                stored.finished_at = datetime.now(UTC)
                version = db.get(DocumentVersion, stored.document_version_id)
                if version is not None and version.graph_status is GraphProjectionStatus.PENDING:
                    version.graph_status = GraphProjectionStatus.FAILED
                db.commit()
            logger.exception(
                "graph_build_dispatch_failed",
                graph_build_request_id=str(request.id),
                document_version_id=str(request.document_version_id),
                error_type=type(exc).__name__,
            )
    return batch_id, [db.get(GraphBuildRequest, value.id) or value for value in requests]


def _preview_item(
    db: Session,
    document: Document | None,
    document_id: uuid.UUID,
    *,
    force: bool,
) -> GraphBuildPreviewItem:
    if document is None:
        return GraphBuildPreviewItem(
            document_id=document_id,
            display_name=str(document_id),
            eligible=False,
            reason="document_not_found",
        )
    if document.status is DocumentStatus.DELETED:
        return GraphBuildPreviewItem(
            document_id=document.id,
            display_name=document.display_name,
            eligible=False,
            reason="document_deleted",
        )
    if document.current_version_id is None:
        return GraphBuildPreviewItem(
            document_id=document.id,
            display_name=document.display_name,
            eligible=False,
            reason="document_not_ready",
        )
    version = db.get(DocumentVersion, document.current_version_id)
    if version is None or version.status is not VersionStatus.READY:
        return GraphBuildPreviewItem(
            document_id=document.id,
            document_version_id=document.current_version_id,
            display_name=document.display_name,
            graph_status=(version.graph_status.value if version is not None else None),
            graph_active=(version.graph_active if version is not None else False),
            eligible=False,
            reason="document_not_ready",
        )
    active_request = db.scalar(
        select(GraphBuildRequest.id)
        .where(
            GraphBuildRequest.document_version_id == version.id,
            GraphBuildRequest.status.in_(
                [GraphBuildRequestStatus.PENDING, GraphBuildRequestStatus.RUNNING]
            ),
        )
        .limit(1)
    )
    reason: str | None = None
    if active_request is not None or version.graph_status in {
        GraphProjectionStatus.PENDING,
        GraphProjectionStatus.RUNNING,
    }:
        reason = "graph_build_in_progress"
    elif version.graph_active and not force:
        reason = "graph_already_available"
    parent_count = int(
        db.scalar(
            select(func.count())
            .select_from(RetrievalNode)
            .where(
                RetrievalNode.document_version_id == version.id,
                RetrievalNode.node_level == RetrievalNodeLevel.PARENT,
                RetrievalNode.quality_status.in_(
                    [QualityDecisionValue.PASSED, QualityDecisionValue.WARNING]
                ),
            )
        )
        or 0
    )
    estimated_tokens = int(
        db.scalar(
            select(func.coalesce(func.sum(RetrievalNode.token_count), 0)).where(
                RetrievalNode.document_version_id == version.id,
                RetrievalNode.node_level == RetrievalNodeLevel.PARENT,
                RetrievalNode.quality_status.in_(
                    [QualityDecisionValue.PASSED, QualityDecisionValue.WARNING]
                ),
            )
        )
        or 0
    )
    if reason is None and parent_count == 0:
        reason = "no_parent_nodes"
    return GraphBuildPreviewItem(
        document_id=document.id,
        document_version_id=version.id,
        display_name=document.display_name,
        graph_status=version.graph_status.value,
        graph_active=version.graph_active,
        eligible=reason is None,
        reason=reason,
        parent_count=parent_count,
        estimated_input_tokens=estimated_tokens,
    )


def _request_type(version: DocumentVersion) -> GraphBuildRequestType:
    if version.graph_active or version.graph_status in {
        GraphProjectionStatus.SUCCEEDED,
        GraphProjectionStatus.STALE,
        GraphProjectionStatus.HIDDEN,
    }:
        return GraphBuildRequestType.REBUILD
    if version.graph_status is GraphProjectionStatus.FAILED:
        return GraphBuildRequestType.RETRY
    return GraphBuildRequestType.GENERATE
