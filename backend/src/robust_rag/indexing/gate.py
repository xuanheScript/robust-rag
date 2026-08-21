"""Deterministic retrieval-node gate between chunking and external projections."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.db.enums import (
    ChunkingRunStatus,
    JobStatus,
    QualityDecisionValue,
    RetrievalNodeLevel,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import ChunkingRun, IngestionJob, RetrievalNode, StageRun

NODE_GATE_VERSION = "stage6-retrieval-node-gate-v2"

_TABLE_KINDS_WITHOUT_COLUMN_HEADERS = {
    "key_value",
    "sectioned_key_value",
    "complex",
}


class RetrievalNodeGateService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def execute(self, job_id: uuid.UUID) -> str:
        with self.session_factory.begin() as db:
            job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id).with_for_update())
            if job is None:
                return "not_found"
            existing = db.scalar(
                select(StageRun)
                .where(
                    StageRun.job_id == job.id,
                    StageRun.stage_name == StageName.CHUNK_EVALUATING,
                    StageRun.config_version == NODE_GATE_VERSION,
                    StageRun.status == StageRunStatus.SUCCEEDED,
                )
                .order_by(StageRun.finished_at.desc())
                .limit(1)
            )
            if existing is not None:
                self._advance(job)
                return "deferred"

            chunking_run = db.scalar(
                select(ChunkingRun)
                .where(
                    ChunkingRun.document_version_id == job.document_version_id,
                    ChunkingRun.status == ChunkingRunStatus.SUCCEEDED,
                )
                .order_by(ChunkingRun.finished_at.desc())
                .limit(1)
            )
            nodes = (
                list(
                    db.scalars(
                        select(RetrievalNode).where(
                            RetrievalNode.chunking_run_id == chunking_run.id
                        )
                    )
                )
                if chunking_run is not None
                else []
            )
            issues = self._issues(nodes)
            attempt = (
                int(
                    db.scalar(
                        select(func.count(StageRun.id)).where(
                            StageRun.job_id == job.id,
                            StageRun.stage_name == StageName.CHUNK_EVALUATING,
                        )
                    )
                    or 0
                )
                + 1
            )
            now = datetime.now(UTC)
            stage = StageRun(
                job_id=job.id,
                stage_name=StageName.CHUNK_EVALUATING,
                implementation_name="retrieval-node-quality-gate",
                implementation_version="2.0.0",
                config_version=NODE_GATE_VERSION,
                config_snapshot={
                    "checks": [
                        "parent_link",
                        "non_empty_content",
                        "source_traceability",
                        "retrieval_text_hash",
                        "quality_decision",
                        "table_shape_semantics",
                    ]
                },
                status=StageRunStatus.SUCCEEDED,
                attempt=attempt,
                input_artifact_uri=chunking_run.artifact_uri if chunking_run else None,
                started_at=now,
                finished_at=now,
                error={"issues": issues} if issues else None,
            )
            db.add(stage)
            if issues:
                job.status = JobStatus.QUARANTINED
                job.error_code = "RETRIEVAL_NODE_GATE_FAILED"
                job.error_message = f"Retrieval node gate found {len(issues)} issue(s)"
                job.finished_at = now
                job.updated_at = now
                job.document_version.status = VersionStatus.QUARANTINED
                return "quarantined"
            self._advance(job)
            return "deferred"

    @staticmethod
    def _issues(nodes: list[RetrievalNode]) -> list[dict[str, str]]:
        issues: list[dict[str, str]] = []
        parents = {node.id for node in nodes if node.node_level is RetrievalNodeLevel.PARENT}
        if not parents or not any(node.node_level is RetrievalNodeLevel.CHILD for node in nodes):
            issues.append({"code": "NODE_SET_INCOMPLETE", "node_id": ""})
        for node in nodes:
            code: str | None = None
            if not node.content.strip() or not node.retrieval_text.strip():
                code = "NODE_CONTENT_EMPTY"
            elif not node.source_block_ids or not node.source_locators_json:
                code = "NODE_SOURCE_MISSING"
            elif len(node.retrieval_text_hash) != 64:
                code = "NODE_HASH_INVALID"
            elif node.quality_status in {
                QualityDecisionValue.QUARANTINED,
                QualityDecisionValue.REJECTED,
            } and not node.quality_summary_json.get("manually_released"):
                code = "NODE_QUALITY_BLOCKED"
            elif node.node_level is RetrievalNodeLevel.CHILD and node.parent_node_id not in parents:
                code = "NODE_PARENT_MISSING"
            elif _table_header_missing(node.attributes_json):
                code = "TABLE_HEADER_MISSING"
            if code:
                issues.append({"code": code, "node_id": str(node.id)})
        return issues

    @staticmethod
    def _advance(job: IngestionJob) -> None:
        now = datetime.now(UTC)
        job.status = JobStatus.PENDING
        job.current_stage = StageName.EMBEDDING
        job.progress_current = max(job.progress_current, 6)
        job.error_code = None
        job.error_message = None
        job.finished_at = None
        job.updated_at = now
        job.document_version.status = VersionStatus.EMBEDDING


def _table_header_missing(attributes: dict[str, object]) -> bool:
    """Require column headers only for table shapes that retrieve by records or rows."""

    if attributes.get("table") is not True or attributes.get("table_header"):
        return False
    raw_kind = attributes.get("table_kind")
    if not isinstance(raw_kind, str):
        profile = attributes.get("table_profile")
        raw_kind = profile.get("kind") if isinstance(profile, dict) else None
    return raw_kind not in _TABLE_KINDS_WITHOUT_COLUMN_HEADERS
