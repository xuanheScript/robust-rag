"""Durable, idempotent Voyage batch embedding orchestration."""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.core.settings import Settings, get_settings
from robust_rag.db.enums import (
    ChunkingRunStatus,
    EmbeddingBatchStatus,
    JobStatus,
    ProjectionRunStatus,
    ProjectionStatus,
    StageName,
    StageRunStatus,
    VersionStatus,
)
from robust_rag.db.models import (
    ChunkingRun,
    EmbeddingBatch,
    EmbeddingRun,
    IngestionJob,
    RetrievalNode,
    StageRun,
)
from robust_rag.indexing.embedding import (
    EmbeddingAdapter,
    EmbeddingAdapterError,
    EmbeddingResponse,
    VoyageEmbeddingAdapter,
)


class EmbeddingService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        adapter: EmbeddingAdapter,
        config_version: str,
        batch_items: int,
        batch_tokens: int,
        max_retries: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
        price_per_million_tokens: float | None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.session_factory = session_factory
        self.adapter = adapter
        self.config_version = config_version
        self.batch_items = batch_items
        self.batch_tokens = batch_tokens
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.price_per_million_tokens = price_per_million_tokens
        self.sleeper = sleeper
        self.jitter = jitter

    @property
    def config_snapshot(self) -> dict[str, object]:
        return {
            "provider": self.adapter.provider,
            "model": self.adapter.model,
            "dimension": self.adapter.dimension,
            "batch_items": self.batch_items,
            "batch_tokens": self.batch_tokens,
            "max_retries": self.max_retries,
            "retry_base_seconds": self.retry_base_seconds,
            "retry_max_seconds": self.retry_max_seconds,
            "price_per_million_tokens": self.price_per_million_tokens,
            "input_type": "document",
        }

    def execute(self, job_id: uuid.UUID) -> str:
        prepared = self._prepare(job_id)
        if isinstance(prepared, str):
            return prepared
        run_id, stage_id, batches = prepared
        total_provider_tokens = 0
        has_provider_usage = False
        for batch_id, node_ids, texts in batches:
            try:
                response, retry_count = self._embed_with_retry(texts, batch_id)
            except EmbeddingAdapterError as exc:
                self._record_failure(job_id, run_id, stage_id, batch_id, node_ids, exc)
                return "failed"
            total_provider_tokens += response.total_tokens or 0
            has_provider_usage = has_provider_usage or response.total_tokens is not None
            self._record_batch_success(
                batch_id=batch_id,
                node_ids=node_ids,
                response=response,
                retry_count=retry_count,
            )
        self._record_success(
            job_id,
            run_id,
            stage_id,
            total_provider_tokens if has_provider_usage else None,
        )
        return "deferred"

    def _prepare(
        self, job_id: uuid.UUID
    ) -> str | tuple[uuid.UUID, uuid.UUID, list[tuple[uuid.UUID, list[uuid.UUID], list[str]]]]:
        with self.session_factory.begin() as db:
            job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id).with_for_update())
            if job is None:
                return "not_found"
            chunking_run = db.scalar(
                select(ChunkingRun)
                .where(
                    ChunkingRun.document_version_id == job.document_version_id,
                    ChunkingRun.status == ChunkingRunStatus.SUCCEEDED,
                )
                .order_by(ChunkingRun.finished_at.desc())
                .limit(1)
            )
            if chunking_run is None:
                self._fail_job(job, "EMBEDDING_INPUT_MISSING", "No successful chunking run")
                return "failed"
            nodes = list(
                db.scalars(
                    select(RetrievalNode)
                    .where(RetrievalNode.chunking_run_id == chunking_run.id)
                    .order_by(RetrievalNode.created_at, RetrievalNode.id)
                )
            )
            if not nodes:
                self._fail_job(job, "EMBEDDING_INPUT_MISSING", "No retrieval nodes to embed")
                return "failed"
            existing = db.scalar(
                select(EmbeddingRun)
                .where(
                    EmbeddingRun.chunking_run_id == chunking_run.id,
                    EmbeddingRun.provider == self.adapter.provider,
                    EmbeddingRun.model == self.adapter.model,
                    EmbeddingRun.dimension == self.adapter.dimension,
                    EmbeddingRun.config_version == self.config_version,
                    EmbeddingRun.status == ProjectionRunStatus.SUCCEEDED,
                )
                .order_by(EmbeddingRun.finished_at.desc())
                .limit(1)
            )
            if existing is not None and all(self._node_is_current(node) for node in nodes):
                self._advance(job)
                return "deferred"

            pending = [node for node in nodes if not self._node_is_current(node)]
            attempt = (
                int(
                    db.scalar(
                        select(func.count(StageRun.id)).where(
                            StageRun.job_id == job.id, StageRun.stage_name == StageName.EMBEDDING
                        )
                    )
                    or 0
                )
                + 1
            )
            now = datetime.now(UTC)
            try:
                grouped = self._batches(pending)
            except EmbeddingAdapterError as exc:
                value = self._error_value(exc)
                db.add(
                    StageRun(
                        job_id=job.id,
                        stage_name=StageName.EMBEDDING,
                        implementation_name=f"{self.adapter.provider}-embeddings",
                        implementation_version=self.adapter.model,
                        config_version=self.config_version,
                        config_snapshot=self.config_snapshot,
                        status=StageRunStatus.FAILED,
                        attempt=attempt,
                        input_artifact_uri=chunking_run.artifact_uri,
                        started_at=now,
                        finished_at=now,
                        error=value,
                    )
                )
                self._fail_job(job, exc.code, exc.message)
                return "failed"
            run = EmbeddingRun(
                document_version_id=job.document_version_id,
                chunking_run_id=chunking_run.id,
                provider=self.adapter.provider,
                model=self.adapter.model,
                dimension=self.adapter.dimension,
                config_version=self.config_version,
                config_snapshot=self.config_snapshot,
                status=ProjectionRunStatus.RUNNING,
                input_count=len(pending),
                batch_count=len(grouped),
                estimated_tokens=sum(self._estimated_tokens(node) for node in pending),
                started_at=now,
            )
            stage = StageRun(
                job_id=job.id,
                stage_name=StageName.EMBEDDING,
                implementation_name=f"{self.adapter.provider}-embeddings",
                implementation_version=self.adapter.model,
                config_version=self.config_version,
                config_snapshot=self.config_snapshot,
                status=StageRunStatus.RUNNING,
                attempt=attempt,
                input_artifact_uri=chunking_run.artifact_uri,
                started_at=now,
            )
            db.add_all([run, stage])
            db.flush()
            prepared: list[tuple[uuid.UUID, list[uuid.UUID], list[str]]] = []
            for index, group in enumerate(grouped):
                batch = EmbeddingBatch(
                    embedding_run_id=run.id,
                    batch_index=index,
                    node_ids_json=[str(node.id) for node in group],
                    input_count=len(group),
                    estimated_tokens=sum(self._estimated_tokens(node) for node in group),
                    status=EmbeddingBatchStatus.PENDING,
                )
                db.add(batch)
                db.flush()
                prepared.append(
                    (batch.id, [node.id for node in group], [node.retrieval_text for node in group])
                )
            job.status = JobStatus.RUNNING
            job.error_code = None
            job.error_message = None
            job.finished_at = None
            job.updated_at = now
            job.document_version.status = VersionStatus.EMBEDDING
            return run.id, stage.id, prepared

    def _embed_with_retry(
        self, texts: list[str], batch_id: uuid.UUID
    ) -> tuple[EmbeddingResponse, int]:
        with self.session_factory.begin() as db:
            batch = db.get(EmbeddingBatch, batch_id)
            if batch is not None:
                batch.status = EmbeddingBatchStatus.RUNNING
                batch.started_at = datetime.now(UTC)
        for retry_count in range(self.max_retries + 1):
            try:
                return self.adapter.embed(texts, input_type="document"), retry_count
            except EmbeddingAdapterError as exc:
                with self.session_factory.begin() as db:
                    batch = db.get(EmbeddingBatch, batch_id)
                    if batch is not None:
                        batch.retry_count = retry_count
                        batch.error = self._error_value(exc)
                if not exc.retryable or retry_count >= self.max_retries:
                    raise
                delay = min(
                    self.retry_max_seconds,
                    self.retry_base_seconds * (2**retry_count) * (0.5 + self.jitter()),
                )
                self.sleeper(delay)
        raise AssertionError("retry loop must return or raise")

    def _record_batch_success(
        self,
        *,
        batch_id: uuid.UUID,
        node_ids: list[uuid.UUID],
        response: EmbeddingResponse,
        retry_count: int,
    ) -> None:
        with self.session_factory.begin() as db:
            batch = db.get(EmbeddingBatch, batch_id)
            nodes = list(db.scalars(select(RetrievalNode).where(RetrievalNode.id.in_(node_ids))))
            by_id = {node.id: node for node in nodes}
            now = datetime.now(UTC)
            for node_id, vector in zip(node_ids, response.vectors, strict=True):
                node = by_id[node_id]
                node.embedding_provider = self.adapter.provider
                node.embedding_model = self.adapter.model
                node.embedding_dimension = self.adapter.dimension
                node.embedding_config_version = self.config_version
                node.embedding_vector = vector
                node.embedding_status = ProjectionStatus.SUCCEEDED
                node.embedded_at = now
            if batch is not None:
                batch.status = EmbeddingBatchStatus.SUCCEEDED
                batch.provider_tokens = response.total_tokens
                batch.retry_count = retry_count
                batch.error = None
                batch.finished_at = now

    def _record_success(
        self,
        job_id: uuid.UUID,
        run_id: uuid.UUID,
        stage_id: uuid.UUID,
        provider_tokens: int | None,
    ) -> None:
        with self.session_factory.begin() as db:
            job = db.get(IngestionJob, job_id)
            run = db.get(EmbeddingRun, run_id)
            stage = db.get(StageRun, stage_id)
            if job is None or run is None or stage is None:
                raise RuntimeError("Embedding state disappeared before completion")
            now = datetime.now(UTC)
            run.status = ProjectionRunStatus.SUCCEEDED
            run.provider_tokens = provider_tokens
            if provider_tokens is not None and self.price_per_million_tokens is not None:
                run.estimated_cost_usd = provider_tokens * self.price_per_million_tokens / 1_000_000
            run.finished_at = now
            stage.status = StageRunStatus.SUCCEEDED
            stage.finished_at = now
            self._advance(job)

    def _record_failure(
        self,
        job_id: uuid.UUID,
        run_id: uuid.UUID,
        stage_id: uuid.UUID,
        batch_id: uuid.UUID,
        node_ids: list[uuid.UUID],
        error: EmbeddingAdapterError,
    ) -> None:
        with self.session_factory.begin() as db:
            now = datetime.now(UTC)
            value = self._error_value(error)
            run = db.get(EmbeddingRun, run_id)
            stage = db.get(StageRun, stage_id)
            batch = db.get(EmbeddingBatch, batch_id)
            job = db.get(IngestionJob, job_id)
            if run is not None:
                run.status = ProjectionRunStatus.FAILED
                run.error = value
                run.finished_at = now
            if stage is not None:
                stage.status = StageRunStatus.FAILED
                stage.error = value
                stage.finished_at = now
            if batch is not None:
                batch.status = EmbeddingBatchStatus.FAILED
                batch.error = value
                batch.finished_at = now
            for node in db.scalars(select(RetrievalNode).where(RetrievalNode.id.in_(node_ids))):
                node.embedding_status = ProjectionStatus.FAILED
            if job is not None:
                self._fail_job(job, error.code, error.message)

    def _batches(self, nodes: list[RetrievalNode]) -> list[list[RetrievalNode]]:
        batches: list[list[RetrievalNode]] = []
        current: list[RetrievalNode] = []
        token_count = 0
        for node in nodes:
            estimated = self._estimated_tokens(node)
            if estimated > self.batch_tokens:
                raise EmbeddingAdapterError(
                    "VOYAGE_INPUT_TOO_LARGE",
                    f"Retrieval node {node.id} exceeds the configured batch token limit",
                    retryable=False,
                )
            if current and (
                len(current) >= self.batch_items or token_count + estimated > self.batch_tokens
            ):
                batches.append(current)
                current = []
                token_count = 0
            current.append(node)
            token_count += estimated
        if current:
            batches.append(current)
        return batches

    def _node_is_current(self, node: RetrievalNode) -> bool:
        return (
            node.embedding_status is ProjectionStatus.SUCCEEDED
            and node.embedding_provider == self.adapter.provider
            and node.embedding_model == self.adapter.model
            and node.embedding_dimension == self.adapter.dimension
            and node.embedding_config_version == self.config_version
            and node.embedding_vector is not None
            and len(node.embedding_vector) == self.adapter.dimension
        )

    @staticmethod
    def _estimated_tokens(node: RetrievalNode) -> int:
        return max(node.token_count, (len(node.retrieval_text) + 3) // 4, 1)

    @staticmethod
    def _error_value(error: EmbeddingAdapterError) -> dict[str, object]:
        return {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "status_code": error.status_code,
        }

    @staticmethod
    def _advance(job: IngestionJob) -> None:
        now = datetime.now(UTC)
        job.status = JobStatus.PENDING
        job.current_stage = StageName.INDEXING
        job.progress_current = max(job.progress_current, 7)
        job.error_code = None
        job.error_message = None
        job.finished_at = None
        job.updated_at = now
        job.document_version.status = VersionStatus.INDEXING

    @staticmethod
    def _fail_job(job: IngestionJob, code: str, message: str) -> None:
        now = datetime.now(UTC)
        job.status = JobStatus.FAILED
        job.error_code = code
        job.error_message = message
        job.finished_at = now
        job.updated_at = now
        job.document_version.status = VersionStatus.FAILED


class UnavailableEmbeddingAdapter:
    provider = "voyage"

    def __init__(self, model: str, dimension: int) -> None:
        self.model = model
        self.dimension = dimension

    def embed(self, texts: list[str], *, input_type: str) -> EmbeddingResponse:
        del texts, input_type
        raise EmbeddingAdapterError(
            "VOYAGE_API_KEY_MISSING",
            "VOYAGE_API_KEY is required for the embedding stage",
            retryable=False,
        )


def build_embedding_adapter(settings: Settings) -> EmbeddingAdapter:
    if settings.voyage_api_key is None:
        return UnavailableEmbeddingAdapter(
            settings.voyage_embedding_model, settings.voyage_embedding_dimension
        )
    return VoyageEmbeddingAdapter(
        api_key=settings.voyage_api_key.get_secret_value(),
        model=settings.voyage_embedding_model,
        dimension=settings.voyage_embedding_dimension,
        base_url=settings.voyage_base_url,
        timeout_seconds=settings.voyage_timeout_seconds,
    )


def get_embedding_service(session_factory: sessionmaker[Session]) -> EmbeddingService:
    settings = get_settings()
    return EmbeddingService(
        session_factory=session_factory,
        adapter=build_embedding_adapter(settings),
        config_version=settings.voyage_embedding_config_version,
        batch_items=settings.voyage_embedding_batch_items,
        batch_tokens=settings.voyage_embedding_batch_tokens,
        max_retries=settings.voyage_embedding_max_retries,
        retry_base_seconds=settings.voyage_embedding_retry_base_seconds,
        retry_max_seconds=settings.voyage_embedding_retry_max_seconds,
        price_per_million_tokens=settings.voyage_embedding_price_per_million_tokens,
    )
