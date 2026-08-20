import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.db.enums import (
    JobStatus,
    QualityAssessmentStatus,
    QualityDecisionValue,
    QualityReviewActionValue,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import (
    IngestionJob,
    QualityAssessment,
    QualityReviewAction,
    StageRun,
)
from robust_rag.quality.deterministic import EvaluatorResult
from robust_rag.quality.dingo import DingoAdapterError, FakeDingoAdapter
from robust_rag.quality.engine import QualityConfig, QualityEngine
from robust_rag.quality.schemas import (
    DimensionScore,
    EvaluatorExecution,
    EvaluatorStatus,
    QualityDimension,
    QualityEvidence,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueSource,
)
from robust_rag.quality.service import QualityService
from robust_rag.storage.local import LocalFileStorage
from tests.fakes import FakeDispatcher
from tests.test_cleaning_service import build_cleaning_service
from tests.test_parsing_service import build_service, create_text_job


def prepare_cleaned_job(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    text = "Policy\n\nThis policy contains complete and traceable business information."
    document_id, version_id, job_id = create_text_job(session_factory, storage, text)
    assert build_service(session_factory, storage).execute(job_id) == "deferred"
    assert build_cleaning_service(session_factory, storage).execute(job_id) == "deferred"
    return document_id, version_id, job_id


def build_quality_service(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    engine: QualityEngine | None = None,
) -> QualityService:
    return QualityService(
        session_factory=session_factory,
        storage=storage,
        engine=engine or QualityEngine(),
    )


def _empty_dingo_result(evaluator_type: str) -> EvaluatorResult:
    return EvaluatorResult(
        scores=[],
        issues=[],
        execution=EvaluatorExecution(
            name=f"fake-{evaluator_type}",
            version="1.0",
            evaluator_type=evaluator_type,
            status=EvaluatorStatus.SUCCEEDED,
            duration_ms=1,
            issue_count=0,
        ),
    )


def _quarantine_result() -> EvaluatorResult:
    evidence = QualityEvidence(metric="fake_high_risk", value=0.0)
    return EvaluatorResult(
        scores=[
            DimensionScore(
                dimension=QualityDimension.RETRIEVAL_READINESS,
                score=0.0,
                evidence=[evidence],
            )
        ],
        issues=[
            QualityIssue(
                code="DINGO_HIGH_RISK",
                dimension=QualityDimension.RETRIEVAL_READINESS,
                severity=QualityIssueSeverity.HIGH,
                source=QualityIssueSource.DINGO_LLM,
                evaluator="fake-dingo",
                evaluator_version="1.0",
                message="fixture quarantine",
                evidence=[evidence],
            )
        ],
        execution=EvaluatorExecution(
            name="fake-dingo-llm",
            version="1.0",
            evaluator_type="dingo_llm",
            status=EvaluatorStatus.SUCCEEDED,
            duration_ms=1,
            issue_count=1,
        ),
    )


def quarantine_engine(*, failure: DingoAdapterError | None = None) -> QualityEngine:
    adapter = FakeDingoAdapter(
        rule_result=_empty_dingo_result("dingo_rule"),
        llm_result=_quarantine_result(),
        llm_error=failure,
    )
    return QualityEngine(
        QualityConfig(dingo_llm_enabled=True),
        dingo_adapter=adapter,
    )


def test_quality_service_persists_report_advances_and_is_idempotent(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    document_id, version_id, job_id = prepare_cleaned_job(session_factory, storage)
    service = build_quality_service(session_factory, storage)

    assert service.execute(job_id) == "deferred"
    assert service.execute(job_id) == "deferred"

    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.status is JobStatus.PENDING
        assert job.current_stage is StageName.CHUNKING
        assert job.document_version.status is VersionStatus.CHUNKING
        assessments = list(
            db.scalars(
                select(QualityAssessment).where(QualityAssessment.document_version_id == version_id)
            )
        )
        assert len(assessments) == 1
        assessment = assessments[0]
        assert assessment.status is QualityAssessmentStatus.SUCCEEDED
        assert assessment.decision in {
            QualityDecisionValue.PASSED,
            QualityDecisionValue.WARNING,
        }
        assert assessment.raw_result_uri
        stage = db.scalar(
            select(StageRun).where(
                StageRun.job_id == job_id,
                StageRun.stage_name == StageName.DOCUMENT_EVALUATING,
            )
        )
        assert stage is not None
        assert stage.status is StageRunStatus.SUCCEEDED

    report = storage.read_json(assessment.raw_result_uri or "")
    assert report["assessment_id"] == str(assessment.id)
    assert len(report["dimensions"]) == 8
    assert report["evaluator_executions"][-1]["status"] == "skipped"

    response = client.get(f"/api/v1/documents/{document_id}/quality")
    assert response.status_code == 200
    assert response.json()[0]["id"] == str(assessment.id)
    report_response = client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/quality-assessments/"
        f"{assessment.id}/report"
    )
    assert report_response.status_code == 200


def test_quarantine_blocks_pipeline_and_manual_release_is_audited(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
    dispatcher: FakeDispatcher,
) -> None:
    document_id, version_id, job_id = prepare_cleaned_job(session_factory, storage)
    service = build_quality_service(session_factory, storage, quarantine_engine())

    assert service.execute(job_id) == "quarantined"
    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.status is JobStatus.QUARANTINED
        assert job.current_stage is StageName.DOCUMENT_EVALUATING
        assert job.document_version.status is VersionStatus.QUARANTINED

    response = client.post(
        f"/api/v1/documents/{document_id}/release",
        json={"actor": "local-reviewer", "reason": "Verified against source pages"},
    )
    assert response.status_code == 200
    assert response.json()["action"]["action"] == "release"
    assert response.json()["job"]["current_stage"] == "chunking"
    assert dispatcher.dispatched[-1] == job_id

    duplicate = client.post(
        f"/api/v1/documents/{document_id}/release",
        json={"actor": "local-reviewer", "reason": "Submitted twice"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "QUALITY_ALREADY_REVIEWED"

    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.status is JobStatus.PENDING
        assert job.document_version.status is VersionStatus.CHUNKING
        action = db.scalar(
            select(QualityReviewAction).where(QualityReviewAction.document_version_id == version_id)
        )
        assert action is not None
        assert action.action is QualityReviewActionValue.RELEASE
        assert action.previous_decision is QualityDecisionValue.QUARANTINED
        assert action.quality_snapshot["issues"]


def test_manual_rejection_and_reevaluation_are_audited(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    document_id, version_id, job_id = prepare_cleaned_job(session_factory, storage)
    service = build_quality_service(session_factory, storage, quarantine_engine())
    assert service.execute(job_id) == "quarantined"

    reevaluate = client.post(
        f"/api/v1/documents/{document_id}/quality/re-evaluate",
        json={"reason": "Run the updated external evaluator again"},
    )
    assert reevaluate.status_code == 200
    assert reevaluate.json()["action"]["action"] == "reevaluate"
    assert service.execute(job_id) == "quarantined"

    with session_factory() as db:
        assessments = list(
            db.scalars(
                select(QualityAssessment).where(QualityAssessment.document_version_id == version_id)
            )
        )
        assert len(assessments) == 2

    reject = client.post(
        f"/api/v1/documents/{document_id}/reject",
        json={"reason": "Confirmed unusable source"},
    )
    assert reject.status_code == 200
    assert reject.json()["action"]["action"] == "reject"
    assert reject.json()["job"]["status"] == "failed"


def test_dingo_failure_is_visible_retryable_and_not_a_quality_decision(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    _, version_id, job_id = prepare_cleaned_job(session_factory, storage)
    error = DingoAdapterError("DINGO_TIMEOUT", "provider timed out", retryable=True)
    failing = build_quality_service(session_factory, storage, quarantine_engine(failure=error))

    assert failing.execute(job_id) == "failed"
    with session_factory() as db:
        assessment = db.scalar(
            select(QualityAssessment).where(QualityAssessment.document_version_id == version_id)
        )
        assert assessment is not None
        assert assessment.status is QualityAssessmentStatus.FAILED
        assert assessment.decision is None
        assert assessment.error == {
            "code": "DINGO_TIMEOUT",
            "message": "provider timed out",
            "retryable": True,
        }

    retry = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retry.status_code == 200
    assert retry.json()["status"] == "pending"
    assert build_quality_service(session_factory, storage).execute(job_id) == "deferred"
