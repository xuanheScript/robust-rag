from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.cleaning.pipeline import CleaningConfig, CleaningPipeline
from robust_rag.cleaning.service import CleaningService
from robust_rag.db.enums import (
    CleaningRunStatus,
    JobStatus,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import CleaningRun, IngestionJob, StageRun
from robust_rag.storage.local import LocalFileStorage
from tests.test_parsing_service import build_service, create_text_job


def build_cleaning_service(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    *,
    config_version: str = "test-cleaning-v1",
    near_duplicate_threshold: float = 0.92,
) -> CleaningService:
    return CleaningService(
        session_factory=session_factory,
        storage=storage,
        pipeline=CleaningPipeline(
            CleaningConfig(
                config_version=config_version,
                near_duplicate_threshold=near_duplicate_threshold,
                near_duplicate_min_chars=10,
            )
        ),
    )


def test_cleaning_service_persists_artifacts_advances_and_is_idempotent(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    text = (
        "Title\n\n"
        "This is a long duplicate-like sentence version A.\n\n"
        "This is a long duplicate-like sentence version B."
    )
    document_id, version_id, job_id = create_text_job(session_factory, storage, text)
    assert build_service(session_factory, storage).execute(job_id) == "deferred"
    service = build_cleaning_service(session_factory, storage)

    assert service.execute(job_id) == "deferred"
    assert service.execute(job_id) == "deferred"

    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.status is JobStatus.PENDING
        assert job.current_stage is StageName.DOCUMENT_EVALUATING
        assert job.progress_current == 3
        assert job.document_version.status is VersionStatus.DOCUMENT_EVALUATING
        runs = list(
            db.scalars(select(CleaningRun).where(CleaningRun.document_version_id == version_id))
        )
        assert len(runs) == 1
        run = runs[0]
        assert run.status is CleaningRunStatus.SUCCEEDED
        assert run.output_artifact_uri
        assert run.report_artifact_uri
        assert run.output_content_hash and len(run.output_content_hash) == 64
        stage_runs = list(
            db.scalars(
                select(StageRun).where(
                    StageRun.job_id == job_id,
                    StageRun.stage_name == StageName.CLEANING,
                )
            )
        )
        assert len(stage_runs) == 1
        assert stage_runs[0].status is StageRunStatus.SUCCEEDED

    raw = storage.read_json(run.input_artifact_uri)
    cleaned = storage.read_json(run.output_artifact_uri or "")
    report = storage.read_json(run.report_artifact_uri or "")
    assert raw["blocks"][1]["original_text"] == cleaned["blocks"][1]["original_text"]
    assert report["cleaning_run_id"] == str(run.id)
    assert report["config_version"] == "test-cleaning-v1"

    runs_response = client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs"
    )
    assert runs_response.status_code == 200
    assert runs_response.json()[0]["id"] == str(run.id)
    document_response = client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs/{run.id}/document"
    )
    assert document_response.status_code == 200
    report_response = client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs/{run.id}/report"
    )
    assert report_response.status_code == 200


def test_cleaning_runs_with_different_configs_can_be_compared(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    text = (
        "This is a long duplicate-like sentence version A.\n\n"
        "This is a long duplicate-like sentence version B."
    )
    document_id, version_id, job_id = create_text_job(session_factory, storage, text)
    assert build_service(session_factory, storage).execute(job_id) == "deferred"
    assert (
        build_cleaning_service(
            session_factory,
            storage,
            config_version="compare-v1",
            near_duplicate_threshold=0.9,
        ).execute(job_id)
        == "deferred"
    )
    assert (
        build_cleaning_service(
            session_factory,
            storage,
            config_version="compare-v2",
            near_duplicate_threshold=1.0,
        ).execute(job_id)
        == "deferred"
    )

    with session_factory() as db:
        runs = list(
            db.scalars(
                select(CleaningRun)
                .where(CleaningRun.document_version_id == version_id)
                .order_by(CleaningRun.started_at)
            )
        )
        assert len(runs) == 2

    response = client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/cleaning-runs/{runs[0].id}/compare",
        params={"against_run_id": str(runs[1].id)},
    )
    assert response.status_code == 200
    comparison = response.json()
    assert comparison["base_run_id"] == str(runs[0].id)
    assert comparison["compared_run_id"] == str(runs[1].id)
    assert comparison["same_output"] is False
    assert comparison["base_issue_count"] > comparison["compared_issue_count"]


def test_cleaning_service_fails_clearly_without_canonical_input(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _, _, job_id = create_text_job(session_factory, storage)

    assert build_cleaning_service(session_factory, storage).execute(job_id) == "failed"

    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert job.error_code == "CANONICAL_DOCUMENT_NOT_FOUND"
        assert list(db.scalars(select(CleaningRun))) == []
