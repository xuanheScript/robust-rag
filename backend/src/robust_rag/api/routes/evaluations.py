"""Stage 11 golden evaluation run and report APIs."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from robust_rag.core.errors import AppError
from robust_rag.db.models import EvaluationRun
from robust_rag.db.session import get_db
from robust_rag.evaluation.schemas import (
    EvaluationCreate,
    EvaluationRunDetail,
    EvaluationRunRead,
)
from robust_rag.evaluation.service import EvaluationError, EvaluationService, get_evaluation_service

router = APIRouter(prefix="/evaluations", tags=["evaluations"])
DatabaseSession = Annotated[Session, Depends(get_db)]
EvaluationDependency = Annotated[EvaluationService, Depends(get_evaluation_service)]


@router.post("", response_model=EvaluationRunRead, status_code=201)
def create_evaluation(request: EvaluationCreate, service: EvaluationDependency) -> EvaluationRun:
    try:
        return service.create_and_run(request)
    except EvaluationError as exc:
        status = 404 if exc.code in {"DATASET_NOT_FOUND", "EVALUATION_BASELINE_NOT_FOUND"} else 422
        raise AppError(code=exc.code, message=exc.message, status_code=status) from exc


@router.get("", response_model=list[EvaluationRunRead])
def list_evaluations(
    db: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[EvaluationRun]:
    return list(
        db.scalars(select(EvaluationRun).order_by(EvaluationRun.created_at.desc()).limit(limit))
    )


@router.get("/{evaluation_id}", response_model=EvaluationRunDetail)
def get_evaluation(evaluation_id: uuid.UUID, db: DatabaseSession) -> EvaluationRun:
    run = db.scalar(
        select(EvaluationRun)
        .options(selectinload(EvaluationRun.results))
        .where(EvaluationRun.id == evaluation_id)
    )
    if run is None:
        raise AppError(
            code="EVALUATION_NOT_FOUND", message="Evaluation run was not found", status_code=404
        )
    return run
