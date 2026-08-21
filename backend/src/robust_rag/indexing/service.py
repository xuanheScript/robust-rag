"""Durable OpenSearch indexing and PostgreSQL-backed rebuild operations."""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache, partial
from typing import NoReturn, Protocol, TypeVar

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.core.settings import Settings, get_settings
from robust_rag.db.enums import (
    DocumentStatus,
    GraphProjectionStatus,
    JobStatus,
    ProjectionRunStatus,
    ProjectionStatus,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import (
    Document,
    DocumentVersion,
    EmbeddingRun,
    IndexingRun,
    IngestionJob,
    RetrievalNode,
    StageRun,
)
from robust_rag.indexing.opensearch import (
    HttpOpenSearchAdapter,
    OpenSearchAdapter,
    OpenSearchAdapterError,
    OpenSearchCapabilities,
    SearchHit,
)

T = TypeVar("T")
logger = structlog.get_logger(__name__)


class GraphLifecycle(Protocol):
    def hide_document(self, document_id: uuid.UUID) -> dict[str, int]: ...

    def restore_version(self, version_id: uuid.UUID) -> dict[str, int]: ...

    def invalidate_version(
        self,
        version_id: uuid.UUID,
        *,
        status: GraphProjectionStatus = GraphProjectionStatus.STALE,
    ) -> int: ...

    def purge_document(self, document_id: uuid.UUID) -> dict[str, int]: ...


class IndexingService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        adapter: OpenSearchAdapter,
        settings: Settings,
        graph_lifecycle: GraphLifecycle | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.session_factory = session_factory
        self.adapter = adapter
        self.settings = settings
        self.graph_lifecycle = graph_lifecycle
        self.sleeper = sleeper
        self.jitter = jitter

    @property
    def config_snapshot(self) -> dict[str, object]:
        return {
            "documents_index": self.settings.opensearch_documents_index,
            "chunks_index": self.settings.opensearch_chunks_index,
            "documents_read_alias": self.settings.opensearch_documents_read_alias,
            "chunks_read_alias": self.settings.opensearch_chunks_read_alias,
            "chunks_write_alias": self.settings.opensearch_chunks_write_alias,
            "bulk_actions": self.settings.opensearch_bulk_actions,
            "mapping": "stage6-strict-icu-faiss-hnsw-cosine-v2",
        }

    def execute(self, job_id: uuid.UUID) -> str:
        prepared = self._prepare(job_id)
        if isinstance(prepared, str):
            return prepared
        (
            run_id,
            stage_id,
            version_id,
            chunking_run_id,
            old_version_id,
            document_projection,
            nodes,
        ) = prepared
        projection_ready = False
        try:
            capabilities = self._retry(self.adapter.capabilities)
            if not capabilities.knn_available or not capabilities.icu_available:
                raise OpenSearchAdapterError(
                    "OPENSEARCH_REQUIRED_PLUGIN_MISSING",
                    "OpenSearch must expose both k-NN and ICU Analysis plugins",
                    retryable=False,
                )
            self._retry(
                lambda: self.adapter.ensure_indexes(
                    self.settings.opensearch_documents_index,
                    self.settings.opensearch_chunks_index,
                    self.settings.voyage_embedding_dimension,
                )
            )
            self._retry(
                lambda: self.adapter.switch_aliases(
                    documents_index=self.settings.opensearch_documents_index,
                    chunks_index=self.settings.opensearch_chunks_index,
                    documents_read_alias=self.settings.opensearch_documents_read_alias,
                    chunks_read_alias=self.settings.opensearch_chunks_read_alias,
                    chunks_write_alias=self.settings.opensearch_chunks_write_alias,
                )
            )
            self._upsert_batches(self.settings.opensearch_chunks_index, nodes)
            expected_documents = 1
            expected_nodes = len(nodes)
            actual_nodes = self.adapter.count_chunking_run(
                self.settings.opensearch_chunks_index,
                str(version_id),
                str(chunking_run_id),
            )
            if actual_nodes != expected_nodes:
                raise OpenSearchAdapterError(
                    "OPENSEARCH_COUNT_MISMATCH",
                    f"Expected {expected_nodes} projections for chunking run "
                    f"{chunking_run_id} but found {actual_nodes}",
                    retryable=True,
                )
            self._upsert_batches(self.settings.opensearch_documents_index, [document_projection])
            actual_documents = self.adapter.count_version(
                self.settings.opensearch_documents_index, str(version_id)
            )
            if actual_documents != expected_documents:
                raise OpenSearchAdapterError(
                    "OPENSEARCH_COUNT_MISMATCH",
                    f"Expected {expected_documents} document projection but found "
                    f"{actual_documents}",
                    retryable=True,
                )
            projection_ready = True
            self._retry(
                lambda: self.adapter.activate_version(
                    self.settings.opensearch_documents_index, str(version_id)
                )
            )
            self._retry(
                lambda: self.adapter.activate_chunking_run(
                    self.settings.opensearch_chunks_index,
                    str(version_id),
                    str(chunking_run_id),
                )
            )
            self._retry(
                lambda: self.adapter.delete_stale_chunking_runs(
                    self.settings.opensearch_chunks_index,
                    str(version_id),
                    str(chunking_run_id),
                )
            )
            actual_nodes = self.adapter.count_version(
                self.settings.opensearch_chunks_index, str(version_id)
            )
            if actual_nodes != expected_nodes:
                raise OpenSearchAdapterError(
                    "OPENSEARCH_COUNT_MISMATCH",
                    f"Expected {expected_nodes} projections after replacing stale chunking "
                    f"runs but found {actual_nodes}",
                    retryable=True,
                )
            if old_version_id is not None and old_version_id != version_id:
                self._delete_version(old_version_id)
                if self.adapter.count_version(
                    self.settings.opensearch_documents_index, str(old_version_id)
                ) or self.adapter.count_version(
                    self.settings.opensearch_chunks_index, str(old_version_id)
                ):
                    raise OpenSearchAdapterError(
                        "OPENSEARCH_OLD_PROJECTION_REMAINS",
                        "The previous document version projection could not be removed",
                        retryable=True,
                    )
        except OpenSearchAdapterError as exc:
            if projection_ready and old_version_id != version_id:
                self._rollback_projection(version_id, old_version_id)
            elif old_version_id == version_id:
                self._restore_document_visibility(version_id)
            self._record_failure(job_id, run_id, stage_id, version_id, chunking_run_id, exc)
            return "failed"
        if old_version_id is not None:
            self._invalidate_graph_version(old_version_id)
        self._record_success(
            job_id,
            run_id,
            stage_id,
            version_id,
            old_version_id,
            capabilities.snapshot(),
            len(nodes),
            chunking_run_id,
        )
        return "succeeded"

    def rebuild_ready(self, document_id: uuid.UUID | None = None) -> dict[str, int]:
        capabilities = self._retry(self.adapter.capabilities)
        if not capabilities.knn_available or not capabilities.icu_available:
            raise OpenSearchAdapterError(
                "OPENSEARCH_REQUIRED_PLUGIN_MISSING",
                "OpenSearch must expose both k-NN and ICU Analysis plugins",
                retryable=False,
            )
        self._retry(
            lambda: self.adapter.ensure_indexes(
                self.settings.opensearch_documents_index,
                self.settings.opensearch_chunks_index,
                self.settings.voyage_embedding_dimension,
            )
        )
        self._retry(
            lambda: self.adapter.switch_aliases(
                documents_index=self.settings.opensearch_documents_index,
                chunks_index=self.settings.opensearch_chunks_index,
                documents_read_alias=self.settings.opensearch_documents_read_alias,
                chunks_read_alias=self.settings.opensearch_chunks_read_alias,
                chunks_write_alias=self.settings.opensearch_chunks_write_alias,
            )
        )
        with self.session_factory() as db:
            statement = (
                select(DocumentVersion)
                .join(Document, Document.current_version_id == DocumentVersion.id)
                .where(DocumentVersion.status == VersionStatus.READY)
                .order_by(DocumentVersion.ready_at)
            )
            if document_id is not None:
                statement = statement.where(Document.id == document_id)
            versions = list(db.scalars(statement))
            document_values: list[dict[str, object]] = []
            node_values: list[dict[str, object]] = []
            chunking_runs: dict[uuid.UUID, tuple[uuid.UUID, int]] = {}
            for version in versions:
                projection = self._latest_embedded_nodes(db, version.id)
                if projection is None:
                    continue
                chunking_run_id, nodes = projection
                document_values.append(self._document_projection(version))
                node_values.extend(self._node_projection(node, version) for node in nodes)
                chunking_runs[version.id] = (chunking_run_id, len(nodes))
        self._upsert_batches(self.settings.opensearch_documents_index, document_values)
        self._upsert_batches(self.settings.opensearch_chunks_index, node_values)
        for version in versions:
            active_projection = chunking_runs.get(version.id)
            if active_projection is None:
                continue
            chunking_run_id, expected_nodes = active_projection
            actual_nodes = self.adapter.count_chunking_run(
                self.settings.opensearch_chunks_index,
                str(version.id),
                str(chunking_run_id),
            )
            if actual_nodes != expected_nodes:
                raise OpenSearchAdapterError(
                    "OPENSEARCH_COUNT_MISMATCH",
                    f"Expected {expected_nodes} projections for chunking run "
                    f"{chunking_run_id} but found {actual_nodes}",
                    retryable=True,
                )
            self.adapter.activate_version(self.settings.opensearch_documents_index, str(version.id))
            self.adapter.activate_chunking_run(
                self.settings.opensearch_chunks_index,
                str(version.id),
                str(chunking_run_id),
            )
            self.adapter.delete_stale_chunking_runs(
                self.settings.opensearch_chunks_index,
                str(version.id),
                str(chunking_run_id),
            )
        return {"documents": len(document_values), "nodes": len(node_values)}

    def delete_document_projection(self, document_id: uuid.UUID) -> dict[str, int]:
        with self.session_factory.begin() as db:
            version_ids = list(
                db.scalars(
                    select(DocumentVersion.id).where(DocumentVersion.document_id == document_id)
                )
            )
            nodes = list(
                db.scalars(select(RetrievalNode).where(RetrievalNode.document_id == document_id))
            )
            for node in nodes:
                node.index_status = ProjectionStatus.STALE
        for version_id in version_ids:
            self._delete_version(version_id)
        return {"versions": len(version_ids), "nodes": len(nodes)}

    def delete_document(self, document_id: uuid.UUID) -> dict[str, object]:
        with self.session_factory() as db:
            if db.get(Document, document_id) is None:
                raise OpenSearchAdapterError(
                    "DOCUMENT_NOT_FOUND", "Document was not found", retryable=False
                )
        result = self.delete_document_projection(document_id)
        graph_result = (
            self.graph_lifecycle.hide_document(document_id)
            if self.graph_lifecycle is not None
            else {"graph_versions": 0, "graph_evidences": 0}
        )
        with self.session_factory.begin() as db:
            document = db.get(Document, document_id)
            if document is None:
                raise OpenSearchAdapterError(
                    "DOCUMENT_NOT_FOUND", "Document was not found", retryable=False
                )
            document.status = DocumentStatus.DELETED
            document.current_version_id = None
            document.deleted_at = datetime.now(UTC)
            document.updated_at = document.deleted_at
        return {
            "document_id": document_id,
            "status": "deleted",
            **result,
            **graph_result,
        }

    def restore_document(self, document_id: uuid.UUID) -> dict[str, object]:
        with self.session_factory.begin() as db:
            document = db.get(Document, document_id)
            if document is None:
                raise OpenSearchAdapterError(
                    "DOCUMENT_NOT_FOUND", "Document was not found", retryable=False
                )
            if document.status is not DocumentStatus.DELETED:
                raise OpenSearchAdapterError(
                    "DOCUMENT_NOT_DELETED",
                    "Only deleted documents can be restored",
                    retryable=False,
                )
            version = db.scalar(
                select(DocumentVersion)
                .where(
                    DocumentVersion.document_id == document_id,
                    DocumentVersion.status == VersionStatus.READY,
                )
                .order_by(DocumentVersion.version_number.desc())
                .limit(1)
            )
            if version is None:
                raise OpenSearchAdapterError(
                    "DOCUMENT_NOT_RESTORABLE",
                    "The document has no ready version to restore",
                    retryable=False,
                )
            version_id = version.id
            document.status = DocumentStatus.ACTIVE
            document.current_version_id = version_id
            document.deleted_at = None
            document.updated_at = datetime.now(UTC)

        try:
            projection = self.rebuild_ready(document_id)
        except OpenSearchAdapterError:
            with self.session_factory.begin() as db:
                document = db.get(Document, document_id)
                if document is not None:
                    document.status = DocumentStatus.DELETED
                    document.current_version_id = None
                    document.deleted_at = datetime.now(UTC)
            raise

        graph_result: dict[str, int] = {
            "graph_entities": 0,
            "graph_facts": 0,
            "graph_evidences": 0,
        }
        graph_warning: str | None = None
        if self.graph_lifecycle is not None:
            try:
                graph_result = self.graph_lifecycle.restore_version(version_id)
            except Exception as exc:
                graph_warning = type(exc).__name__
                with self.session_factory.begin() as db:
                    version = db.get(DocumentVersion, version_id)
                    if version is not None:
                        version.graph_status = GraphProjectionStatus.FAILED
                        version.graph_active = False

        with self.session_factory.begin() as db:
            nodes = list(
                db.scalars(
                    select(RetrievalNode).where(RetrievalNode.document_version_id == version_id)
                )
            )
            for node in nodes:
                node.index_status = ProjectionStatus.SUCCEEDED
        return {
            "document_id": document_id,
            "document_version_id": version_id,
            "status": "active",
            **projection,
            **graph_result,
            "graph_warning": graph_warning,
        }

    def purge_document(self, document_id: uuid.UUID) -> dict[str, int]:
        projection = self.delete_document_projection(document_id)
        graph_result = (
            self.graph_lifecycle.purge_document(document_id)
            if self.graph_lifecycle is not None
            else {"graph_versions": 0, "graph_evidences": 0, "graph_facts": 0}
        )
        return {**projection, **graph_result}

    def switch_aliases(self, documents_index: str, chunks_index: str) -> None:
        self.adapter.switch_aliases(
            documents_index=documents_index,
            chunks_index=chunks_index,
            documents_read_alias=self.settings.opensearch_documents_read_alias,
            chunks_read_alias=self.settings.opensearch_chunks_read_alias,
            chunks_write_alias=self.settings.opensearch_chunks_write_alias,
        )

    def _invalidate_graph_version(self, version_id: uuid.UUID) -> None:
        if self.graph_lifecycle is not None:
            try:
                self.graph_lifecycle.invalidate_version(version_id)
                return
            except Exception as exc:
                logger.exception(
                    "graph_version_invalidation_failed",
                    document_version_id=str(version_id),
                    error_type=type(exc).__name__,
                )
        with self.session_factory.begin() as db:
            version = db.get(DocumentVersion, version_id)
            if version is None:
                return
            had_projection = bool(version.graph_active or version.graph_projected_at is not None)
            version.graph_active = False
            version.graph_status = (
                GraphProjectionStatus.STALE
                if had_projection
                else GraphProjectionStatus.NOT_REQUESTED
            )

    def _prepare(
        self, job_id: uuid.UUID
    ) -> (
        str
        | tuple[
            uuid.UUID,
            uuid.UUID,
            uuid.UUID,
            uuid.UUID,
            uuid.UUID | None,
            dict[str, object],
            list[dict[str, object]],
        ]
    ):
        with self.session_factory.begin() as db:
            job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id).with_for_update())
            if job is None:
                return "not_found"
            embedding_run = db.scalar(
                select(EmbeddingRun)
                .where(
                    EmbeddingRun.document_version_id == job.document_version_id,
                    EmbeddingRun.status == ProjectionRunStatus.SUCCEEDED,
                )
                .order_by(EmbeddingRun.finished_at.desc())
                .limit(1)
            )
            if embedding_run is None:
                self._fail_job(job, "INDEXING_INPUT_MISSING", "No successful embedding run")
                return "failed"
            nodes = list(
                db.scalars(
                    select(RetrievalNode).where(
                        RetrievalNode.chunking_run_id == embedding_run.chunking_run_id
                    )
                )
            )
            if not nodes or any(
                node.embedding_status is not ProjectionStatus.SUCCEEDED
                or node.embedding_vector is None
                or node.embedding_dimension != embedding_run.dimension
                for node in nodes
            ):
                self._fail_job(
                    job,
                    "INDEXING_EMBEDDING_INCOMPLETE",
                    "Every retrieval node must have a compatible embedding",
                )
                return "failed"
            existing = db.scalar(
                select(IndexingRun)
                .where(
                    IndexingRun.embedding_run_id == embedding_run.id,
                    IndexingRun.documents_index == self.settings.opensearch_documents_index,
                    IndexingRun.chunks_index == self.settings.opensearch_chunks_index,
                    IndexingRun.config_version == self.settings.opensearch_index_config_version,
                    IndexingRun.status == ProjectionRunStatus.SUCCEEDED,
                )
                .order_by(IndexingRun.finished_at.desc())
                .limit(1)
            )
            if existing is not None:
                self._mark_ready(
                    job,
                    job.document_version.id,
                    job.document_version.document.current_version_id,
                )
                return "succeeded"
            attempt = (
                int(
                    db.scalar(
                        select(func.count(StageRun.id)).where(
                            StageRun.job_id == job.id, StageRun.stage_name == StageName.INDEXING
                        )
                    )
                    or 0
                )
                + 1
            )
            now = datetime.now(UTC)
            run = IndexingRun(
                document_version_id=job.document_version_id,
                embedding_run_id=embedding_run.id,
                documents_index=self.settings.opensearch_documents_index,
                chunks_index=self.settings.opensearch_chunks_index,
                documents_read_alias=self.settings.opensearch_documents_read_alias,
                chunks_read_alias=self.settings.opensearch_chunks_read_alias,
                chunks_write_alias=self.settings.opensearch_chunks_write_alias,
                config_version=self.settings.opensearch_index_config_version,
                config_snapshot=self.config_snapshot,
                status=ProjectionRunStatus.RUNNING,
                expected_document_count=1,
                expected_node_count=len(nodes),
                started_at=now,
            )
            stage = StageRun(
                job_id=job.id,
                stage_name=StageName.INDEXING,
                implementation_name="opensearch-projection",
                implementation_version="2.0.0",
                config_version=self.settings.opensearch_index_config_version,
                config_snapshot=self.config_snapshot,
                status=StageRunStatus.RUNNING,
                attempt=attempt,
                started_at=now,
            )
            db.add_all([run, stage])
            db.flush()
            job.status = JobStatus.RUNNING
            job.error_code = None
            job.error_message = None
            job.finished_at = None
            job.updated_at = now
            job.document_version.status = VersionStatus.INDEXING
            old_version_id = job.document_version.document.current_version_id
            return (
                run.id,
                stage.id,
                job.document_version.id,
                embedding_run.chunking_run_id,
                old_version_id,
                self._document_projection(job.document_version),
                [self._node_projection(node, job.document_version) for node in nodes],
            )

    def _record_success(
        self,
        job_id: uuid.UUID,
        run_id: uuid.UUID,
        stage_id: uuid.UUID,
        version_id: uuid.UUID,
        old_version_id: uuid.UUID | None,
        capabilities: dict[str, object],
        node_count: int,
        chunking_run_id: uuid.UUID,
    ) -> None:
        with self.session_factory.begin() as db:
            job = db.get(IngestionJob, job_id)
            run = db.get(IndexingRun, run_id)
            stage = db.get(StageRun, stage_id)
            if job is None or run is None or stage is None:
                raise RuntimeError("Indexing state disappeared before completion")
            now = datetime.now(UTC)
            run.status = ProjectionRunStatus.SUCCEEDED
            run.capability_snapshot = capabilities
            run.indexed_document_count = 1
            run.indexed_node_count = node_count
            run.finished_at = now
            stage.status = StageRunStatus.SUCCEEDED
            stage.finished_at = now
            for node in db.scalars(
                select(RetrievalNode).where(RetrievalNode.document_version_id == version_id)
            ):
                node.index_status = (
                    ProjectionStatus.SUCCEEDED
                    if node.chunking_run_id == chunking_run_id
                    else ProjectionStatus.STALE
                )
            self._mark_ready(job, version_id, old_version_id)

    def _record_failure(
        self,
        job_id: uuid.UUID,
        run_id: uuid.UUID,
        stage_id: uuid.UUID,
        version_id: uuid.UUID,
        chunking_run_id: uuid.UUID,
        error: OpenSearchAdapterError,
    ) -> None:
        with self.session_factory.begin() as db:
            now = datetime.now(UTC)
            value: dict[str, object] = {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
                "status_code": error.status_code,
                "failed_ids": error.failed_ids,
            }
            run = db.get(IndexingRun, run_id)
            stage = db.get(StageRun, stage_id)
            job = db.get(IngestionJob, job_id)
            if run is not None:
                run.status = ProjectionRunStatus.FAILED
                run.error = value
                run.finished_at = now
            if stage is not None:
                stage.status = StageRunStatus.FAILED
                stage.error = value
                stage.finished_at = now
            for node in db.scalars(
                select(RetrievalNode).where(
                    RetrievalNode.document_version_id == version_id,
                    RetrievalNode.chunking_run_id == chunking_run_id,
                )
            ):
                node.index_status = ProjectionStatus.FAILED
            if job is not None:
                self._fail_job(job, error.code, error.message)

    def _upsert_batches(self, index: str, documents: list[dict[str, object]]) -> None:
        if not documents:
            return
        size = self.settings.opensearch_bulk_actions
        for start in range(0, len(documents), size):
            batch = documents[start : start + size]
            self._retry(partial(self.adapter.bulk_upsert, index, batch))
        self._retry(partial(self.adapter.refresh, index))

    def _retry(self, operation: Callable[[], T]) -> T:
        for retry_count in range(self.settings.opensearch_max_retries + 1):
            try:
                return operation()
            except OpenSearchAdapterError as exc:
                if not exc.retryable or retry_count >= self.settings.opensearch_max_retries:
                    raise
                delay = min(
                    self.settings.opensearch_retry_max_seconds,
                    self.settings.opensearch_retry_base_seconds
                    * (2**retry_count)
                    * (0.5 + self.jitter()),
                )
                self.sleeper(delay)
        raise AssertionError("retry loop must return or raise")

    def _delete_version(self, version_id: uuid.UUID) -> None:
        self._retry(
            lambda: self.adapter.delete_version(
                self.settings.opensearch_documents_index, str(version_id)
            )
        )
        self._retry(
            lambda: self.adapter.delete_version(
                self.settings.opensearch_chunks_index, str(version_id)
            )
        )

    def _restore_document_visibility(self, version_id: uuid.UUID) -> None:
        """Keep the existing document visible when same-version replacement fails."""

        try:
            self._retry(
                lambda: self.adapter.activate_version(
                    self.settings.opensearch_documents_index, str(version_id)
                )
            )
        except OpenSearchAdapterError:
            return

    def _rollback_projection(self, version_id: uuid.UUID, old_version_id: uuid.UUID | None) -> None:
        """Best-effort restoration if activation or old-version removal fails."""

        try:
            self._delete_version(version_id)
            if old_version_id is None or old_version_id == version_id:
                return
            with self.session_factory() as db:
                old_version = db.get(DocumentVersion, old_version_id)
                if old_version is None:
                    return
                old_projection = self._latest_embedded_nodes(db, old_version_id)
                if old_projection is None:
                    return
                old_chunking_run_id, old_nodes = old_projection
                document_projection = self._document_projection(old_version)
                node_projections = [self._node_projection(node, old_version) for node in old_nodes]
            self._upsert_batches(self.settings.opensearch_documents_index, [document_projection])
            self._upsert_batches(self.settings.opensearch_chunks_index, node_projections)
            self.adapter.activate_version(
                self.settings.opensearch_documents_index, str(old_version_id)
            )
            self.adapter.activate_chunking_run(
                self.settings.opensearch_chunks_index,
                str(old_version_id),
                str(old_chunking_run_id),
            )
            self.adapter.delete_stale_chunking_runs(
                self.settings.opensearch_chunks_index,
                str(old_version_id),
                str(old_chunking_run_id),
            )
        except Exception:
            return

    @staticmethod
    def _latest_embedded_nodes(
        db: Session, version_id: uuid.UUID
    ) -> tuple[uuid.UUID, list[RetrievalNode]] | None:
        embedding_run = db.scalar(
            select(EmbeddingRun)
            .where(
                EmbeddingRun.document_version_id == version_id,
                EmbeddingRun.status == ProjectionRunStatus.SUCCEEDED,
            )
            .order_by(EmbeddingRun.finished_at.desc())
            .limit(1)
        )
        if embedding_run is None:
            return None
        nodes = list(
            db.scalars(
                select(RetrievalNode).where(
                    RetrievalNode.chunking_run_id == embedding_run.chunking_run_id,
                    RetrievalNode.embedding_status == ProjectionStatus.SUCCEEDED,
                )
            )
        )
        return embedding_run.chunking_run_id, nodes

    @staticmethod
    def _document_projection(version: DocumentVersion) -> dict[str, object]:
        canonical = version.canonical_documents[-1] if version.canonical_documents else None
        return {
            "_id": str(version.id),
            "document_id": str(version.document_id),
            "document_version_id": str(version.id),
            "version_number": version.version_number,
            "title": (
                canonical.title if canonical and canonical.title else version.document.display_name
            ),
            "original_filename": version.original_filename,
            "mime_type": version.mime_type,
            "language": canonical.language if canonical else None,
            "status": "ready",
            "is_active": False,
            "document_updated_at": version.document.updated_at.isoformat(),
        }

    @staticmethod
    def _node_projection(node: RetrievalNode, version: DocumentVersion) -> dict[str, object]:
        locators = node.source_locators_json
        quality_score = node.quality_summary_json.get("overall_score")
        issue_codes_value = node.quality_summary_json.get("issue_codes", [])
        issue_codes = issue_codes_value if isinstance(issue_codes_value, list) else []
        return {
            "_id": str(node.id),
            "node_id": str(node.id),
            "document_id": str(node.document_id),
            "document_version_id": str(node.document_version_id),
            "chunking_run_id": str(node.chunking_run_id),
            "parent_node_id": str(node.parent_node_id) if node.parent_node_id else None,
            "previous_node_id": str(node.previous_node_id) if node.previous_node_id else None,
            "next_node_id": str(node.next_node_id) if node.next_node_id else None,
            "node_level": node.node_level.value,
            "title": node.title,
            "heading_path": node.heading_path,
            "content": node.content,
            "retrieval_text": node.retrieval_text,
            "language": node.language,
            "content_types": node.content_types,
            "page_numbers": _unique_ints(locators, "page_number"),
            "slide_numbers": _unique_ints(locators, "slide_number"),
            "sheet_names": _unique_strings(locators, "sheet_name"),
            "cell_ranges": _unique_strings(locators, "cell_range"),
            "quality_status": node.quality_status.value,
            "quality_score": (
                float(quality_score) if isinstance(quality_score, (int, float, str)) else None
            ),
            "quality_flags": [str(value) for value in issue_codes],
            "embedding_model": node.embedding_model,
            "embedding": node.embedding_vector,
            "is_active": False,
            "document_updated_at": version.document.updated_at.isoformat(),
        }

    @staticmethod
    def _mark_ready(
        job: IngestionJob, version_id: uuid.UUID, old_version_id: uuid.UUID | None
    ) -> None:
        now = datetime.now(UTC)
        if old_version_id is not None and old_version_id != version_id:
            for version in job.document_version.document.versions:
                if version.id == old_version_id:
                    version.status = VersionStatus.SUPERSEDED
                    version.superseded_at = now
        job.document_version.document.current_version_id = version_id
        job.document_version.status = VersionStatus.READY
        job.document_version.ready_at = now
        job.status = JobStatus.SUCCEEDED
        job.current_stage = StageName.INDEXING
        job.progress_current = job.progress_total
        job.error_code = None
        job.error_message = None
        job.finished_at = now
        job.updated_at = now

    @staticmethod
    def _fail_job(job: IngestionJob, code: str, message: str) -> None:
        now = datetime.now(UTC)
        job.status = JobStatus.FAILED
        job.error_code = code
        job.error_message = message
        job.finished_at = now
        job.updated_at = now
        job.document_version.status = VersionStatus.FAILED


