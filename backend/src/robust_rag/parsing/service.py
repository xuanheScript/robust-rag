"""Durable parsing-stage orchestration and artifact persistence."""

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.core.settings import Settings, get_settings
from robust_rag.db.enums import JobStatus, ParseRunStatus, StageName, StageRunStatus, VersionStatus
from robust_rag.db.models import (
    CanonicalDocumentRecord,
    DocumentVersion,
    IngestionJob,
    ParseRun,
    StageRun,
)
from robust_rag.parsing.base import FileMetadata, ParseError
from robust_rag.parsing.canonicalizer import Canonicalizer
from robust_rag.parsing.mineru import MinerUParser
from robust_rag.parsing.native import (
    ExcelParser,
    HtmlParser,
    LegacyOfficeParser,
    MarkdownParser,
    PlainTextParser,
    PowerPointParser,
    WordParser,
)
from robust_rag.parsing.router import ParserRouter
from robust_rag.parsing.schemas import CANONICAL_SCHEMA_VERSION
from robust_rag.storage.base import FileStorage
from robust_rag.storage.local import get_file_storage


class ParsingService:
    config_version = "stage2-parsing-v1"

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        storage: FileStorage,
        router: ParserRouter,
        canonicalizer: Canonicalizer,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.router = router
        self.canonicalizer = canonicalizer

    def execute(self, job_id: uuid.UUID) -> str:
        with self._job_lock(job_id) as acquired:
            if not acquired:
                return "running"
            return self._execute_locked(job_id)

    def _execute_locked(self, job_id: uuid.UUID) -> str:
        with self.session_factory() as db:
            job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id).with_for_update())
            if job is None:
                return "not_found"
            version = job.document_version
            existing = db.scalar(
                select(CanonicalDocumentRecord).where(
                    CanonicalDocumentRecord.document_version_id == version.id,
                    CanonicalDocumentRecord.schema_version == CANONICAL_SCHEMA_VERSION,
                )
            )
            if existing is not None:
                self._advance_to_cleaning(job, version)
                db.commit()
                return "deferred"

            metadata = FileMetadata(
                filename=version.original_filename,
                mime_type=version.mime_type,
                file_size=version.file_size,
                sha256=version.sha256,
            )
            try:
                source_path = self.storage.resolve(version.storage_uri)
                parser = self.router.select(source_path, metadata)
            except ParseError as exc:
                self._fail_without_run(db, job, version, exc)
                return "failed"
            except Exception as exc:
                error = ParseError("SOURCE_ASSET_UNAVAILABLE", str(exc), retryable=True)
                self._fail_without_run(db, job, version, error)
                return "failed"

            attempt = (
                int(
                    db.scalar(
                        select(func.count(StageRun.id)).where(
                            StageRun.job_id == job.id, StageRun.stage_name == StageName.PARSING
                        )
                    )
                    or 0
                )
                + 1
            )
            now = datetime.now(UTC)
            parse_run = ParseRun(
                document_version_id=version.id,
                parser_name=parser.name,
                parser_version=parser.version,
                parser_mode=parser.mode,
                parser_config={"config_version": self.config_version},
                status=ParseRunStatus.RUNNING,
                started_at=now,
            )
            stage_run = StageRun(
                job_id=job.id,
                stage_name=StageName.PARSING,
                implementation_name=parser.name,
                implementation_version=parser.version,
                config_version=self.config_version,
                config_snapshot={"mode": parser.mode, "mime_type": metadata.mime_type},
                status=StageRunStatus.RUNNING,
                attempt=attempt,
                input_artifact_uri=version.storage_uri,
                started_at=now,
            )
            job.status = JobStatus.RUNNING
            job.started_at = job.started_at or now
            job.updated_at = now
            version.status = VersionStatus.PARSING
            db.add_all([parse_run, stage_run])
            db.commit()
            parse_run_id = parse_run.id
            stage_run_id = stage_run.id
            document_id = version.document_id
            version_id = version.id

        try:
            artifact = parser.parse(source_path, metadata)
            canonical = self.canonicalizer.convert(
                artifact=artifact, document_id=document_id, version_id=version_id
            )
            parse_uri = self.storage.write_json(
                Path("parse-artifacts")
                / str(document_id)
                / str(version_id)
                / str(parse_run_id)
                / "artifact.json",
                artifact.model_dump(mode="json"),
            )
            canonical_value = canonical.model_dump(mode="json")
            canonical_payload = json.dumps(
                canonical_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            content_hash = hashlib.sha256(canonical_payload).hexdigest()
            canonical_uri = self.storage.write_json(
                Path("canonical")
                / str(document_id)
                / str(version_id)
                / "canonical-document-v1.json",
                canonical_value,
            )
        except ParseError as exc:
            self._record_failure(job_id, parse_run_id, stage_run_id, exc)
            return "failed"
        except Exception as exc:
            error = ParseError("PARSING_FAILED", str(exc), retryable=False)
            self._record_failure(job_id, parse_run_id, stage_run_id, error)
            return "failed"

        with self.session_factory.begin() as db:
            job = db.get(IngestionJob, job_id)
            completed_parse_run = db.get(ParseRun, parse_run_id)
            completed_stage_run = db.get(StageRun, stage_run_id)
            if job is None or completed_parse_run is None or completed_stage_run is None:
                raise RuntimeError("Parsing state disappeared before completion")
            version = job.document_version
            finished_at = datetime.now(UTC)
            completed_parse_run.status = ParseRunStatus.SUCCEEDED
            completed_parse_run.artifact_uri = parse_uri
            completed_parse_run.finished_at = finished_at
            completed_stage_run.status = StageRunStatus.SUCCEEDED
            completed_stage_run.output_artifact_uri = canonical_uri
            completed_stage_run.finished_at = finished_at
            db.add(
                CanonicalDocumentRecord(
                    document_version_id=version.id,
                    parse_run_id=completed_parse_run.id,
                    schema_version=canonical.schema_version,
                    artifact_uri=canonical_uri,
                    language=canonical.language,
                    title=canonical.title,
                    block_count=len(canonical.blocks),
                    content_hash=content_hash,
                )
            )
            self._advance_to_cleaning(job, version)
        return "deferred"

    @contextmanager
    def _job_lock(self, job_id: uuid.UUID) -> Iterator[bool]:
        """Serialize one job across worker processes with a PostgreSQL advisory lock."""

        bind = self.session_factory.kw.get("bind")
        if not isinstance(bind, Engine) or bind.dialect.name != "postgresql":
            yield True
            return
        lock_key = job_id.int % (2**63 - 1)
        with bind.connect() as connection:
            acquired = bool(
                connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
            )
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})

    def _record_failure(
        self,
        job_id: uuid.UUID,
        parse_run_id: uuid.UUID,
        stage_run_id: uuid.UUID,
        error: ParseError,
    ) -> None:
        with self.session_factory.begin() as db:
            job = db.get(IngestionJob, job_id)
            parse_run = db.get(ParseRun, parse_run_id)
            stage_run = db.get(StageRun, stage_run_id)
            if job is None or parse_run is None or stage_run is None:
                return
            finished_at = datetime.now(UTC)
            error_value: dict[str, object] = {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            }
            parse_run.status = ParseRunStatus.FAILED
            parse_run.finished_at = finished_at
            parse_run.error = error_value
            stage_run.status = StageRunStatus.FAILED
            stage_run.finished_at = finished_at
            stage_run.error = error_value
            job.status = JobStatus.FAILED
            job.error_code = error.code
            job.error_message = error.message
            job.finished_at = finished_at
            job.document_version.status = VersionStatus.FAILED

    @staticmethod
    def _advance_to_cleaning(job: IngestionJob, version: DocumentVersion) -> None:
        job.status = JobStatus.PENDING
        job.current_stage = StageName.CLEANING
        job.progress_current = max(job.progress_current, 2)
        job.error_code = None
        job.error_message = None
        job.updated_at = datetime.now(UTC)
        version.status = VersionStatus.CLEANING

    @staticmethod
    def _fail_without_run(
        db: Session, job: IngestionJob, version: DocumentVersion, error: ParseError
    ) -> None:
        finished_at = datetime.now(UTC)
        job.status = JobStatus.FAILED
        job.error_code = error.code
        job.error_message = error.message
        job.finished_at = finished_at
        version.status = VersionStatus.FAILED
        db.commit()


def build_parser_router(settings: Settings) -> ParserRouter:
    word = WordParser()
    powerpoint = PowerPointParser()
    excel = ExcelParser()
    return ParserRouter(
        [
            MinerUParser(
                base_url=settings.mineru_base_url,
                api_key=settings.mineru_api_key,
                timeout_seconds=settings.mineru_timeout_seconds,
                backend=settings.mineru_backend,
            ),
            word,
            powerpoint,
            excel,
            LegacyOfficeParser(
                settings.libreoffice_path,
                {".docx": word, ".pptx": powerpoint, ".xlsx": excel},
            ),
            HtmlParser(),
            MarkdownParser(),
            PlainTextParser(),
        ]
    )


def get_parsing_service(session_factory: sessionmaker[Session]) -> ParsingService:
    settings = get_settings()
    return ParsingService(
        session_factory=session_factory,
        storage=get_file_storage(),
        router=build_parser_router(settings),
        canonicalizer=Canonicalizer(),
    )
