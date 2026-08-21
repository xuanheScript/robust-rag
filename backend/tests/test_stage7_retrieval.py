import json
import uuid
from collections.abc import Callable
from threading import Event
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.core.settings import Settings
from robust_rag.db.enums import RetrievalMode, RetrievalTraceStatus
from robust_rag.db.models import RetrievalTrace
from robust_rag.indexing.embedding import EmbeddingAdapterError, EmbeddingResponse
from robust_rag.indexing.opensearch import MemoryOpenSearchAdapter, OpenSearchAdapter, SearchHit
from robust_rag.indexing.rate_limit import VoyageRateLimiter
from robust_rag.indexing.service import IndexingService
from robust_rag.retrieval.context import assemble_context
from robust_rag.retrieval.fusion import diversify_candidates, reciprocal_rank_fusion
from robust_rag.retrieval.query import (
    IdentityQueryRewriter,
    QueryError,
    QueryRewriteResult,
    normalize_query,
    parse_query_plan,
)
from robust_rag.retrieval.rerank import (
    RerankAdapterError,
    RerankItem,
    RerankResponse,
    VoyageRerankAdapter,
)
from robust_rag.retrieval.schemas import Candidate, NodeValue, RetrievalSearchRequest
from robust_rag.retrieval.service import RetrievalError, RetrievalService, get_retrieval_service
from robust_rag.storage.local import LocalFileStorage
from tests.test_stage6_indexing import (
    FakeEmbeddingAdapter,
    _embedding_service,
    _prepare_chunked_job,
    _stage6_settings,
)


class FakeQueryEmbeddingAdapter:
    provider = "voyage"
    model = "voyage-4"
    dimension = 4

    def __init__(self, failure: EmbeddingAdapterError | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[list[str], str]] = []

    def embed(self, texts: list[str], *, input_type: str) -> EmbeddingResponse:
        self.calls.append((texts, input_type))
        if self.failure is not None:
            raise self.failure
        return EmbeddingResponse(vectors=[[1.0, 1.0, 1.0, 1.0]], total_tokens=4)


class FakeRerankAdapter:
    provider = "voyage"
    model = "rerank-2.5"

    def __init__(self, failure: RerankAdapterError | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, list[str], int]] = []

    def rerank(self, query: str, documents: list[str], *, top_k: int) -> RerankResponse:
        self.calls.append((query, documents, top_k))
        if self.failure is not None:
            raise self.failure
        return RerankResponse(
            results=[
                RerankItem(index=index, relevance_score=float(index + 1) / len(documents))
                for index in reversed(range(len(documents)))
            ],
            total_tokens=len(documents) * 11,
        )


class QueryRateLimiter:
    def __init__(self, wait_seconds: float) -> None:
        self.wait_seconds = wait_seconds
        self.reservations: list[int] = []

    def reserve(self, estimated_tokens: int) -> float:
        self.reservations.append(estimated_tokens)
        return self.wait_seconds


class ParallelProbeEmbeddingAdapter(FakeQueryEmbeddingAdapter):
    def __init__(self, started: Event) -> None:
        super().__init__()
        self.started = started

    def embed(self, texts: list[str], *, input_type: str) -> EmbeddingResponse:
        self.started.set()
        return super().embed(texts, input_type=input_type)


class ParallelProbeSearchAdapter:
    def __init__(self, delegate: MemoryOpenSearchAdapter, embedding_started: Event) -> None:
        self.delegate = delegate
        self.embedding_started = embedding_started
        self.observed_parallel_start = False

    def search_bm25_hits(self, alias: str, query: str, size: int = 10) -> list[SearchHit]:
        self.observed_parallel_start = self.embedding_started.wait(timeout=1)
        if not self.observed_parallel_start:
            raise AssertionError("dense pipeline did not start while BM25 was running")
        return self.delegate.search_bm25_hits(alias, query, size)

    def search_dense_hits(self, alias: str, vector: list[float], size: int = 10) -> list[SearchHit]:
        return self.delegate.search_dense_hits(alias, vector, size)


def _retrieval_settings() -> Settings:
    return _stage6_settings().model_copy(
        update={
            "retrieval_bm25_top_k": 20,
            "retrieval_dense_top_k": 20,
            "retrieval_rrf_top_k": 20,
            "retrieval_rerank_candidate_top_k": 10,
            "retrieval_final_child_top_k": 5,
            "retrieval_context_max_tokens": 1000,
            "retrieval_context_parent_max_tokens": 500,
            "voyage_rerank_max_retries": 0,
            "voyage_embedding_max_retries": 0,
            "opensearch_max_retries": 0,
        }
    )


