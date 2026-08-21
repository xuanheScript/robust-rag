import json
import uuid
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.api.routes.search_admin import get_search_admin_service
from robust_rag.chunking.service import ChunkingService
from robust_rag.core.settings import Settings
from robust_rag.db.enums import (
    DocumentStatus,
    EmbeddingBatchStatus,
    JobStatus,
    ProjectionRunStatus,
    ProjectionStatus,
    StageName,
    VersionStatus,
)
from robust_rag.db.models import Document, EmbeddingRun, IndexingRun, IngestionJob, RetrievalNode
from robust_rag.indexing.embedding import (
    EmbeddingAdapterError,
    EmbeddingResponse,
    VoyageEmbeddingAdapter,
)
from robust_rag.indexing.embedding_service import EmbeddingService, chunk_embedding_text
from robust_rag.indexing.gate import RetrievalNodeGateService, _table_header_missing
from robust_rag.indexing.opensearch import MemoryOpenSearchAdapter
from robust_rag.indexing.rate_limit import VoyageRateLimiter
from robust_rag.indexing.service import IndexingService
from robust_rag.quality.service import QualityService
from robust_rag.storage.local import LocalFileStorage
from tests.test_chunking import build_chunking_service
from tests.test_quality_service import build_quality_service, prepare_cleaned_job


class FakeEmbeddingAdapter:
    provider = "voyage"
    model = "voyage-4"
    dimension = 4

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str], *, input_type: str) -> EmbeddingResponse:
        assert input_type == "document"
        self.calls.append(texts)
        if self.failures:
            self.failures -= 1
            raise EmbeddingAdapterError(
                "VOYAGE_RATE_LIMIT", "try later", retryable=True, status_code=429
            )
        vectors = [
            [float(index + 1), float(len(text) % 7 + 1), 1.0, 0.5]
            for index, text in enumerate(texts)
        ]
        return EmbeddingResponse(vectors=vectors, total_tokens=len(texts) * 7)


class ScriptedRateLimiter:
    def __init__(self, waits: list[float]) -> None:
        self.waits = waits
        self.reservations: list[int] = []

    def reserve(self, estimated_tokens: int) -> float:
        self.reservations.append(estimated_tokens)
        return self.waits.pop(0) if self.waits else 0


@pytest.mark.parametrize("table_kind", ["key_value", "sectioned_key_value", "complex"])
def test_node_gate_accepts_table_shapes_without_column_headers(table_kind: str) -> None:
    assert not _table_header_missing({"table": True, "table_header": [], "table_kind": table_kind})
    assert not _table_header_missing(
        {
            "table": True,
            "table_header": [],
            "table_profile": {"kind": table_kind},
        }
    )


@pytest.mark.parametrize("table_kind", ["record_table", "matrix", None])
def test_node_gate_still_requires_headers_for_row_or_unknown_tables(
    table_kind: str | None,
) -> None:
    attributes: dict[str, object] = {"table": True, "table_header": []}
    if table_kind is not None:
        attributes["table_kind"] = table_kind
    assert _table_header_missing(attributes)


def _prepare_chunked_job(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    document_id, version_id, job_id = prepare_cleaned_job(session_factory, storage)
    quality: QualityService = build_quality_service(session_factory, storage)
    chunking: ChunkingService = build_chunking_service(session_factory, storage)
    assert quality.execute(job_id) == "deferred"
    assert chunking.execute(job_id) == "deferred"
    assert RetrievalNodeGateService(session_factory).execute(job_id) == "deferred"
    return document_id, version_id, job_id


def _embedding_service(
    session_factory: sessionmaker[Session],
    adapter: FakeEmbeddingAdapter,
    *,
    rate_limiter: VoyageRateLimiter | None = None,
    batch_items: int = 2,
) -> EmbeddingService:
    return EmbeddingService(
        session_factory=session_factory,
        adapter=adapter,
        config_version="test-voyage-v1",
        batch_items=batch_items,
        batch_tokens=10000,
        max_retries=2,
        retry_base_seconds=0,
        retry_max_seconds=0,
        price_per_million_tokens=0.12,
        rate_limiter=rate_limiter,
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    )


def _stage6_settings() -> Settings:
    return Settings(_env_file=None).model_copy(
        update={
            "voyage_embedding_dimension": 4,
            "voyage_embedding_config_version": "test-voyage-v1",
            "opensearch_bulk_actions": 2,
            "opensearch_max_retries": 1,
            "opensearch_retry_base_seconds": 0,
            "opensearch_retry_max_seconds": 0,
        }
    )


def test_voyage_adapter_contract_orders_vectors_and_rejects_dimensions() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [2.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ],
                "usage": {"total_tokens": 9},
            },
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.voyageai.com/v1"
    )
    adapter = VoyageEmbeddingAdapter(api_key="secret", model="voyage-4", dimension=2, client=client)

    result = adapter.embed(["first", "second"], input_type="document")

    assert result.vectors == [[1.0, 0.0], [2.0, 0.0]]
    assert result.total_tokens == 9
    assert len(requests) == 1
    assert requests[0]["model"] == "voyage-4"
    assert requests[0]["input_type"] == "document"
    assert requests[0]["output_dimension"] == 2


