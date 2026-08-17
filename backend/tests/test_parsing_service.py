import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.db.enums import (
    JobStatus,
    JobType,
    ParseRunStatus,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import (
    CanonicalDocumentRecord,
    Document,
    DocumentVersion,
    IngestionJob,
    ParseRun,
    StageRun,
)
from robust_rag.parsing.base import FileMetadata, ParseError
from robust_rag.parsing.canonicalizer import Canonicalizer
from robust_rag.parsing.native import PlainTextParser
from robust_rag.parsing.router import ParserRouter
from robust_rag.parsing.service import ParsingService
from robust_rag.storage.local import LocalFileStorage


def create_text_job(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    text: str = "标题\n\n正文 text",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    source = storage.root / "originals" / str(document_id) / str(version_id) / "fixture.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(text, encoding="utf-8")
    with session_factory.begin() as db:
        document = Document(id=document_id, display_name="Fixture")
        version = DocumentVersion(
            id=version_id,
            document=document,
            version_number=1,
            original_filename="fixture.txt",
            mime_type="text/plain",
            file_size=source.stat().st_size,
            sha256="f" * 64,
            storage_uri=f"local://originals/{document_id}/{version_id}/fixture.txt",
            status=VersionStatus.UPLOADED,
        )
        job = IngestionJob(
            document_version=version,
            job_type=JobType.INGESTION,
            status=JobStatus.PENDING,
            current_stage=StageName.PARSING,
            progress_current=1,
            progress_total=8,
        )
        db.add(job)
        db.flush()
        job_id = job.id
    return document_id, version_id, job_id


def build_service(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    router: ParserRouter | None = None,
) -> ParsingService:
    return ParsingService(
        session_factory=session_factory,
        storage=storage,
        router=router or ParserRouter([PlainTextParser()]),
        canonicalizer=Canonicalizer(),
    )


def test_parsing_service_persists_replayable_artifacts_and_is_idempotent(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    document_id, version_id, job_id = create_text_job(session_factory, storage)
    service = build_service(session_factory, storage)

    assert service.execute(job_id) == "deferred"
    assert service.execute(job_id) == "deferred"

    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.status is JobStatus.PENDING
        assert job.current_stage is StageName.CLEANING
        assert job.progress_current == 2
        assert job.document_version.status is VersionStatus.CLEANING
        parse_runs = list(
            db.scalars(select(ParseRun).where(ParseRun.document_version_id == version_id))
        )
        assert len(parse_runs) == 1
        assert parse_runs[0].status is ParseRunStatus.SUCCEEDED
        assert parse_runs[0].artifact_uri
        stage_run = db.scalar(
            select(StageRun).where(
                StageRun.job_id == job_id, StageRun.stage_name == StageName.PARSING
            )
        )
        assert stage_run is not None
        assert stage_run.status is StageRunStatus.SUCCEEDED
        record = db.scalar(
            select(CanonicalDocumentRecord).where(
                CanonicalDocumentRecord.document_version_id == version_id
            )
        )
        assert record is not None
        assert record.block_count == 3
        assert len(record.content_hash) == 64

    canonical = storage.read_json(record.artifact_uri)
    parsed = storage.read_json(parse_runs[0].artifact_uri or "")
    assert canonical["document_id"] == str(document_id)
    assert canonical["document_version_id"] == str(version_id)
    assert parsed["schema_version"] == "parse-artifact/1.0"


class FailingParser(PlainTextParser):
    name = "failing-parser"

    def parse(self, source_path: Path, metadata: FileMetadata):  # type: ignore[no-untyped-def]
        raise ParseError("FIXTURE_PARSE_FAILED", "expected fixture failure", retryable=True)


def test_parsing_service_records_failure_and_retryability(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _, version_id, job_id = create_text_job(session_factory, storage)
    service = build_service(session_factory, storage, ParserRouter([FailingParser()]))

    assert service.execute(job_id) == "failed"

    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert job.error_code == "FIXTURE_PARSE_FAILED"
        assert job.document_version.status is VersionStatus.FAILED
        parse_run = db.scalar(select(ParseRun).where(ParseRun.document_version_id == version_id))
        assert parse_run is not None
        assert parse_run.status is ParseRunStatus.FAILED
        assert parse_run.error == {
            "code": "FIXTURE_PARSE_FAILED",
            "message": "expected fixture failure",
            "retryable": True,
        }


def test_parsing_service_handles_missing_job_and_missing_parser(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    service = build_service(session_factory, storage)
    assert service.execute(uuid.uuid4()) == "not_found"

    _, _, job_id = create_text_job(session_factory, storage)
    unavailable = build_service(session_factory, storage, ParserRouter([]))
    assert unavailable.execute(job_id) == "failed"
    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.error_code == "PARSER_UNAVAILABLE"
        assert db.scalar(select(func.count(ParseRun.id))) == 0


def test_canonical_api_exposes_metadata_blocks_and_parse_runs(
    client: TestClient,
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
) -> None:
    document_id, version_id, job_id = create_text_job(session_factory, storage)
    assert build_service(session_factory, storage).execute(job_id) == "deferred"

    metadata_response = client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/canonical/metadata"
    )
    assert metadata_response.status_code == 200
    assert metadata_response.json()["block_count"] == 3

    canonical_response = client.get(
        f"/api/v1/documents/{document_id}/versions/{version_id}/canonical"
    )
    assert canonical_response.status_code == 200
    assert canonical_response.json()["blocks"][1]["source_locators"][0]["line_start"] == 1

    runs_response = client.get(f"/api/v1/documents/{document_id}/versions/{version_id}/parse-runs")
    assert runs_response.status_code == 200
    assert runs_response.json()[0]["status"] == "succeeded"

    assert (
        client.get(
            f"/api/v1/documents/{uuid.uuid4()}/versions/{uuid.uuid4()}/canonical"
        ).status_code
        == 404
    )