def _ready_search_fixture(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> tuple[uuid.UUID, uuid.UUID, MemoryOpenSearchAdapter]:
    document_id, version_id, job_id = _prepare_chunked_job(session_factory, storage)
    assert _embedding_service(session_factory, FakeEmbeddingAdapter()).execute(job_id) == "deferred"
    adapter = MemoryOpenSearchAdapter()
    indexing = IndexingService(
        session_factory=session_factory,
        adapter=adapter,
        settings=_retrieval_settings(),
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    )
    assert indexing.execute(job_id) == "succeeded"
    return document_id, version_id, adapter


def _retrieval_service(
    session_factory: sessionmaker[Session],
    search_adapter: OpenSearchAdapter,
    *,
    embedding: FakeQueryEmbeddingAdapter | None = None,
    reranker: FakeRerankAdapter | None = None,
    embedding_rate_limiter: VoyageRateLimiter | None = None,
    sleeper: Callable[[float], None] = lambda _delay: None,
) -> RetrievalService:
    return RetrievalService(
        session_factory=session_factory,
        search_adapter=search_adapter,
        embedding_adapter=embedding or FakeQueryEmbeddingAdapter(),
        rerank_adapter=reranker or FakeRerankAdapter(),
        query_rewriter=IdentityQueryRewriter(),
        settings=_retrieval_settings(),
        embedding_rate_limiter=embedding_rate_limiter,
        sleeper=sleeper,
        jitter=lambda: 0,
    )


def _candidate(
    node_id: uuid.UUID,
    document_id: uuid.UUID,
    parent_id: uuid.UUID,
    *,
    exact: bool = False,
) -> Candidate:
    return Candidate(
        node_id=node_id,
        document_id=document_id,
        document_version_id=uuid.uuid4(),
        parent_node_id=parent_id,
        previous_node_id=None,
        next_node_id=None,
        title="Title",
        heading_path=["Section"],
        content="content",
        retrieval_text="content",
        content_types=["paragraph"],
        source_locators=[],
        attributes={},
        token_count=1,
        exact_match=exact,
    )


def test_query_normalization_rrf_and_diversity_are_deterministic() -> None:
    query = "  \uff21\uff22\uff23-100  是什么 \uff1f "
    assert normalize_query(query, max_chars=100) == "ABC-100 是什么?"
    with pytest.raises(QueryError, match="empty"):
        normalize_query(" \u200b ", max_chars=100)

    fused = reciprocal_rank_fusion(
        [SearchHit("a", 8, 1), SearchHit("b", 7, 2)],
        [SearchHit("b", 0.9, 1), SearchHit("c", 0.8, 2)],
        rank_constant=60,
        bm25_weight=1,
        dense_weight=1,
        limit=10,
    )
    assert [value.node_id for value in fused] == ["b", "a", "c"]
    assert fused[0].rrf_score == pytest.approx(1 / 62 + 1 / 61)

    document_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    candidates = [
        _candidate(uuid.uuid4(), document_id, parent_id),
        _candidate(uuid.uuid4(), document_id, parent_id),
        _candidate(uuid.uuid4(), document_id, parent_id, exact=True),
    ]
    selected, excluded = diversify_candidates(
        candidates, max_per_document=1, max_per_parent=1, limit=3
    )
    assert [value.node_id for value in selected] == [candidates[0].node_id, candidates[2].node_id]
    assert excluded == [{"node_id": str(candidates[1].node_id), "reason": "document_limit"}]

    plan = parse_query_plan(
        json.dumps(
            {
                "standalone_query": "住众公司有哪些竞聘岗位?",
                "semantic_query": "查询住众公司竞聘岗位名称、部门和岗位定员",
                "lexical_queries": [
                    "住众公司 竞聘 岗位名称 所在部门 岗位定员",
                    "住众公司 招聘 职位",
                    "这个查询应被截断",
                ],
                "entities": ["住众公司"],
                "answer_facets": ["岗位名称", "所在部门", "岗位定员"],
                "filters": {},
            },
            ensure_ascii=False,
        ),
        original_query="住众公司 竞聘 岗位",
        max_chars=500,
        strategy="test",
        implementation="fake",
        version="1",
    )
    assert plan.query == "住众公司有哪些竞聘岗位?"
    assert len(plan.lexical_queries) == 2
    assert plan.lexical_search_queries("住众公司 竞聘 岗位")[0] == (
        "住众公司 竞聘 岗位"
    )


def test_query_plan_expansion_adds_recall_without_dropping_original(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _, _, search_adapter = _ready_search_fixture(session_factory, storage)
    service = _retrieval_service(session_factory, search_adapter)
    plan = QueryRewriteResult(
        query="生效时间是什么",
        semantic_query="企业 Policy 的生效时间是什么",
        lexical_queries=("Policy",),
        strategy="retrieval-query-plan",
        implementation="test",
        version="2",
        changed=True,
    )

    response = service.search(
        RetrievalSearchRequest(
            query="生效时间是什么",
            mode=RetrievalMode.BM25,
            debug=True,
        ),
        rewrite_override=plan,
    )

    assert response.children
    assert response.usage["bm25_query_count"] == 2
    assert response.debug is not None
    debug_queries = cast(list[dict[str, object]], response.debug["queries"])
    assert debug_queries[0]["lexical"] == ["生效时间是什么", "Policy"]
    with session_factory() as db:
        trace = db.get(RetrievalTrace, response.trace_id)
        assert trace is not None
        assert trace.rewrite_snapshot["lexical_queries"] == ["Policy"]


def test_voyage_rerank_adapter_uses_official_contract_and_validates_indices() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "document": "second", "relevance_score": 0.9},
                    {"index": 0, "document": "first", "relevance_score": 0.3},
                ],
                "usage": {"total_tokens": 21},
            },
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.voyageai.com/v1"
    )
    adapter = VoyageRerankAdapter(api_key="secret", client=client)
    response = adapter.rerank("query", ["first", "second"], top_k=2)

    assert [value.index for value in response.results] == [1, 0]
    assert response.total_tokens == 21
    assert requests[0] == {
        "query": "query",
        "documents": ["first", "second"],
        "model": "rerank-2.5",
        "top_k": 2,
        "truncation": True,
    }

    invalid_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "relevance_score": 0.9}],
                    "usage": {"total_tokens": 10},
                },
            )
        ),
        base_url="https://api.voyageai.com/v1",
    )
    with pytest.raises(RerankAdapterError, match="indices or count"):
        VoyageRerankAdapter(api_key="secret", client=invalid_client).rerank(
            "query", ["first", "second"], top_k=2
        )


