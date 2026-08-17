"""Ingestion job query and retry APIs."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from robust_rag.api.schemas.documents import JobDetail, JobListResponse, JobRead
from robust_rag.core.errors import AppError
from robust_rag.db.models import IngestionJob
from robust_rag.db.session import get_db
from robust_rag.services.dispatcher import JobDispatcher, get_job_dispatcher
from robust_rag.services.ingestion import retry_failed_job

router = APIRouter(prefix="/jobs", tags=["jobs"])
DatabaseSession = Annotated[Session, Depends(get_db)]
DispatcherDependency = Annotated[JobDispatcher, Depends(get_job_dispatcher)]


@router.get("", response_model=JobListResponse)
def list_jobs(
    db: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobListResponse:
    total = db.scalar(select(func.count()).select_from(IngestionJob)) or 0
    jobs = list(
        db.scalars(
            select(IngestionJob)
            .order_by(IngestionJob.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return JobListResponse(items=jobs, total=total)


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: uuid.UUID, db: DatabaseSession) -> IngestionJob:
    job = db.scalar(
        select(IngestionJob)
        .where(IngestionJob.id == job_id)
        .options(selectinload(IngestionJob.stage_runs))
    )
    if job is None:
        raise AppError(code="JOB_NOT_FOUND", message="Job was not found", status_code=404)
    return job


@router.post("/{job_id}/retry", response_model=JobRead)
def retry_job(
    job_id: uuid.UUID,
    db: DatabaseSession,
    dispatcher: DispatcherDependency,
) -> IngestionJob:
    return retry_failed_job(db, dispatcher, job_id)
