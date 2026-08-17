"""Stage 7 retrieval comparison and trace debugging APIs."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from robust_rag.core.errors import AppError
from robust_rag.db.models import RetrievalTrace
from robust_rag.db.session import get_db
from robust_rag.retrieval.query import QueryError
from robust_rag.retrieval.schemas import (
    RetrievalSearchRequest,
    RetrievalSearchResponse,
    RetrievalTraceRead,
)
from robust_rag.retrieval.service import RetrievalError, RetrievalService, get_retrieval_service

router = APIRouter(prefix="/retrieval", tags=["retrieval"])
DatabaseSession = Annotated[Session, Depends(get_db)]
RetrievalDependency = Annotated[RetrievalService, Depends(get_retrieval_service)]


@router.post("/search", response_model=RetrievalSearchResponse)
def search(
    request: RetrievalSearchRequest, service: RetrievalDependency
) -> RetrievalSearchResponse:
    try:
        return service.search(request)
    except QueryError as exc:
        raise AppError(code=exc.code, message=exc.message, status_code=422) from exc
    except RetrievalError as exc:
        raise AppError(
            code=exc.code,
            message=exc.message,
            status_code=503 if exc.retryable else 409,
            details={"retryable": exc.retryable},
        ) from exc


@router.get("/traces", response_model=list[RetrievalTraceRead])
def list_retrieval_traces(
    db: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[RetrievalTrace]:
    return list(
        db.scalars(select(RetrievalTrace).order_by(RetrievalTrace.started_at.desc()).limit(limit))
    )


@router.get("/traces/{trace_id}", response_model=RetrievalTraceRead)
def get_retrieval_trace(trace_id: uuid.UUID, db: DatabaseSession) -> RetrievalTrace:
    trace = db.get(RetrievalTrace, trace_id)
    if trace is None:
        raise AppError(
            code="RETRIEVAL_TRACE_NOT_FOUND",
            message="Retrieval trace was not found",
            status_code=404,
        )
    return trace
