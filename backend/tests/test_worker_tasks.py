import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from pytest import MonkeyPatch
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.db.enums import (
    DocumentStatus,
    GraphBuildRequestStatus,
    GraphBuildRequestType,
    GraphProjectionStatus,
    JobStatus,
    JobType,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import (
    Document,
    DocumentVersion,
    GraphBuildRequest,
    IngestionJob,
    StageRun,
)
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


def test_rate_limited_embedding_schedules_durable_continuation(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    job_id = create_job(session_factory, stage=StageName.EMBEDDING)
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(
        tasks,
        "get_embedding_service",
        lambda _factory: SimpleNamespace(
            execute=lambda _job_id: "rate_limited",
            retry_after_seconds=43.2,
        ),
    )
    scheduled: list[tuple[list[str], int]] = []

    def apply_async(*, args: list[str], countdown: int) -> SimpleNamespace:
        scheduled.append((args, countdown))
        return SimpleNamespace(id="embedding-continuation")

    monkeypatch.setattr(tasks.advance_ingestion, "apply_async", apply_async)

    result = tasks._execute_embedding_then_indexing(str(job_id), job_id)

    assert result == {
        "job_id": str(job_id),
        "status": "rate_limited",
        "current_stage": "embedding",
    }
    assert scheduled == [([str(job_id)], 44)]
    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.status is JobStatus.PENDING
        assert job.celery_task_id == "embedding-continuation"


def test_manual_graph_build_request_is_required_and_completed(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    with session_factory.begin() as db:
        document = Document(display_name="manual graph")
        version = DocumentVersion(
            document=document,
            version_number=1,
            original_filename="graph.txt",
            mime_type="text/plain",
            file_size=10,
            sha256="a" * 64,
            storage_uri="local://graph.txt",
            status=VersionStatus.READY,
            graph_status=GraphProjectionStatus.PENDING,
        )
        db.add(version)
        db.flush()
        document.current_version_id = version.id
        request = GraphBuildRequest(
            batch_id=uuid.uuid4(),
            document_id=document.id,
            document_version_id=version.id,
            request_type=GraphBuildRequestType.GENERATE,
            status=GraphBuildRequestStatus.PENDING,
            requested_by="tester",
            idempotency_key=f"test:{version.id}",
            previous_graph_status=GraphProjectionStatus.NOT_REQUESTED.value,
        )
        db.add(request)
        db.flush()
        request_id = request.id
        version_id = version.id

    calls: list[tuple[uuid.UUID, bool, uuid.UUID | None]] = []

    class ExtractionService:
        def execute(
            self,
            value: uuid.UUID,
            *,
            force: bool,
            build_request_id: uuid.UUID | None,
        ) -> str:
            calls.append((value, force, build_request_id))
            return "succeeded"

    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(tasks, "get_graph_extraction_service", ExtractionService)

    result = tasks.extract_graph(str(request_id))

    assert result["status"] == "succeeded"
    assert calls == [(version_id, False, request_id)]
    with session_factory() as db:
        stored_request = db.get(GraphBuildRequest, request_id)
        stored_version = db.get(DocumentVersion, version_id)
        assert (
            stored_request is not None
            and stored_request.status is GraphBuildRequestStatus.SUCCEEDED
        )
        assert stored_request.started_at is not None and stored_request.finished_at is not None
        assert (
            stored_version is not None
            and stored_version.graph_status is GraphProjectionStatus.SUCCEEDED
        )
        assert stored_version.graph_active


def test_old_or_unrequested_graph_task_exits_without_extraction(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)

    def unexpected_service() -> object:
        raise AssertionError("unrequested graph work must not reach the LLM service")

    monkeypatch.setattr(tasks, "get_graph_extraction_service", unexpected_service)

    result = tasks.extract_graph(str(uuid.uuid4()))

    assert result["status"] == "cancelled"


def test_cancelled_graph_build_cannot_reactivate_a_deleted_document(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    with session_factory.begin() as db:
        document = Document(display_name="deleted during graph build")
        version = DocumentVersion(
            document=document,
            version_number=1,
            original_filename="graph.txt",
            mime_type="text/plain",
            file_size=10,
            sha256="b" * 64,
            storage_uri="local://graph-deleted.txt",
            status=VersionStatus.READY,
            graph_status=GraphProjectionStatus.PENDING,
        )
        db.add(version)
        db.flush()
        document.current_version_id = version.id
        request = GraphBuildRequest(
            batch_id=uuid.uuid4(),
            document_id=document.id,
            document_version_id=version.id,
            request_type=GraphBuildRequestType.GENERATE,
            status=GraphBuildRequestStatus.PENDING,
            requested_by="tester",
            idempotency_key=f"deleted:{version.id}",
            previous_graph_status=GraphProjectionStatus.NOT_REQUESTED.value,
        )
        db.add(request)
        db.flush()
        request_id = request.id
        version_id = version.id
        document_id = document.id

    class ExtractionService:
        def execute(
            self,
            value: uuid.UUID,
            *,
            force: bool,
            build_request_id: uuid.UUID | None,
        ) -> str:
            del value, force, build_request_id
            with session_factory.begin() as db:
                stored_document = db.get(Document, document_id)
                stored_version = db.get(DocumentVersion, version_id)
                stored_request = db.get(GraphBuildRequest, request_id)
                assert stored_document is not None
                assert stored_version is not None
                assert stored_request is not None
                stored_document.status = DocumentStatus.DELETED
                stored_document.current_version_id = None
                stored_request.status = GraphBuildRequestStatus.CANCELLED
                stored_version.graph_active = True
                stored_version.graph_status = GraphProjectionStatus.SUCCEEDED
                stored_version.graph_projected_at = datetime.now(UTC)
            return "succeeded"

    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(tasks, "get_graph_extraction_service", ExtractionService)
    monkeypatch.setattr(tasks, "get_graph_lifecycle_service", lambda: None)

    result = tasks.extract_graph(str(request_id))

    assert result["status"] == "cancelled"
    with session_factory() as db:
        stored_request = db.get(GraphBuildRequest, request_id)
        stored_version = db.get(DocumentVersion, version_id)
        assert (
            stored_request is not None
            and stored_request.status is GraphBuildRequestStatus.CANCELLED
        )
        assert stored_version is not None
        assert stored_version.graph_status is GraphProjectionStatus.HIDDEN
        assert not stored_version.graph_active


def test_indexing_success_does_not_automatically_enqueue_graph(
    session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    job_id = create_job(session_factory, stage=StageName.INDEXING)
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(
        tasks,
        "get_indexing_service",
        lambda _factory: SimpleNamespace(execute=lambda _job_id: "succeeded"),
    )

    def unexpected_delay(_value: str) -> None:
        raise AssertionError("indexing must not enqueue graph generation")

    monkeypatch.setattr(tasks.extract_graph, "delay", unexpected_delay)

    result = tasks._execute_indexing(str(job_id), job_id)

    assert result == {
        "job_id": str(job_id),
        "status": "succeeded",
        "current_stage": "indexing",
    }