def _unique_ints(values: list[dict[str, object]], key: str) -> list[int]:
    result: set[int] = set()
    for value in values:
        candidate = value.get(key)
        if isinstance(candidate, (int, str)):
            result.add(int(candidate))
    return sorted(result)


def _unique_strings(values: list[dict[str, object]], key: str) -> list[str]:
    return sorted({str(value[key]) for value in values if value.get(key)})


@lru_cache(maxsize=1)
def get_opensearch_adapter() -> OpenSearchAdapter:
    settings = get_settings()
    if not settings.opensearch_url:
        return UnavailableOpenSearchAdapter()
    verify: bool | str = settings.opensearch_verify_tls
    if settings.opensearch_ca_cert is not None:
        verify = str(settings.opensearch_ca_cert)
    return HttpOpenSearchAdapter(
        base_url=settings.opensearch_url,
        username=settings.opensearch_username,
        password=(
            settings.opensearch_password.get_secret_value()
            if settings.opensearch_password is not None
            else None
        ),
        verify=verify,
        timeout_seconds=settings.opensearch_timeout_seconds,
    )


class UnavailableOpenSearchAdapter:
    def _raise(self) -> NoReturn:
        raise OpenSearchAdapterError(
            "OPENSEARCH_URL_MISSING",
            "OPENSEARCH_URL is required for indexing",
            retryable=False,
        )

    def capabilities(self) -> OpenSearchCapabilities:
        self._raise()

    def ensure_indexes(self, documents_index: str, chunks_index: str, dimension: int) -> None:
        del documents_index, chunks_index, dimension
        self._raise()

    def switch_aliases(
        self,
        *,
        documents_index: str,
        chunks_index: str,
        documents_read_alias: str,
        chunks_read_alias: str,
        chunks_write_alias: str,
    ) -> None:
        del (
            documents_index,
            chunks_index,
            documents_read_alias,
            chunks_read_alias,
            chunks_write_alias,
        )
        self._raise()

    def bulk_upsert(self, index: str, documents: list[dict[str, object]]) -> None:
        del index, documents
        self._raise()

    def refresh(self, index: str) -> None:
        del index
        self._raise()

    def count_version(self, index: str, document_version_id: str) -> int:
        del index, document_version_id
        self._raise()

    def count_chunking_run(self, index: str, document_version_id: str, chunking_run_id: str) -> int:
        del index, document_version_id, chunking_run_id
        self._raise()

    def activate_version(self, index: str, document_version_id: str) -> None:
        del index, document_version_id
        self._raise()

    def activate_chunking_run(
        self, index: str, document_version_id: str, chunking_run_id: str
    ) -> None:
        del index, document_version_id, chunking_run_id
        self._raise()

    def delete_stale_chunking_runs(
        self, index: str, document_version_id: str, chunking_run_id: str
    ) -> None:
        del index, document_version_id, chunking_run_id
        self._raise()

    def delete_version(self, index: str, document_version_id: str) -> None:
        del index, document_version_id
        self._raise()

    def index_exists(self, index: str) -> bool:
        del index
        self._raise()

    def delete_index(self, index: str) -> None:
        del index
        self._raise()

    def search_bm25(self, alias: str, query: str, size: int = 10) -> list[str]:
        del alias, query, size
        self._raise()

    def search_dense(self, alias: str, vector: list[float], size: int = 10) -> list[str]:
        del alias, vector, size
        self._raise()

    def search_bm25_hits(self, alias: str, query: str, size: int = 10) -> list[SearchHit]:
        del alias, query, size
        self._raise()

    def search_dense_hits(self, alias: str, vector: list[float], size: int = 10) -> list[SearchHit]:
        del alias, vector, size
        self._raise()


def get_indexing_service(session_factory: sessionmaker[Session]) -> IndexingService:
    from robust_rag.graph.factory import get_graph_lifecycle_service

    return IndexingService(
        session_factory=session_factory,
        adapter=get_opensearch_adapter(),
        settings=get_settings(),
        graph_lifecycle=get_graph_lifecycle_service(),
    )