def test_voyage_adapter_preserves_retry_after_for_rate_limit_deferral() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                429,
                headers={"Retry-After": "37"},
                json={"detail": "rate limited"},
            )
        ),
        base_url="https://api.voyageai.com/v1",
    )
    adapter = VoyageEmbeddingAdapter(api_key="secret", model="voyage-4", dimension=2, client=client)

    with pytest.raises(EmbeddingAdapterError) as raised:
        adapter.embed(["first"], input_type="document")

    assert raised.value.status_code == 429
    assert raised.value.retry_after_seconds == 37


def test_embedding_batches_retry_audit_cost_and_idempotency(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    document_id, version_id, job_id = _prepare_chunked_job(session_factory, storage)
    adapter = FakeEmbeddingAdapter(failures=1)
    service = _embedding_service(session_factory, adapter)

    assert service.execute(job_id) == "rate_limited"
    assert service.retry_after_seconds == 65
    with session_factory() as db:
        waiting_job = db.get(IngestionJob, job_id)
        waiting_run = db.scalar(
            select(EmbeddingRun).where(EmbeddingRun.document_version_id == version_id)
        )
        assert waiting_job is not None and waiting_job.status is JobStatus.PENDING
        assert waiting_run is not None and waiting_run.status is ProjectionRunStatus.RUNNING

    assert service.execute(job_id) == "deferred"
    calls_after_success = len(adapter.calls)
    assert service.execute(job_id) == "deferred"
    assert len(adapter.calls) == calls_after_success

    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        run = db.scalar(select(EmbeddingRun).where(EmbeddingRun.document_version_id == version_id))
        nodes = list(
            db.scalars(select(RetrievalNode).where(RetrievalNode.document_version_id == version_id))
        )
        assert job is not None and job.current_stage is StageName.INDEXING
        assert run is not None and run.status is ProjectionRunStatus.SUCCEEDED
        assert run.batch_count >= 1
        assert run.provider_tokens == sum(batch.provider_tokens or 0 for batch in run.batches)
        assert run.estimated_cost_usd == (run.provider_tokens or 0) * 0.12 / 1_000_000
        assert any(batch.retry_count == 1 for batch in run.batches)
        assert all(batch.status is EmbeddingBatchStatus.SUCCEEDED for batch in run.batches)
        assert all(node.embedding_status is ProjectionStatus.SUCCEEDED for node in nodes)
        assert all(node.embedding_model == "voyage-4" for node in nodes)
        assert all(node.embedding_vector and len(node.embedding_vector) == 4 for node in nodes)

    response = client.get(f"/api/v1/documents/{document_id}/versions/{version_id}/embedding-runs")
    assert response.status_code == 200
    assert response.json()[0]["batches"]


def test_chunk_embedding_text_excludes_document_title_and_source_metadata(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _, version_id, job_id = _prepare_chunked_job(session_factory, storage)
    adapter = FakeEmbeddingAdapter()
    service = _embedding_service(session_factory, adapter)

    assert service.execute(job_id) == "deferred"
    with session_factory() as db:
        nodes = list(
            db.scalars(select(RetrievalNode).where(RetrievalNode.document_version_id == version_id))
        )

    submitted = [text for batch in adapter.calls for text in batch]
    expected = [chunk_embedding_text(node) for node in nodes]
    assert sorted(submitted) == sorted(expected)
    assert all(node.content.strip() in chunk_embedding_text(node) for node in nodes)
    for node in nodes:
        if node.title and node.title not in node.content:
            assert node.title not in chunk_embedding_text(node)
    assert service.config_snapshot["embedding_text_contract"] == "chunk_heading_content_v2"


def test_embedding_pauses_before_over_budget_batch_and_resumes_same_run(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _, version_id, job_id = _prepare_chunked_job(session_factory, storage)
    adapter = FakeEmbeddingAdapter()
    limiter = ScriptedRateLimiter([0, 47])
    service = _embedding_service(
        session_factory,
        adapter,
        rate_limiter=limiter,
        batch_items=1,
    )

    assert service.execute(job_id) == "rate_limited"
    assert service.retry_after_seconds == 47
    first_call_count = len(adapter.calls)
    assert first_call_count == 1

    with session_factory() as db:
        runs = list(
            db.scalars(select(EmbeddingRun).where(EmbeddingRun.document_version_id == version_id))
        )
        assert len(runs) == 1
        assert runs[0].status is ProjectionRunStatus.RUNNING
        assert runs[0].batches[0].status is EmbeddingBatchStatus.SUCCEEDED
        assert runs[0].batches[1].status is EmbeddingBatchStatus.PENDING

    assert service.execute(job_id) == "deferred"
    assert len(adapter.calls) == len(limiter.reservations) - 1
    with session_factory() as db:
        runs = list(
            db.scalars(select(EmbeddingRun).where(EmbeddingRun.document_version_id == version_id))
        )
        assert len(runs) == 1
        assert runs[0].status is ProjectionRunStatus.SUCCEEDED


def test_ready_projection_supports_bm25_dense_delete_rebuild_and_alias_switch(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    document_id, version_id, job_id = _prepare_chunked_job(session_factory, storage)
    embedding = _embedding_service(session_factory, FakeEmbeddingAdapter())
    assert embedding.execute(job_id) == "deferred"
    adapter = MemoryOpenSearchAdapter()
    service = IndexingService(
        session_factory=session_factory,
        adapter=adapter,
        settings=_stage6_settings(),
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    )

    assert service.execute(job_id) == "succeeded"
    first_call_count = len(adapter.visible("rag-chunks-read"))
    assert first_call_count > 0
    assert adapter.search_bm25("rag-chunks-read", "policy")
    first_vector = adapter.visible("rag-chunks-read")[0]["embedding"]
    assert isinstance(first_vector, list)
    assert adapter.search_dense("rag-chunks-read", first_vector)
    assert adapter.search_dense_hits(
        "rag-chunks-read", first_vector, embedding_config_version="test-voyage-v1"
    )
    assert not adapter.search_dense_hits(
        "rag-chunks-read", first_vector, embedding_config_version="legacy-title-vector-v1"
    )
    assert service.execute(job_id) == "succeeded"
    assert len(adapter.visible("rag-chunks-read")) == first_call_count

    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        run = db.scalar(select(IndexingRun).where(IndexingRun.document_version_id == version_id))
        assert job is not None and job.status is JobStatus.SUCCEEDED
        assert job.document_version.status is VersionStatus.READY
        assert job.document_version.document.current_version_id == version_id
        assert run is not None and run.status is ProjectionRunStatus.SUCCEEDED
        assert run.indexed_node_count == first_call_count
        assert run.capability_snapshot["knn_available"] is True

    adapter.delete_index("rag-documents-v1")
    adapter.delete_index("rag-chunks-v1")
    assert service.rebuild_ready() == {"documents": 1, "nodes": first_call_count}
    assert len(adapter.visible("rag-chunks-read")) == first_call_count

    adapter.ensure_indexes("rag-documents-v2", "rag-chunks-v2", 4)
    service.switch_aliases("rag-documents-v2", "rag-chunks-v2")
    assert adapter.aliases["rag-documents-read"] == "rag-documents-v2"
    assert adapter.aliases["rag-chunks-read"] == "rag-chunks-v2"
    assert adapter.aliases["rag-chunks-write"] == "rag-chunks-v2"

    response = client.get(f"/api/v1/documents/{document_id}/versions/{version_id}/indexing-runs")
    assert response.status_code == 200
    assert response.json()[0]["status"] == "succeeded"

    cast(FastAPI, client.app).dependency_overrides[get_search_admin_service] = lambda: service
    capabilities = client.get("/api/v1/system/search-capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["knn_available"] is True
    deletion = client.delete(f"/api/v1/documents/{document_id}")
    assert deletion.status_code == 200
    assert deletion.json()["status"] == "deleted"
    assert not adapter.visible("rag-chunks-read")
    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        assert job is not None
        assert job.document_version.document.status is DocumentStatus.DELETED
        assert job.document_version.document.current_version_id is None

    restored = client.post(f"/api/v1/documents/{document_id}/restore")
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    assert restored.json()["document_version_id"] == str(version_id)
    assert len(adapter.visible("rag-chunks-read")) == first_call_count

    reprocess = client.post(f"/api/v1/documents/{document_id}/reprocess")
    assert reprocess.status_code == 202
    assert reprocess.json()["job_type"] == "reprocess"
    assert reprocess.json()["status"] == "pending"

    assert client.delete(f"/api/v1/documents/{document_id}").status_code == 200
    mismatch = client.request(
        "DELETE",
        f"/api/v1/documents/{document_id}/purge",
        json={"confirmation": "wrong name"},
    )
    assert mismatch.status_code == 409
    with session_factory() as db:
        document = db.get(Document, document_id)
        assert document is not None
        display_name = document.display_name
    purged = client.request(
        "DELETE",
        f"/api/v1/documents/{document_id}/purge",
        json={"confirmation": display_name},
    )
    assert purged.status_code == 200
    assert purged.json()["status"] == "purged"
    with session_factory() as db:
        assert db.get(Document, document_id) is None


def test_same_version_reindex_replaces_nodes_from_older_chunking_run(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _, version_id, job_id = _prepare_chunked_job(session_factory, storage)
    embedding = _embedding_service(session_factory, FakeEmbeddingAdapter())
    assert embedding.execute(job_id) == "deferred"
    adapter = MemoryOpenSearchAdapter()
    settings = _stage6_settings()
    service = IndexingService(
        session_factory=session_factory,
        adapter=adapter,
        settings=settings,
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    )

    assert service.execute(job_id) == "succeeded"
    expected_nodes = adapter.count_version("rag-chunks-v1", str(version_id))
    stale_node_id = uuid.uuid4()
    adapter.indices["rag-chunks-v1"][str(stale_node_id)] = {
        "_id": str(stale_node_id),
        "node_id": str(stale_node_id),
        "document_version_id": str(version_id),
        "node_level": "child",
        "is_active": True,
    }
    assert adapter.count_version("rag-chunks-v1", str(version_id)) == expected_nodes + 1

    replacement = IndexingService(
        session_factory=session_factory,
        adapter=adapter,
        settings=settings.model_copy(
            update={"opensearch_index_config_version": "stage6-opensearch-test-v3"}
        ),
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    )

    assert replacement.execute(job_id) == "succeeded"
    assert adapter.count_version("rag-chunks-v1", str(version_id)) == expected_nodes
    assert str(stale_node_id) not in adapter.indices["rag-chunks-v1"]
    current_nodes = [
        node
        for node in adapter.indices["rag-chunks-v1"].values()
        if node.get("document_version_id") == str(version_id)
    ]
    assert all(node.get("chunking_run_id") for node in current_nodes)
    assert all(node.get("is_active") is True for node in current_nodes)


def test_embedding_non_retryable_failure_is_persisted(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _, version_id, job_id = _prepare_chunked_job(session_factory, storage)

    class RejectedAdapter(FakeEmbeddingAdapter):
        def embed(self, texts: list[str], *, input_type: str) -> EmbeddingResponse:
            del texts, input_type
            raise EmbeddingAdapterError(
                "VOYAGE_BAD_REQUEST", "invalid input", retryable=False, status_code=400
            )

    assert _embedding_service(session_factory, RejectedAdapter()).execute(job_id) == "failed"
    with session_factory() as db:
        job = db.get(IngestionJob, job_id)
        run = db.scalar(select(EmbeddingRun).where(EmbeddingRun.document_version_id == version_id))
        assert job is not None and job.status is JobStatus.FAILED
        assert job.error_code == "VOYAGE_BAD_REQUEST"
        assert run is not None and run.status is ProjectionRunStatus.FAILED
        assert run.error and run.error["retryable"] is False