def test_context_assembly_deduplicates_parent_and_uses_neighbor_when_parent_is_too_large() -> None:
    parent_id = uuid.uuid4()
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    document_id = uuid.uuid4()
    first = _candidate(first_id, document_id, parent_id)
    first.next_node_id = second_id
    second = _candidate(second_id, document_id, parent_id)
    second.previous_node_id = first_id
    nodes = {
        parent_id: NodeValue(
            parent_id,
            None,
            None,
            None,
            "Title",
            ["Section"],
            "parent context",
            ["paragraph"],
            [],
            {},
            10,
        ),
        first_id: NodeValue(
            first_id,
            parent_id,
            None,
            second_id,
            "Title",
            ["Section"],
            "first child",
            ["paragraph"],
            [],
            {},
            4,
        ),
        second_id: NodeValue(
            second_id,
            parent_id,
            first_id,
            None,
            "Title",
            ["Section"],
            "second child",
            ["paragraph"],
            [],
            {},
            4,
        ),
    }

    parent_context, used = assemble_context(
        [first, second], nodes, budget_tokens=20, parent_max_tokens=20, neighbor_limit=1
    )
    assert len(parent_context) == 1
    assert parent_context[0].role == "parent"
    assert parent_context[0].supporting_child_ids == [first_id, second_id]
    assert used == 10

    child_context, used = assemble_context(
        [first], nodes, budget_tokens=8, parent_max_tokens=5, neighbor_limit=1
    )
    assert [value.role for value in child_context] == ["child", "neighbor"]
    assert used == 8


def test_all_retrieval_modes_and_debug_trace_are_independently_runnable(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    _, _, search_adapter = _ready_search_fixture(session_factory, storage)
    embedding = FakeQueryEmbeddingAdapter()
    reranker = FakeRerankAdapter()
    service = _retrieval_service(
        session_factory, search_adapter, embedding=embedding, reranker=reranker
    )

    responses = {
        mode: service.search(RetrievalSearchRequest(query="Policy", mode=mode, debug=True))
        for mode in RetrievalMode
    }

    assert all(response.children for response in responses.values())
    assert all(response.context_nodes for response in responses.values())
    assert responses[RetrievalMode.BM25].children[0].bm25_rank == 1
    assert responses[RetrievalMode.BM25].children[0].dense_rank is None
    assert responses[RetrievalMode.DENSE].children[0].dense_rank == 1
    assert responses[RetrievalMode.HYBRID].children[0].rrf_score > 0
    assert responses[RetrievalMode.HYBRID_RERANK].children[0].rerank_score is not None
    assert responses[RetrievalMode.BM25].usage["bm25_retries"] == 0
    assert responses[RetrievalMode.DENSE].usage["dense_retries"] == 0
    assert len(embedding.calls) == 3
    assert all(input_type == "query" for _, input_type in embedding.calls)
    assert len(reranker.calls) == 1
    assert responses[RetrievalMode.HYBRID_RERANK].debug

    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(RetrievalTrace)) == 4
        traces = list(db.scalars(select(RetrievalTrace)))
        assert all(trace.status is RetrievalTraceStatus.SUCCEEDED for trace in traces)
        assert all(trace.context_nodes_json for trace in traces)

    cast(FastAPI, client.app).dependency_overrides[get_retrieval_service] = lambda: service
    api_response = client.post(
        "/api/v1/retrieval/search",
        json={
            "query": "  Policy  ",
            "mode": "hybrid_rerank",
            "top_k": 100,
            "context_budget_tokens": 100000,
            "debug": True,
        },
    )
    assert api_response.status_code == 200
    assert len(api_response.json()["children"]) <= 5
    assert api_response.json()["context_budget_tokens"] == 1000
    trace_id = api_response.json()["trace_id"]
    trace_response = client.get(f"/api/v1/retrieval/traces/{trace_id}")
    assert trace_response.status_code == 200
    assert trace_response.json()["rrf_candidates_json"]
    assert trace_response.json()["context_nodes_json"]
    assert trace_response.json()["config_snapshot"]["request_top_k"] == 100
    assert trace_response.json()["config_snapshot"]["effective_final_child_top_k"] == 5
    list_response = client.get("/api/v1/retrieval/traces?limit=2")
    assert list_response.status_code == 200
    assert len(list_response.json()) == 2

    empty_query = client.post(
        "/api/v1/retrieval/search", json={"query": " \u200b ", "mode": "bm25"}
    )
    assert empty_query.status_code == 422
    assert empty_query.json()["error"]["code"] == "QUERY_EMPTY"


