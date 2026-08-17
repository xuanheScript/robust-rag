"""Durable document quality stage orchestration and report persistence."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.core.settings import Settings, get_settings
from robust_rag.db.enums import (
    CleaningRunStatus,
    JobStatus,
    QualityAssessmentStatus,
    QualityDecisionValue,
    QualityReviewActionValue,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import (
    CleaningRun,
    IngestionJob,
    QualityAssessment,
    QualityReviewAction,
    StageRun,
)
from robust_rag.parsing.schemas import CanonicalDocument
from robust_rag.quality.dingo import DingoAdapterError, DingoConfig, DingoPythonAdapter
from robust_rag.quality.engine import QualityConfig, QualityEngine
from robust_rag.quality.schemas import QualityReport
from robust_rag.storage.base import FileStorage
from robust_rag.storage.local import get_file_storage


class QualityStageError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class QualityService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        storage: FileStorage,
        engine: QualityEngine,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.engine = engine

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
            cleaning_run = db.scalar(
                select(CleaningRun)
                .where(
                    CleaningRun.document_version_id == version.id,
                    CleaningRun.status == CleaningRunStatus.SUCCEEDED,
                )
                .order_by(CleaningRun.finished_at.desc())
                .limit(1)
            )
            if (
                cleaning_run is None
                or cleaning_run.output_artifact_uri is None
                or cleaning_run.output_content_hash is None
            ):
                self._fail_without_assessment(
                    db,
                    job,
                    QualityStageError(
                        "CLEANED_DOCUMENT_NOT_FOUND",
                        "Document quality evaluation requires a completed cleaning run",
                        retryable=False,
                    ),
                )
                return "failed"

            existing = self._existing_assessment(db, cleaning_run)
            if existing is not None and not self._reevaluation_requested(db, existing):
                self._apply_decision(job, existing.decision)
                db.commit()
                return self._decision_status(existing.decision)

            attempt = (
                int(
                    db.scalar(
                        select(func.count(StageRun.id)).where(
                            StageRun.job_id == job.id,
                            StageRun.stage_name == StageName.DOCUMENT_EVALUATING,
                        )
                    )
                    or 0
                )
                + 1
            )
            now = datetime.now(UTC)
            assessment = QualityAssessment(
                document_version_id=version.id,
                cleaning_run_id=cleaning_run.id,
                target_type="document",
                target_id=version.id,
                evaluator=self.engine.name,
                evaluator_version=self.engine.version,
                engine_version=self.engine.version,
                rule_set_version=self.engine.config.rule_set_version,
                policy_version=self.engine.config.policy_version,
                config_snapshot=self.engine.config_snapshot,
                model=self._configured_model(),
                prompt_version=self._configured_prompt_version(),
                status=QualityAssessmentStatus.RUNNING,
                input_content_hash=cleaning_run.output_content_hash,
                started_at=now,
            )
            stage_run = StageRun(
                job_id=job.id,
                stage_name=StageName.DOCUMENT_EVALUATING,
                implementation_name=self.engine.name,
                implementation_version=self.engine.version,
                config_version=self.engine.config.policy_version,
                config_snapshot=self.engine.config_snapshot,
                status=StageRunStatus.RUNNING,
                attempt=attempt,
                input_artifact_uri=cleaning_run.output_artifact_uri,
                started_at=now,
            )
            job.status = JobStatus.RUNNING
            job.started_at = job.started_at or now
            job.finished_at = None
            job.error_code = None
            job.error_message = None
            job.updated_at = now
            version.status = VersionStatus.DOCUMENT_EVALUATING
            db.add_all([assessment, stage_run])
            db.commit()
            assessment_id = assessment.id
            stage_run_id = stage_run.id
            document_id = version.document_id
            version_id = version.id
            cleaning_run_id = cleaning_run.id
            input_uri = cleaning_run.output_artifact_uri
            input_hash = cleaning_run.output_content_hash

        try:
            document = CanonicalDocument.model_validate(self.storage.read_json(input_uri))
            result = self.engine.evaluate(document)
            report = QualityReport(
                assessment_id=str(assessment_id),
                document_id=str(document_id),
                document_version_id=str(version_id),
                cleaning_run_id=str(cleaning_run_id),
                target_id=str(version_id),
                engine_name=self.engine.name,
                engine_version=self.engine.version,
                rule_set_version=self.engine.config.rule_set_version,
                policy_version=self.engine.config.policy_version,
                config_snapshot=self.engine.config_snapshot,
                input_content_hash=input_hash,
                decision=result.decision,
                overall_score=result.overall_score,
                dimensions=result.dimensions,
                issues=result.issues,
                evaluator_executions=result.evaluator_executions,
            )
            report_uri = self.storage.write_json(
                Path("quality")
                / str(document_id)
                / str(version_id)
                / str(assessment_id)
                / "quality-report.json",
                report.model_dump(mode="json"),
            )
        except DingoAdapterError as exc:
            self._record_failure(
                job_id,
                assessment_id,
                stage_run_id,
                QualityStageError(exc.code, exc.message, retryable=exc.retryable),
            )
            return "failed"
        except Exception as exc:
            self._record_failure(
                job_id,
                assessment_id,
                stage_run_id,
                QualityStageError("QUALITY_EVALUATION_FAILED", str(exc), retryable=False),
            )
            return "failed"

        with self.session_factory.begin() as db:
            job = db.get(IngestionJob, job_id)
            completed = db.get(QualityAssessment, assessment_id)
            completed_stage = db.get(StageRun, stage_run_id)
            if job is None or completed is None or completed_stage is None:
                raise RuntimeError("Quality assessment state disappeared before completion")
            finished_at = datetime.now(UTC)
            decision_value = QualityDecisionValue(result.decision.value)
            completed.status = QualityAssessmentStatus.SUCCEEDED
            completed.decision = decision_value
            completed.overall_score = result.overall_score
            completed.dimensions_json = [
                score.model_dump(mode="json") for score in result.dimensions
            ]
            completed.issues_json = [issue.model_dump(mode="json") for issue in result.issues]
            completed.evaluator_executions_json = [
                execution.model_dump(mode="json") for execution in result.evaluator_executions
            ]
            completed.raw_result_uri = report_uri
            completed.finished_at = finished_at
            completed_stage.status = StageRunStatus.SUCCEEDED
            completed_stage.output_artifact_uri = report_uri
            completed_stage.finished_at = finished_at
            self._apply_decision(job, decision_value)
        return self._decision_status(decision_value)

    def _existing_assessment(
        self, db: Session, cleaning_run: CleaningRun
    ) -> QualityAssessment | None:
        return db.scalar(
            select(QualityAssessment)
            .where(
                QualityAssessment.cleaning_run_id == cleaning_run.id,
                QualityAssessment.engine_version == self.engine.version,
                QualityAssessment.rule_set_version == self.engine.config.rule_set_version,
                QualityAssessment.policy_version == self.engine.config.policy_version,
                QualityAssessment.input_content_hash == cleaning_run.output_content_hash,
                QualityAssessment.status == QualityAssessmentStatus.SUCCEEDED,
            )
            .order_by(QualityAssessment.finished_at.desc())
            .limit(1)
        )

    @staticmethod
    def _reevaluation_requested(db: Session, assessment: QualityAssessment) -> bool:
        action = db.scalar(
            select(QualityReviewAction.id)
            .where(
                QualityReviewAction.assessment_id == assessment.id,
                QualityReviewAction.action == QualityReviewActionValue.REEVALUATE,
            )
            .limit(1)
        )
        return action is not None

    @staticmethod
    def _apply_decision(job: IngestionJob, decision: QualityDecisionValue | None) -> None:
        now = datetime.now(UTC)
        if decision in {QualityDecisionValue.PASSED, QualityDecisionValue.WARNING}:
            job.status = JobStatus.PENDING
            job.current_stage = StageName.CHUNKING
            job.progress_current = max(job.progress_current, 4)
            job.error_code = None
            job.error_message = None
            job.finished_at = None
            job.document_version.status = VersionStatus.CHUNKING
        elif decision is QualityDecisionValue.QUARANTINED:
            job.status = JobStatus.QUARANTINED
            job.error_code = "QUALITY_QUARANTINED"
            job.error_message = "Document requires manual quality review"
            job.finished_at = now
            job.document_version.status = VersionStatus.QUARANTINED
        else:
            job.status = JobStatus.FAILED
            job.error_code = "QUALITY_REJECTED"
            job.error_message = "Document failed non-recoverable quality checks"
            job.finished_at = now
            job.document_version.status = VersionStatus.FAILED
        job.updated_at = now

    @staticmethod
    def _decision_status(decision: QualityDecisionValue | None) -> str:
        if decision is QualityDecisionValue.QUARANTINED:
            return "quarantined"
        if decision is QualityDecisionValue.REJECTED:
            return "rejected"
        return "deferred"

    def _configured_model(self) -> str | None:
        adapter = self.engine.dingo_adapter
        if self.engine.config.dingo_llm_enabled and isinstance(adapter, DingoPythonAdapter):
            return adapter.config.llm_model
        return None

    def _configured_prompt_version(self) -> str | None:
        adapter = self.engine.dingo_adapter
        if self.engine.config.dingo_llm_enabled and isinstance(adapter, DingoPythonAdapter):
            return adapter.config.prompt_version
        return None

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
        assessment_id: uuid.UUID,
        stage_run_id: uuid.UUID,
        error: QualityStageError,
    ) -> None:
        with self.session_factory.begin() as db:
            job = db.get(IngestionJob, job_id)
            assessment = db.get(QualityAssessment, assessment_id)
            stage_run = db.get(StageRun, stage_run_id)
            if job is None or assessment is None or stage_run is None:
                return
            finished_at = datetime.now(UTC)
            error_value: dict[str, object] = {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            }
            assessment.status = QualityAssessmentStatus.FAILED
            assessment.finished_at = finished_at
            assessment.error = error_value
            stage_run.status = StageRunStatus.FAILED
            stage_run.finished_at = finished_at
            stage_run.error = error_value
            job.status = JobStatus.FAILED
            job.error_code = error.code
            job.error_message = error.message
            job.finished_at = finished_at
            job.document_version.status = VersionStatus.FAILED

    @staticmethod
    def _fail_without_assessment(db: Session, job: IngestionJob, error: QualityStageError) -> None:
        finished_at = datetime.now(UTC)
        job.status = JobStatus.FAILED
        job.error_code = error.code
        job.error_message = error.message
        job.finished_at = finished_at
        job.document_version.status = VersionStatus.FAILED
        db.commit()


def build_quality_engine(settings: Settings) -> QualityEngine:
    dingo_rules = tuple(
        value.strip() for value in settings.dingo_rule_names.split(",") if value.strip()
    )
    quality_config = QualityConfig(
        rule_set_version=settings.quality_rule_set_version,
        policy_version=settings.quality_policy_version,
        corruption_warning_ratio=settings.quality_corruption_warning_ratio,
        corruption_quarantine_ratio=settings.quality_corruption_quarantine_ratio,
        corruption_reject_ratio=settings.quality_corruption_reject_ratio,
        duplicate_quarantine_ratio=settings.quality_duplicate_quarantine_ratio,
        missing_locator_quarantine_ratio=settings.quality_missing_locator_quarantine_ratio,
        empty_page_quarantine_ratio=settings.quality_empty_page_quarantine_ratio,
        parser_confidence_warning=settings.quality_parser_confidence_warning,
        low_confidence_quarantine_ratio=settings.quality_low_confidence_quarantine_ratio,
        information_density_warning=settings.quality_information_density_warning,
        information_density_quarantine=settings.quality_information_density_quarantine,
        reject_parse_threshold=settings.quality_reject_parse_threshold,
        reject_text_threshold=settings.quality_reject_text_threshold,
        quarantine_dimension_threshold=settings.quality_quarantine_dimension_threshold,
        warning_dimension_threshold=settings.quality_warning_dimension_threshold,
        dingo_rule_enabled=settings.dingo_enabled and settings.dingo_rule_enabled,
        dingo_llm_enabled=settings.dingo_enabled and settings.dingo_llm_enabled,
    )
    adapter = None
    if quality_config.dingo_rule_enabled or quality_config.dingo_llm_enabled:
        adapter = DingoPythonAdapter(
            DingoConfig(
                rule_names=dingo_rules,
                llm_model=settings.llm_model,
                llm_base_url=settings.llm_base_url,
                llm_api_key=(
                    settings.dingo_llm_api_key.get_secret_value()
                    if settings.dingo_llm_api_key
                    else None
                ),
                llm_max_chars=settings.dingo_llm_max_chars,
            )
        )
    return QualityEngine(config=quality_config, dingo_adapter=adapter)


def get_quality_service(session_factory: sessionmaker[Session]) -> QualityService:
    return QualityService(
        session_factory=session_factory,
        storage=get_file_storage(),
        engine=build_quality_engine(get_settings()),
    )
