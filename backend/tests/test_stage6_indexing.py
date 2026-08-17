import json
import uuid
from typing import cast

import httpx
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
from robust_rag.indexing.embedding_service import EmbeddingService
from robust_rag.indexing.gate import RetrievalNodeGateService
from robust_rag.indexing.opensearch import MemoryOpenSearchAdapter
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
    session_factory: sessionmaker[Session], adapter: FakeEmbeddingAdapter
) -> EmbeddingService:
    return EmbeddingService(
        session_factory=session_factory,
        adapter=adapter,
        config_version="test-voyage-v1",
        batch_items=2,
        batch_tokens=10000,
        max_retries=2,
        retry_base_seconds=0,
        retry_max_seconds=0,
        price_per_million_tokens=0.12,
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    )


def _stage6_settings() -> Settings:
    return Settings(_env_file=None).model_copy(
        update={
            "voyage_embedding_dimension": 4,
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


def test_embedding_batches_retry_audit_cost_and_idempotency(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    document_id, version_id, job_id = _prepare_chunked_job(session_factory, storage)
    adapter = FakeEmbeddingAdapter(failures=1)
    service = _embedding_service(session_factory, adapter)

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
