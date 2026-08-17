"""Durable orchestration and persistence for structure-aware chunking."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.chunking.chunker import ChunkingConfig, StructureAwareChunker
from robust_rag.chunking.schemas import ChunkingArtifact, ChunkingReport, RetrievalNodeData
from robust_rag.core.settings import Settings, get_settings
from robust_rag.db.enums import (
    ChunkingRunStatus,
    CleaningRunStatus,
    JobStatus,
    ProjectionStatus,
    QualityAssessmentStatus,
    QualityDecisionValue,
    QualityReviewActionValue,
    RetrievalNodeLevel,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import (
    ChunkingRun,
    CleaningRun,
    IngestionJob,
    QualityAssessment,
    QualityReviewAction,
    RetrievalNode,
    StageRun,
)
from robust_rag.parsing.schemas import CanonicalDocument
from robust_rag.quality.schemas import QualityDecision
from robust_rag.storage.base import FileStorage
from robust_rag.storage.local import get_file_storage


class ChunkingError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class ChunkingService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        storage: FileStorage,
        chunker: StructureAwareChunker,
    ) -> None:
        self.session_factory = session_factory
        self.storage = storage
        self.chunker = chunker

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
                self._fail_without_run(
                    db,
                    job,
                    ChunkingError(
                        "CLEANED_DOCUMENT_NOT_FOUND",
                        "Chunking requires a completed cleaning run",
                        retryable=False,
                    ),
                )
                return "failed"

            assessment = db.scalar(
                select(QualityAssessment)
                .where(
                    QualityAssessment.cleaning_run_id == cleaning_run.id,
                    QualityAssessment.status == QualityAssessmentStatus.SUCCEEDED,
                )
                .order_by(QualityAssessment.finished_at.desc())
                .limit(1)
            )
            released = assessment is not None and self._manually_released(db, assessment)
            if (
                assessment is None
                or assessment.decision is None
                or (
                    assessment.decision
                    not in {QualityDecisionValue.PASSED, QualityDecisionValue.WARNING}
                    and not released
                )
            ):
                self._fail_without_run(
                    db,
                    job,
                    ChunkingError(
                        "CHUNKING_QUALITY_GATE_BLOCKED",
                        "Chunking requires a passed, warning, or manually released assessment",
                        retryable=False,
                    ),
                )
                return "failed"

            existing = db.scalar(
                select(ChunkingRun)
                .where(
                    ChunkingRun.cleaning_run_id == cleaning_run.id,
                    ChunkingRun.chunker_name == self.chunker.name,
                    ChunkingRun.chunker_version == self.chunker.version,
                    ChunkingRun.config_version == self.chunker.config.config_version,
                    ChunkingRun.input_content_hash == cleaning_run.output_content_hash,
                    ChunkingRun.status == ChunkingRunStatus.SUCCEEDED,
                )
                .order_by(ChunkingRun.finished_at.desc())
                .limit(1)
            )
            if existing is not None:
                self._advance_to_chunk_evaluation(job)
                db.commit()
                return "deferred"

            attempt = (
                int(
                    db.scalar(
                        select(func.count(StageRun.id)).where(
                            StageRun.job_id == job.id,
                            StageRun.stage_name == StageName.CHUNKING,
                        )
                    )
                    or 0
                )
                + 1
            )
            now = datetime.now(UTC)
            chunking_run = ChunkingRun(
                document_version_id=version.id,
                canonical_document_id=cleaning_run.canonical_document_id,
                cleaning_run_id=cleaning_run.id,
                quality_assessment_id=assessment.id,
                chunker_name=self.chunker.name,
                chunker_version=self.chunker.version,
                config_version=self.chunker.config.config_version,
                config_snapshot=self.chunker.config_snapshot,
                status=ChunkingRunStatus.RUNNING,
                input_artifact_uri=cleaning_run.output_artifact_uri,
                input_content_hash=cleaning_run.output_content_hash,
                started_at=now,
            )
            stage_run = StageRun(
                job_id=job.id,
                stage_name=StageName.CHUNKING,
                implementation_name=self.chunker.name,
                implementation_version=self.chunker.version,
                config_version=self.chunker.config.config_version,
                config_snapshot=self.chunker.config_snapshot,
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
            version.status = VersionStatus.CHUNKING
            db.add_all([chunking_run, stage_run])
            db.commit()
            run_id = chunking_run.id
            stage_run_id = stage_run.id
            document_id = version.document_id
            version_id = version.id
            canonical_document_id = cleaning_run.canonical_document_id
            cleaning_run_id = cleaning_run.id
            assessment_id = assessment.id
            input_uri = cleaning_run.output_artifact_uri
            input_hash = cleaning_run.output_content_hash
            quality_status = QualityDecision(assessment.decision.value)
            quality_summary = {
                "assessment_id": str(assessment.id),
                "decision": assessment.decision.value,
                "overall_score": assessment.overall_score,
                "dimensions": assessment.dimensions_json,
                "issue_codes": [
                    str(issue.get("code")) for issue in assessment.issues_json if issue.get("code")
                ],
                "manually_released": released,
            }

        try:
            document = CanonicalDocument.model_validate(self.storage.read_json(input_uri))
            result = self.chunker.chunk(
                document,
                canonical_document_id=canonical_document_id,
                quality_status=quality_status,
                quality_summary=quality_summary,
            )
            if not result.parents or not result.children:
                raise ChunkingError(
                    "NO_RETRIEVAL_NODES",
                    "Chunking produced no parent/child retrieval nodes",
                    retryable=False,
                )
            artifact = ChunkingArtifact(
                chunking_run_id=run_id,
                document_id=document_id,
                document_version_id=version_id,
                canonical_document_id=canonical_document_id,
                cleaning_run_id=cleaning_run_id,
                quality_assessment_id=assessment_id,
                chunker_name=self.chunker.name,
                chunker_version=self.chunker.version,
                config_version=self.chunker.config.config_version,
                config_snapshot=self.chunker.config_snapshot,
                input_content_hash=input_hash,
                nodes=result.nodes,
            )
            base_path = Path("chunks") / str(document_id) / str(version_id) / str(run_id)
            artifact_uri = self.storage.write_json(
                base_path / "retrieval-nodes.json", artifact.model_dump(mode="json")
            )
            report = ChunkingReport(
                chunking_run_id=run_id,
                document_id=document_id,
                document_version_id=version_id,
                canonical_document_id=canonical_document_id,
                cleaning_run_id=cleaning_run_id,
                quality_assessment_id=assessment_id,
                chunker_name=self.chunker.name,
                chunker_version=self.chunker.version,
                config_version=self.chunker.config.config_version,
                config_snapshot=self.chunker.config_snapshot,
                input_content_hash=input_hash,
                parent_count=len(result.parents),
                child_count=len(result.children),
                total_tokens=sum(node.token_count for node in result.nodes),
                source_block_count=len(
                    {block_id for node in result.nodes for block_id in node.source_block_ids}
                ),
            )
            report_uri = self.storage.write_json(
                base_path / "chunking-report.json", report.model_dump(mode="json")
            )
        except ChunkingError as exc:
            self._record_failure(job_id, run_id, stage_run_id, exc)
            return "failed"
        except Exception as exc:
            self._record_failure(
                job_id,
                run_id,
                stage_run_id,
                ChunkingError("CHUNKING_FAILED", str(exc), retryable=False),
            )
            return "failed"

        with self.session_factory.begin() as db:
            job = db.get(IngestionJob, job_id)
            completed_run = db.get(ChunkingRun, run_id)
            completed_stage = db.get(StageRun, stage_run_id)
            if job is None or completed_run is None or completed_stage is None:
                raise RuntimeError("Chunking state disappeared before completion")
            db.execute(
                delete(RetrievalNode).where(
                    RetrievalNode.document_version_id == version_id,
                    RetrievalNode.chunking_config_version == self.chunker.config.config_version,
                )
            )
            db.add_all([self._record_for_node(run_id, node) for node in result.nodes])
            finished_at = datetime.now(UTC)
            completed_run.status = ChunkingRunStatus.SUCCEEDED
            completed_run.artifact_uri = artifact_uri
            completed_run.report_artifact_uri = report_uri
            completed_run.parent_count = len(result.parents)
            completed_run.child_count = len(result.children)
            completed_run.total_tokens = sum(node.token_count for node in result.nodes)
            completed_run.finished_at = finished_at
            completed_stage.status = StageRunStatus.SUCCEEDED
            completed_stage.output_artifact_uri = artifact_uri
            completed_stage.finished_at = finished_at
            self._advance_to_chunk_evaluation(job)
        return "deferred"

    @staticmethod
    def _manually_released(db: Session, assessment: QualityAssessment) -> bool:
        return (
            db.scalar(
                select(QualityReviewAction.id)
                .where(
                    QualityReviewAction.assessment_id == assessment.id,
                    QualityReviewAction.action == QualityReviewActionValue.RELEASE,
                )
                .limit(1)
            )
            is not None
        )

    @staticmethod
    def _record_for_node(run_id: uuid.UUID, value: RetrievalNodeData) -> RetrievalNode:
        return RetrievalNode(
            id=value.node_id,
            chunking_run_id=run_id,
            document_id=value.document_id,
            document_version_id=value.document_version_id,
            canonical_document_id=value.canonical_document_id,
            node_level=RetrievalNodeLevel(value.node_level.value),
            parent_node_id=value.parent_node_id,
            previous_node_id=value.previous_node_id,
            next_node_id=value.next_node_id,
            title=value.title,
            heading_path=value.heading_path,
            content=value.content,
            retrieval_text=value.retrieval_text,
            source_locators_json=[
                locator.model_dump(mode="json") for locator in value.source_locators
            ],
            source_block_ids=value.source_block_ids,
            content_types=value.content_types,
            language=value.language,
            token_count=value.token_count,
            quality_status=QualityDecisionValue(value.quality_status.value),
            quality_summary_json=value.quality_summary,
            chunker_name=value.chunker_name,
            chunker_version=value.chunker_version,
            chunking_config_version=value.chunking_config_version,
            retrieval_text_hash=value.retrieval_text_hash,
            attributes_json=value.attributes,
            embedding_status=ProjectionStatus.PENDING,
            index_status=ProjectionStatus.PENDING,
        )

    @staticmethod
    def _advance_to_chunk_evaluation(job: IngestionJob) -> None:
        now = datetime.now(UTC)
        job.status = JobStatus.PENDING
        job.current_stage = StageName.CHUNK_EVALUATING
        job.progress_current = max(job.progress_current, 5)
        job.error_code = None
        job.error_message = None
        job.finished_at = None
        job.updated_at = now
        job.document_version.status = VersionStatus.CHUNK_EVALUATING

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
        run_id: uuid.UUID,
        stage_run_id: uuid.UUID,
        error: ChunkingError,
    ) -> None:
        with self.session_factory.begin() as db:
            job = db.get(IngestionJob, job_id)
            run = db.get(ChunkingRun, run_id)
            stage_run = db.get(StageRun, stage_run_id)
            if job is None or run is None or stage_run is None:
                return
            finished_at = datetime.now(UTC)
            error_value: dict[str, object] = {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            }
            run.status = ChunkingRunStatus.FAILED
            run.finished_at = finished_at
            run.error = error_value
            stage_run.status = StageRunStatus.FAILED
            stage_run.finished_at = finished_at
            stage_run.error = error_value
            job.status = JobStatus.FAILED
            job.error_code = error.code
            job.error_message = error.message
            job.finished_at = finished_at
            job.document_version.status = VersionStatus.FAILED

    @staticmethod
    def _fail_without_run(db: Session, job: IngestionJob, error: ChunkingError) -> None:
        finished_at = datetime.now(UTC)
        job.status = JobStatus.FAILED
        job.error_code = error.code
        job.error_message = error.message
        job.finished_at = finished_at
        job.document_version.status = VersionStatus.FAILED
        db.commit()


def build_chunker(settings: Settings) -> StructureAwareChunker:
    return StructureAwareChunker(
        ChunkingConfig(
            config_version=settings.chunking_config_version,
            parent_target_tokens=settings.chunking_parent_target_tokens,
            parent_max_tokens=settings.chunking_parent_max_tokens,
            child_target_tokens=settings.chunking_child_target_tokens,
            child_max_tokens=settings.chunking_child_max_tokens,
            child_overlap_tokens=settings.chunking_child_overlap_tokens,
        )
    )


def get_chunking_service(session_factory: sessionmaker[Session]) -> ChunkingService:
    return ChunkingService(
        session_factory=session_factory,
        storage=get_file_storage(),
        chunker=build_chunker(get_settings()),
    )
