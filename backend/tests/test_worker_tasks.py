import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from pytest import MonkeyPatch
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.db.enums import JobStatus, JobType, StageName, StageRunStatus, VersionStatus
from robust_rag.db.models import Document, DocumentVersion, IngestionJob, StageRun
from robust_rag.workers import tasks


def create_job(
    session_factory: sessionmaker[Session], *, stage: StageName, updated_at: datetime | None = None
) -> uuid.UUID:
    with session_factory.begin() as db:
        document = Document(display_name=f"document-{uuid.uuid4()}")
        version = DocumentVersion(
            document=document,
            version_number=1,
            original_filename="fixture.txt",
            mime_type="text/plain",
            file_size=7,
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            storage_uri="local://originals/fixture.txt",
            status=VersionStatus.UPLOADED,
        )
        job = IngestionJob(
            document_version=version,
            job_type=JobType.INGESTION,
            status=JobStatus.PENDING,
            current_stage=stage,
            progress_current=0,
            progress_total=8,
        )
        if updated_at is not None:
            job.updated_at = updated_at
        db.add(job)
        db.flush()
        return job.id


def test_advance_dispatches_parsing_service_idempotently(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    job_id = create_job(session_factory, stage=StageName.UPLOAD)
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    fake_service = SimpleNamespace(execute=lambda _job_id: "deferred")
    monkeypatch.setattr(tasks, "get_parsing_service", lambda _factory: fake_service)

    first = tasks.advance_ingestion(str(job_id))
    second = tasks.advance_ingestion(str(job_id))

    assert first == {"job_id": str(job_id), "status": "deferred", "current_stage": "parsing"}
    assert second == first
    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.current_stage is StageName.PARSING
        assert job.progress_current == 1
        runs = list(db.scalars(select(StageRun).where(StageRun.job_id == job_id)))
        assert len(runs) == 1
        assert runs[0].status is StageRunStatus.SUCCEEDED


def test_recovery_requeues_stale_database_jobs(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    job_id = create_job(
        session_factory,
        stage=StageName.PARSING,
        updated_at=datetime.now(UTC) - timedelta(hours=1),
    )
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    dispatched: list[str] = []

    def fake_delay(value: str) -> SimpleNamespace:
        dispatched.append(value)
        return SimpleNamespace(id=f"recovered-{value}")

    monkeypatch.setattr(tasks.advance_ingestion, "delay", fake_delay)

    result = tasks.recover_pending_jobs()

    assert result == {"requeued": 1}
    assert dispatched == [str(job_id)]
    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.celery_task_id == f"recovered-{job_id}"
        assert job.status is JobStatus.PENDING


def test_advance_handles_missing_and_terminal_jobs(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    missing_id = uuid.uuid4()
    assert tasks.advance_ingestion(str(missing_id))["status"] == "not_found"

    job_id = create_job(session_factory, stage=StageName.INDEXING)
    with session_factory.begin() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        job.status = JobStatus.CANCELLED

    result = tasks.advance_ingestion(str(job_id))

    assert result["status"] == "cancelled"
    assert result["current_stage"] == "indexing"


def test_advance_dispatches_cleaning_service(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    job_id = create_job(session_factory, stage=StageName.CLEANING)
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    executed: list[uuid.UUID] = []

    def execute(value: uuid.UUID) -> str:
        executed.append(value)
        return "deferred"

    fake_service = SimpleNamespace(execute=execute)
    monkeypatch.setattr(tasks, "get_cleaning_service", lambda _factory: fake_service)

    result = tasks.advance_ingestion(str(job_id))

    assert executed == [job_id]
    assert result == {
        "job_id": str(job_id),
        "status": "deferred",
        "current_stage": "cleaning",
    }


def test_advance_dispatches_quality_service(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    job_id = create_job(session_factory, stage=StageName.DOCUMENT_EVALUATING)
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    executed: list[uuid.UUID] = []

    def execute(value: uuid.UUID) -> str:
        executed.append(value)
        return "quarantined"

    fake_service = SimpleNamespace(execute=execute)
    monkeypatch.setattr(tasks, "get_quality_service", lambda _factory: fake_service)

    result = tasks.advance_ingestion(str(job_id))

    assert executed == [job_id]
    assert result == {
        "job_id": str(job_id),
        "status": "quarantined",
        "current_stage": "document_evaluating",
    }


def test_advance_dispatches_chunking_service(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    job_id = create_job(session_factory, stage=StageName.CHUNKING)
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    executed: list[uuid.UUID] = []

    def execute(value: uuid.UUID) -> str:
        executed.append(value)
        return "deferred"

    fake_service = SimpleNamespace(execute=execute)
    monkeypatch.setattr(tasks, "get_chunking_service", lambda _factory: fake_service)

    result = tasks.advance_ingestion(str(job_id))

    assert executed == [job_id]
    assert result == {
        "job_id": str(job_id),
        "status": "deferred",
        "current_stage": "chunking",
    }


def test_quality_success_continues_into_chunking(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    job_id = create_job(session_factory, stage=StageName.DOCUMENT_EVALUATING)
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    chunked: list[uuid.UUID] = []

    def evaluate(value: uuid.UUID) -> str:
        with session_factory.begin() as db:
            job = db.get(IngestionJob, value)
            assert job is not None
            job.current_stage = StageName.CHUNKING
        return "deferred"

    def chunk(value: uuid.UUID) -> str:
        chunked.append(value)
        return "deferred"

    monkeypatch.setattr(
        tasks, "get_quality_service", lambda _factory: SimpleNamespace(execute=evaluate)
    )
    monkeypatch.setattr(
        tasks, "get_chunking_service", lambda _factory: SimpleNamespace(execute=chunk)
    )

    result = tasks.advance_ingestion(str(job_id))

    assert chunked == [job_id]
    assert result == {
        "job_id": str(job_id),
        "status": "deferred",
        "current_stage": "chunking",
    }


def test_chunk_evaluation_continues_through_embedding_and_indexing(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    job_id = create_job(session_factory, stage=StageName.CHUNK_EVALUATING)
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    executed: list[str] = []

    def gate(value: uuid.UUID) -> str:
        executed.append("gate")
        with session_factory.begin() as db:
            job = db.get(IngestionJob, value)
            assert job is not None
            job.current_stage = StageName.EMBEDDING
        return "deferred"

    def embed(value: uuid.UUID) -> str:
        executed.append("embedding")
        with session_factory.begin() as db:
            job = db.get(IngestionJob, value)
            assert job is not None
            job.current_stage = StageName.INDEXING
        return "deferred"

    def index(value: uuid.UUID) -> str:
        executed.append("indexing")
        with session_factory.begin() as db:
            job = db.get(IngestionJob, value)
            assert job is not None
            job.status = JobStatus.SUCCEEDED
        return "succeeded"

    monkeypatch.setattr(
        tasks, "RetrievalNodeGateService", lambda _factory: SimpleNamespace(execute=gate)
    )
    monkeypatch.setattr(
        tasks, "get_embedding_service", lambda _factory: SimpleNamespace(execute=embed)
    )
    monkeypatch.setattr(
        tasks, "get_indexing_service", lambda _factory: SimpleNamespace(execute=index)
    )

    result = tasks.advance_ingestion(str(job_id))

    assert executed == ["gate", "embedding", "indexing"]
    assert result == {
        "job_id": str(job_id),
        "status": "succeeded",
        "current_stage": "indexing",
    }
