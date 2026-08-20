"""Audited local-admin actions for quarantined quality assessments."""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from robust_rag.core.errors import AppError
from robust_rag.db.enums import (
    JobStatus,
    QualityAssessmentStatus,
    QualityDecisionValue,
    QualityReviewActionValue,
    StageName,
    VersionStatus,
)
from robust_rag.db.models import (
    Document,
    DocumentVersion,
    IngestionJob,
    QualityAssessment,
    QualityReviewAction,
)
from robust_rag.services.dispatcher import JobDispatcher

logger = structlog.get_logger(__name__)


def release_quarantined_document(
    *,
    db: Session,
    dispatcher: JobDispatcher,
    document_id: uuid.UUID,
    actor: str,
    reason: str,
) -> tuple[QualityReviewAction, IngestionJob]:
    version, job, assessment = _review_context(db, document_id)
    _ensure_not_already_reviewed(db, assessment)
    if assessment.decision is not QualityDecisionValue.QUARANTINED:
        raise AppError(
            code="QUALITY_RELEASE_NOT_ALLOWED",
            message="Only a quarantined assessment can be manually released",
            status_code=409,
        )
    action = _record_action(
        db,
        version=version,
        job=job,
        assessment=assessment,
        action=QualityReviewActionValue.RELEASE,
        actor=actor,
        reason=reason,
    )
    job.status = JobStatus.PENDING
    job.current_stage = StageName.CHUNKING
    job.progress_current = max(job.progress_current, 4)
    job.error_code = None
    job.error_message = None
    job.finished_at = None
    job.updated_at = datetime.now(UTC)
    version.status = VersionStatus.CHUNKING
    db.commit()
    _dispatch_reviewed_job(db, dispatcher, job)
    return action, job


def reject_quarantined_document(
    *,
    db: Session,
    document_id: uuid.UUID,
    actor: str,
    reason: str,
) -> tuple[QualityReviewAction, IngestionJob]:
    version, job, assessment = _review_context(db, document_id)
    _ensure_not_already_reviewed(db, assessment)
    if assessment.decision is not QualityDecisionValue.QUARANTINED:
        raise AppError(
            code="QUALITY_REJECT_NOT_ALLOWED",
            message="Only a quarantined assessment can be manually rejected",
            status_code=409,
        )
    action = _record_action(
        db,
        version=version,
        job=job,
        assessment=assessment,
        action=QualityReviewActionValue.REJECT,
        actor=actor,
        reason=reason,
    )
    job.status = JobStatus.FAILED
    job.error_code = "QUALITY_MANUALLY_REJECTED"
    job.error_message = reason
    job.finished_at = datetime.now(UTC)
    job.updated_at = datetime.now(UTC)
    version.status = VersionStatus.FAILED
    db.commit()
    return action, job


def reevaluate_document_quality(
    *,
    db: Session,
    dispatcher: JobDispatcher,
    document_id: uuid.UUID,
    actor: str,
    reason: str,
) -> tuple[QualityReviewAction, IngestionJob]:
    version, job, assessment = _review_context(db, document_id)
    if assessment.decision not in {
        QualityDecisionValue.QUARANTINED,
        QualityDecisionValue.REJECTED,
    }:
        raise AppError(
            code="QUALITY_REEVALUATION_NOT_ALLOWED",
            message="Only quarantined or rejected assessments can be re-evaluated",
            status_code=409,
        )
    action = _record_action(
        db,
        version=version,
        job=job,
        assessment=assessment,
        action=QualityReviewActionValue.REEVALUATE,
        actor=actor,
        reason=reason,
    )
    job.status = JobStatus.PENDING
    job.current_stage = StageName.DOCUMENT_EVALUATING
    job.attempt += 1
    job.error_code = None
    job.error_message = None
    job.finished_at = None
    job.updated_at = datetime.now(UTC)
    version.status = VersionStatus.DOCUMENT_EVALUATING
    db.commit()
    _dispatch_reviewed_job(db, dispatcher, job)
    return action, job


def _review_context(
    db: Session, document_id: uuid.UUID
) -> tuple[DocumentVersion, IngestionJob, QualityAssessment]:
    document = db.get(Document, document_id)
    if document is None:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="Document was not found", status_code=404)
    version = db.scalar(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc())
        .limit(1)
        .with_for_update()
    )
    if version is None:
        raise AppError(
            code="DOCUMENT_VERSION_NOT_FOUND",
            message="Document version was not found",
            status_code=404,
        )
    job = db.scalar(
        select(IngestionJob)
        .where(IngestionJob.document_version_id == version.id)
        .order_by(IngestionJob.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    assessment = db.scalar(
        select(QualityAssessment)
        .where(
            QualityAssessment.document_version_id == version.id,
            QualityAssessment.status == QualityAssessmentStatus.SUCCEEDED,
        )
        .order_by(QualityAssessment.finished_at.desc())
        .limit(1)
    )
    if job is None or assessment is None or assessment.decision is None:
        raise AppError(
            code="QUALITY_REVIEW_NOT_AVAILABLE",
            message="No completed document quality assessment is available",
            status_code=409,
        )
    return version, job, assessment


def _ensure_not_already_reviewed(db: Session, assessment: QualityAssessment) -> None:
    latest_decision = db.scalar(
        select(QualityReviewAction)
        .where(
            QualityReviewAction.assessment_id == assessment.id,
            QualityReviewAction.action.in_(
                [QualityReviewActionValue.RELEASE, QualityReviewActionValue.REJECT]
            ),
        )
        .order_by(QualityReviewAction.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if latest_decision is not None:
        decision_label = (
            "released" if latest_decision.action is QualityReviewActionValue.RELEASE else "rejected"
        )
        raise AppError(
            code="QUALITY_ALREADY_REVIEWED",
            message=f"Quality assessment was already {decision_label}",
            status_code=409,
        )


def _record_action(
    db: Session,
    *,
    version: DocumentVersion,
    job: IngestionJob,
    assessment: QualityAssessment,
    action: QualityReviewActionValue,
    actor: str,
    reason: str,
) -> QualityReviewAction:
    assert assessment.decision is not None
    audit = QualityReviewAction(
        document_version_id=version.id,
        assessment_id=assessment.id,
        action=action,
        actor=actor,
        reason=reason,
        previous_job_status=job.status.value,
        previous_version_status=version.status.value,
        previous_decision=assessment.decision,
        quality_snapshot={
            "overall_score": assessment.overall_score,
            "dimensions": assessment.dimensions_json,
            "issues": assessment.issues_json,
            "report_uri": assessment.raw_result_uri,
        },
    )
    db.add(audit)
    db.flush()
    return audit


def _dispatch_reviewed_job(db: Session, dispatcher: JobDispatcher, job: IngestionJob) -> None:
    try:
        job.celery_task_id = dispatcher.dispatch(job.id)
        db.add(job)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("quality_review_dispatch_deferred", job_id=str(job.id), error=str(exc))
