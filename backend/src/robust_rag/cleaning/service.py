"""Durable orchestration and artifact persistence for the cleaning stage."""

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.cleaning.pipeline import CleaningConfig, CleaningPipeline
from robust_rag.cleaning.schemas import CleaningReport
from robust_rag.core.settings import Settings, get_settings
from robust_rag.db.enums import (
    CleaningRunStatus,
    JobStatus,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import (
    CanonicalDocumentRecord,
    CleaningRun,
    IngestionJob,
    StageRun,
)
from robust_rag.parsing.schemas import CanonicalDocument
from robust_rag.storage.base import FileStorage
from robust_rag.storage.local import get_file_storage


class CleaningError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class CleaningService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        storage: FileStorage,
        pipeline: CleaningPipeline,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.pipeline = pipeline

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
            canonical_record = db.scalar(
                select(CanonicalDocumentRecord)
                .where(CanonicalDocumentRecord.document_version_id == version.id)
                .order_by(CanonicalDocumentRecord.created_at.desc())
                .limit(1)
            )
            if canonical_record is None:
                self._fail_without_run(
                    db,
                    job,
                    CleaningError(
                        "CANONICAL_DOCUMENT_NOT_FOUND",
                        "Cleaning requires a canonical document",
                        retryable=False,
                    ),
                )
                return "failed"

            existing = db.scalar(
                select(CleaningRun)
                .where(
                    CleaningRun.canonical_document_id == canonical_record.id,
                    CleaningRun.pipeline_name == self.pipeline.name,
                    CleaningRun.pipeline_version == self.pipeline.version,
                    CleaningRun.config_version == self.pipeline.config.config_version,
                    CleaningRun.input_content_hash == canonical_record.content_hash,
                    CleaningRun.status == CleaningRunStatus.SUCCEEDED,
                )
                .order_by(CleaningRun.finished_at.desc())
                .limit(1)
            )
            if existing is not None:
                self._advance_to_document_evaluation(job)
                db.commit()
                return "deferred"

            attempt = (
                int(
                    db.scalar(
                        select(func.count(StageRun.id)).where(
                            StageRun.job_id == job.id,
                            StageRun.stage_name == StageName.CLEANING,
                        )
                    )
                    or 0
                )
                + 1
            )
            now = datetime.now(UTC)
            cleaning_run = CleaningRun(
                document_version_id=version.id,
                canonical_document_id=canonical_record.id,
                pipeline_name=self.pipeline.name,
                pipeline_version=self.pipeline.version,
                config_version=self.pipeline.config.config_version,
                config_snapshot=self.pipeline.config_snapshot,
                status=CleaningRunStatus.RUNNING,
                input_artifact_uri=canonical_record.artifact_uri,
                input_content_hash=canonical_record.content_hash,
                input_block_count=canonical_record.block_count,
                started_at=now,
            )
            stage_run = StageRun(
                job_id=job.id,
                stage_name=StageName.CLEANING,
                implementation_name=self.pipeline.name,
                implementation_version=self.pipeline.version,
                config_version=self.pipeline.config.config_version,
                config_snapshot=self.pipeline.config_snapshot,
                status=StageRunStatus.RUNNING,
                attempt=attempt,
                input_artifact_uri=canonical_record.artifact_uri,
                started_at=now,
            )
            job.status = JobStatus.RUNNING
            job.started_at = job.started_at or now
            job.finished_at = None
            job.updated_at = now
            version.status = VersionStatus.CLEANING
            db.add_all([cleaning_run, stage_run])
            db.commit()
            cleaning_run_id = cleaning_run.id
            stage_run_id = stage_run.id
            document_id = version.document_id
            version_id = version.id
            canonical_record_id = canonical_record.id
            input_uri = canonical_record.artifact_uri
            input_hash = canonical_record.content_hash
            input_block_count = canonical_record.block_count

        try:
            source = CanonicalDocument.model_validate(self.storage.read_json(input_uri))
            result = self.pipeline.clean(source)
            output_value = result.document.model_dump(mode="json")
            output_hash = _content_hash(output_value)
            output_uri = self.storage.write_json(
                Path("canonical")
                / str(document_id)
                / str(version_id)
                / "cleaned"
                / str(cleaning_run_id)
                / "canonical-document.json",
                output_value,
            )
            report = CleaningReport(
                cleaning_run_id=str(cleaning_run_id),
                document_id=str(document_id),
                document_version_id=str(version_id),
                canonical_document_id=str(canonical_record_id),
                pipeline_name=self.pipeline.name,
                pipeline_version=self.pipeline.version,
                config_version=self.pipeline.config.config_version,
                config_snapshot=self.pipeline.config_snapshot,
                input_content_hash=input_hash,
                output_content_hash=output_hash,
                input_block_count=input_block_count,
                output_block_count=len(result.document.blocks),
                changed_block_count=len(result.changed_block_ids),
                removed_block_count=len(result.removed_block_ids),
                operator_executions=result.operator_executions,
                issues=result.issues,
            )
            report_uri = self.storage.write_json(
                Path("canonical")
                / str(document_id)
                / str(version_id)
                / "cleaned"
                / str(cleaning_run_id)
                / "cleaning-report.json",
                report.model_dump(mode="json"),
            )
        except CleaningError as exc:
            self._record_failure(job_id, cleaning_run_id, stage_run_id, exc)
            return "failed"
        except Exception as exc:
            self._record_failure(
                job_id,
                cleaning_run_id,
                stage_run_id,
                CleaningError("CLEANING_FAILED", str(exc), retryable=False),
            )
            return "failed"

        with self.session_factory.begin() as db:
            job = db.get(IngestionJob, job_id)
            completed_run = db.get(CleaningRun, cleaning_run_id)
            completed_stage = db.get(StageRun, stage_run_id)
            if job is None or completed_run is None or completed_stage is None:
                raise RuntimeError("Cleaning state disappeared before completion")
            finished_at = datetime.now(UTC)
            completed_run.status = CleaningRunStatus.SUCCEEDED
            completed_run.output_artifact_uri = output_uri
            completed_run.report_artifact_uri = report_uri
            completed_run.output_content_hash = output_hash
            completed_run.output_block_count = len(result.document.blocks)
            completed_run.changed_block_count = len(result.changed_block_ids)
            completed_run.removed_block_count = len(result.removed_block_ids)
            completed_run.issue_count = len(result.issues)
            completed_run.operator_executions = [
                execution.model_dump(mode="json") for execution in result.operator_executions
            ]
            completed_run.finished_at = finished_at
            completed_stage.status = StageRunStatus.SUCCEEDED
            completed_stage.output_artifact_uri = output_uri
            completed_stage.finished_at = finished_at
            self._advance_to_document_evaluation(job)
        return "deferred"

    @contextmanager
    def _job_lock(self, job_id: uuid.UUID) -> Iterator[bool]:
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
        cleaning_run_id: uuid.UUID,
        stage_run_id: uuid.UUID,
        error: CleaningError,
    ) -> None:
        with self.session_factory.begin() as db:
            job = db.get(IngestionJob, job_id)
            cleaning_run = db.get(CleaningRun, cleaning_run_id)
            stage_run = db.get(StageRun, stage_run_id)
            if job is None or cleaning_run is None or stage_run is None:
                return
            finished_at = datetime.now(UTC)
            error_value: dict[str, object] = {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            }
            cleaning_run.status = CleaningRunStatus.FAILED
            cleaning_run.finished_at = finished_at
            cleaning_run.error = error_value
            stage_run.status = StageRunStatus.FAILED
            stage_run.finished_at = finished_at
            stage_run.error = error_value
            job.status = JobStatus.FAILED
            job.error_code = error.code
            job.error_message = error.message
            job.finished_at = finished_at
            job.document_version.status = VersionStatus.FAILED

    @staticmethod
    def _advance_to_document_evaluation(job: IngestionJob) -> None:
        now = datetime.now(UTC)
        job.status = JobStatus.PENDING
        job.current_stage = StageName.DOCUMENT_EVALUATING
        job.progress_current = max(job.progress_current, 3)
        job.error_code = None
        job.error_message = None
        job.updated_at = now
        job.document_version.status = VersionStatus.DOCUMENT_EVALUATING

    @staticmethod
    def _fail_without_run(db: Session, job: IngestionJob, error: CleaningError) -> None:
        finished_at = datetime.now(UTC)
        job.status = JobStatus.FAILED
        job.error_code = error.code
        job.error_message = error.message
        job.finished_at = finished_at
        job.document_version.status = VersionStatus.FAILED
        db.commit()


def _content_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def build_cleaning_pipeline(settings: Settings) -> CleaningPipeline:
    return CleaningPipeline(
        CleaningConfig(
            config_version=settings.cleaning_config_version,
            boilerplate_min_occurrences=settings.cleaning_boilerplate_min_occurrences,
            boilerplate_min_page_ratio=settings.cleaning_boilerplate_min_page_ratio,
            near_duplicate_threshold=settings.cleaning_near_duplicate_threshold,
            near_duplicate_min_chars=settings.cleaning_near_duplicate_min_chars,
        )
    )


def get_cleaning_service(session_factory: sessionmaker[Session]) -> CleaningService:
    return CleaningService(
        session_factory=session_factory,
        storage=get_file_storage(),
        pipeline=build_cleaning_pipeline(get_settings()),
    )