def test_query_embedding_uses_the_shared_voyage_budget(
    session_factory: sessionmaker[Session],
) -> None:
    limiter = QueryRateLimiter(12)
    slept: list[float] = []
    embedding = FakeQueryEmbeddingAdapter()
    service = _retrieval_service(
        session_factory,
        MemoryOpenSearchAdapter(),
        embedding=embedding,
        embedding_rate_limiter=limiter,
        sleeper=slept.append,
    )

    response, retries = service._embed_query_with_retry("shared Voyage budget")

    assert response.total_tokens == 4
    assert retries == 0
    assert limiter.reservations == [5]
    assert slept == [12]
    assert embedding.calls == [(["shared Voyage budget"], "query")]


def test_hybrid_runs_bm25_and_dense_pipeline_in_parallel(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _, _, delegate = _ready_search_fixture(session_factory, storage)
    embedding_started = Event()
    search_adapter = ParallelProbeSearchAdapter(delegate, embedding_started)
    service = _retrieval_service(
        session_factory,
        cast(OpenSearchAdapter, search_adapter),
        embedding=ParallelProbeEmbeddingAdapter(embedding_started),
    )

    response = service.search(
        RetrievalSearchRequest(query="Policy", mode=RetrievalMode.HYBRID_RERANK)
    )

    assert search_adapter.observed_parallel_start is True
    assert response.children
    assert cast(float, response.latency_ms["lexical_dense_fanout"]) >= 0
    assert cast(float, response.latency_ms["parallel_savings_estimate"]) >= 0
    assert cast(float, response.latency_ms["dense_pipeline"]) >= 0


def test_rerank_failure_degrades_but_dense_failure_is_audited(
    session_factory: sessionmaker[Session], storage: LocalFileStorage
) -> None:
    _, _, search_adapter = _ready_search_fixture(session_factory, storage)
    rerank_failure = RerankAdapterError("VOYAGE_RERANK_TIMEOUT", "timed out", retryable=True)
    degraded = _retrieval_service(
        session_factory,
        search_adapter,
        reranker=FakeRerankAdapter(failure=rerank_failure),
    ).search(RetrievalSearchRequest(query="Policy", mode=RetrievalMode.HYBRID_RERANK))
    assert degraded.status is RetrievalTraceStatus.DEGRADED
    assert degraded.rerank_fallback_reason == "VOYAGE_RERANK_TIMEOUT"
    assert degraded.children

    embedding_failure = EmbeddingAdapterError("VOYAGE_NETWORK_ERROR", "offline", retryable=True)
    failing = _retrieval_service(
        session_factory,
        search_adapter,
        embedding=FakeQueryEmbeddingAdapter(failure=embedding_failure),
    )
    with pytest.raises(RetrievalError, match="offline"):
        failing.search(RetrievalSearchRequest(query="Policy", mode=RetrievalMode.DENSE))
    with session_factory() as db:
        trace = db.scalar(
            select(RetrievalTrace)
            .where(RetrievalTrace.mode == RetrievalMode.DENSE)
            .order_by(RetrievalTrace.started_at.desc())
        )
        assert trace is not None
        assert trace.status is RetrievalTraceStatus.FAILED
        assert trace.error and trace.error["code"] == "VOYAGE_NETWORK_ERROR"
