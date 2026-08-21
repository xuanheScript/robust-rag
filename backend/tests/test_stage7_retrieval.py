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
from robust_rag.indexing.opensearch import (
    DocumentSearchHit,
    HttpOpenSearchAdapter,
    MemoryOpenSearchAdapter,
    OpenSearchAdapter,
    SearchHit,
)
from robust_rag.indexing.rate_limit import VoyageRateLimiter
from robust_rag.indexing.service import IndexingService
from robust_rag.retrieval.context import assemble_context
from robust_rag.retrieval.fusion import (
    filter_rerank_candidates,
    fuse_relevance_scores,
    reciprocal_rank_fusion,
    select_mmr_candidates,
)
from robust_rag.retrieval.query import (
    IdentityQueryRewriter,
    QueryError,
    QueryRewriteResult,
    normalize_query,
    parse_query_plan,
    parse_tool_query_plan,
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

    def search_document_bm25_hits(
        self, alias: str, query: str, size: int = 10
    ) -> list[DocumentSearchHit]:
        return self.delegate.search_document_bm25_hits(alias, query, size)

    def search_chunk_bm25_hits(self, alias: str, query: str, size: int = 10) -> list[SearchHit]:
        self.observed_parallel_start = self.embedding_started.wait(timeout=1)
        if not self.observed_parallel_start:
            raise AssertionError("dense pipeline did not start while BM25 was running")
        return self.delegate.search_chunk_bm25_hits(alias, query, size)

    def search_dense_hits(
        self,
        alias: str,
        vector: list[float],
        size: int = 10,
        embedding_config_version: str | None = None,
    ) -> list[SearchHit]:
        return self.delegate.search_dense_hits(alias, vector, size, embedding_config_version)


def _retrieval_settings() -> Settings:
    return _stage6_settings().model_copy(
        update={
            "retrieval_bm25_top_k": 20,
            "retrieval_document_bm25_top_k": 10,
            "retrieval_dense_top_k": 20,
            "retrieval_rrf_top_k": 20,
            "retrieval_rerank_candidate_top_k": 10,
            "retrieval_final_child_top_k": 5,
            "retrieval_document_weight": 0.5,
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
    content: str = "content",
    rrf_score: float = 1,
    rerank_score: float | None = None,
    embedding: list[float] | None = None,
    content_types: list[str] | None = None,
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
        content=content,
        retrieval_text=content,
        content_types=content_types or ["paragraph"],
        source_locators=[],
        attributes={},
        token_count=1,
        embedding=embedding,
        rrf_score=rrf_score,
        rerank_score=rerank_score,
        exact_match=exact,
    )


def test_query_normalization_rrf_and_candidate_filter_are_deterministic() -> None:
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
        _candidate(uuid.uuid4(), document_id, parent_id, content="first evidence"),
        _candidate(uuid.uuid4(), document_id, parent_id, content="second evidence"),
        _candidate(uuid.uuid4(), document_id, parent_id, exact=True, content="exact evidence"),
    ]
    selected, excluded = filter_rerank_candidates(
        candidates,
        limit=3,
        sibling_similarity_threshold=0.99,
        min_rrf_score_ratio=0.1,
    )
    assert [value.node_id for value in selected] == [value.node_id for value in candidates]
    assert excluded == []

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
    assert plan.lexical_search_queries("住众公司 竞聘 岗位")[0] == ("住众公司 竞聘 岗位")
    assert plan.lexical_search_queries("住众公司 竞聘 岗位")[-1] == (
        "住众公司有哪些竞聘岗位? 岗位名称 所在部门 岗位定员"
    )
    assert plan.dense_search_queries("住众公司 竞聘 岗位")[-1] == (
        "查询住众公司竞聘岗位名称、部门和岗位定员 岗位名称 所在部门 岗位定员"
    )

    tool_plan, degraded = parse_tool_query_plan(
        {
            "query": "它什么时候生效?",
            "semantic_query": "合同模板 CT-2026-04 在什么时候正式生效?",
            "lexical_queries": ["CT-2026-04 生效日期", "CT-2026-04 生效 时间"],
            "entities": ["CT-2026-04"],
            "answer_facets": ["生效日期"],
        },
        original_query="它什么时候生效?",
        max_chars=500,
        strategy="agent-query-plan",
        implementation="test-agent",
        version="1",
    )
    assert degraded is False
    assert tool_plan.semantic_query == "合同模板 CT-2026-04 在什么时候正式生效?"
    assert tool_plan.lexical_queries == (
        "CT-2026-04 生效日期",
        "CT-2026-04 生效 时间",
    )
    assert tool_plan.entities == ("CT-2026-04",)
    assert tool_plan.answer_facets == ("生效日期",)

    degraded_plan, degraded = parse_tool_query_plan(
        {"query": "Policy"},
        original_query="Policy",
        max_chars=500,
        strategy="agent-query-plan",
        implementation="test-agent",
        version="1",
    )
    assert degraded is True
    assert degraded_plan.semantic_query == "Policy"
    assert degraded_plan.lexical_queries == ()
    assert degraded_plan.metadata["plan_degraded"] is True


def test_document_prior_is_separate_and_does_not_flatten_sibling_chunk_ranks() -> None:
    document_id = str(uuid.uuid4())
    fused = reciprocal_rank_fusion(
        [
            SearchHit("relevant", 9, 1, document_id),
            SearchHit("irrelevant", 5, 2, document_id),
        ],
        [],
        rank_constant=60,
        bm25_weight=1,
        dense_weight=1,
        document_hits=[DocumentSearchHit(document_id, 12, 1)],
        document_weight=0.5,
        limit=10,
    )

    assert [value.node_id for value in fused] == ["relevant", "irrelevant"]
    assert fused[0].document_rrf_score == fused[1].document_rrf_score
    assert fused[0].chunk_rrf_score > fused[1].chunk_rrf_score
    assert fused[0].rrf_score == pytest.approx(
        fused[0].chunk_rrf_score + fused[0].document_rrf_score
    )


def test_prerank_filter_removes_only_noise_and_never_applies_a_document_quota() -> None:
    document_id = uuid.uuid4()
    candidates = [
        _candidate(
            uuid.uuid4(),
            document_id,
            uuid.uuid4(),
            content=f"竞聘流程证据 {index}",
            rrf_score=1 - index * 0.02,
        )
        for index in range(8)
    ]
    target = _candidate(
        uuid.uuid4(),
        document_id,
        uuid.uuid4(),
        content="会计岗职责及任职要求",
        rrf_score=0.82,
    )
    candidates.append(target)

    selected, excluded = filter_rerank_candidates(
        candidates,
        limit=40,
        sibling_similarity_threshold=0.96,
        min_rrf_score_ratio=0.25,
    )

    assert target in selected
    assert len(selected) == 9
    assert all(value["reason"] not in {"document_limit", "parent_limit"} for value in excluded)


def test_prerank_filter_explains_duplicate_heading_and_low_relevance_noise() -> None:
    document_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    first = _candidate(
        uuid.uuid4(),
        document_id,
        parent_id,
        content="竞聘岗位公告正文",
        rrf_score=1,
        embedding=[1, 0],
    )
    duplicate = _candidate(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        content="竞聘岗位公告正文",
        rrf_score=0.9,
    )
    duplicate.graph_rank = 1
    duplicate.graph_score = 0.8
    sibling_duplicate = _candidate(
        uuid.uuid4(),
        document_id,
        parent_id,
        content="竞聘岗位公告正文的重复切片",
        rrf_score=0.8,
        embedding=[1, 0],
    )
    heading_only = _candidate(
        uuid.uuid4(),
        document_id,
        uuid.uuid4(),
        content="附件 1",
        rrf_score=0.7,
        content_types=["heading"],
    )
    low_relevance = _candidate(
        uuid.uuid4(),
        document_id,
        uuid.uuid4(),
        content="食堂值班安排",
        rrf_score=0.1,
    )

    selected, excluded = filter_rerank_candidates(
        [first, duplicate, sibling_duplicate, heading_only, low_relevance],
        limit=40,
        sibling_similarity_threshold=0.96,
        min_rrf_score_ratio=0.25,
    )

    assert selected == [first]
    assert first.graph_rank == 1
    assert "duplicate_signals_merged" in first.selection_reasons
    assert [value["reason"] for value in excluded] == [
        "duplicate_content",
        "sibling_near_duplicate",
        "low_information_heading",
        "below_rrf_threshold",
    ]


def test_mmr_keeps_complementary_same_document_evidence() -> None:
    document_id = uuid.uuid4()
    first = _candidate(
        uuid.uuid4(),
        document_id,
        uuid.uuid4(),
        content="竞聘公告标题",
        rerank_score=0.95,
        embedding=[1, 0],
    )
    redundant = _candidate(
        uuid.uuid4(),
        document_id,
        uuid.uuid4(),
        content="附件中的竞聘公告标题",
        rerank_score=0.9,
        embedding=[1, 0],
    )
    complementary = _candidate(
        uuid.uuid4(),
        document_id,
        uuid.uuid4(),
        content="会计岗职责及任职要求",
        rerank_score=0.8,
        embedding=[0, 1],
    )

    selected = select_mmr_candidates(
        [first, redundant, complementary], limit=3, relevance_weight=0.85
    )

    assert selected == [first, complementary, redundant]
    assert complementary.max_selected_similarity == 0
    assert all(value.mmr_score is not None for value in selected)


def test_hybrid_relevance_preserves_scope_and_first_stage_signals() -> None:
    target = _candidate(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        content="岗位名称: 会计岗; 所在部门: 财务部; 岗位定员: 1人",
        rrf_score=0.8,
        rerank_score=0.75,
    )
    target.title = "住众公司 2026 年度空缺岗位内部选聘公告"
    target.heading_path = ["内部选聘岗位职责及任职要求"]
    target.bm25_score = 0.7
    wrong_document = _candidate(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        content="竞聘部门及岗位",
        rrf_score=0.4,
        rerank_score=0.9,
    )
    wrong_document.title = "新岸公司及棉三公司中层管理人员竞聘上岗公告"
    wrong_document.bm25_score = 0.6

    ranked = fuse_relevance_scores(
        [wrong_document, target],
        entities=("住众公司",),
        rerank_weight=0.55,
        rrf_weight=0.25,
        lexical_weight=0.1,
        scope_weight=0.1,
    )

    assert ranked[0] is target
    assert target.scope_match_score == 1
    assert wrong_document.scope_match_score == 0
    assert target.relevance_score is not None
    assert target.normalized_rrf_score == 1
    assert "Source document: 住众公司" in target.rerank_text()


def test_parent_merge_happens_before_context_slot_cutoff() -> None:
    parent_id = uuid.uuid4()
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    answer_id = uuid.uuid4()
    document_id = uuid.uuid4()
    first = _candidate(first_id, document_id, parent_id, content="流程上半段")
    second = _candidate(second_id, document_id, parent_id, content="流程下半段")
    first.next_node_id = second_id
    second.previous_node_id = first_id
    answer = _candidate(answer_id, document_id, uuid.uuid4(), content="岗位名称: 会计岗")
    nodes = {
        parent_id: NodeValue(
            parent_id,
            None,
            None,
            None,
            "公告",
            ["流程"],
            "完整流程",
            ["paragraph"],
            [],
            {},
            5,
        ),
        first_id: NodeValue(
            first_id,
            parent_id,
            None,
            second_id,
            "公告",
            ["流程"],
            "流程上半段",
            ["paragraph"],
            [],
            {"child_ordinal": 0},
            3,
        ),
        second_id: NodeValue(
            second_id,
            parent_id,
            first_id,
            None,
            "公告",
            ["流程"],
            "流程下半段",
            ["paragraph"],
            [],
            {"child_ordinal": 1},
            3,
        ),
        answer_id: NodeValue(
            answer_id,
            answer.parent_node_id,
            None,
            None,
            "公告附件",
            ["岗位职责及任职要求"],
            "岗位名称: 会计岗",
            ["table"],
            [],
            {},
            4,
        ),
    }

    context, _ = assemble_context(
        [first, second, answer],
        nodes,
        budget_tokens=20,
        parent_max_tokens=20,
        neighbor_limit=0,
        max_context_nodes=2,
    )

    assert [value.role for value in context] == ["parent", "child"]
    assert context[0].supporting_child_ids == [first_id, second_id]
    assert context[1].content == "岗位名称: 会计岗"


def test_memory_search_keeps_document_title_out_of_chunk_bm25() -> None:
    adapter = MemoryOpenSearchAdapter()
    adapter.ensure_indexes("documents", "chunks", 2)
    adapter.switch_aliases(
        documents_index="documents",
        chunks_index="chunks",
        documents_read_alias="documents-read",
        chunks_read_alias="chunks-read",
        chunks_write_alias="chunks-write",
    )
    document_id = str(uuid.uuid4())
    adapter.bulk_upsert(
        "documents",
        [
            {
                "_id": document_id,
                "document_id": document_id,
                "title": "Accounting recruitment notice",
                "original_filename": "notice.pdf",
                "is_active": True,
            }
        ],
    )
    adapter.bulk_upsert(
        "chunks",
        [
            {
                "_id": "accounting",
                "node_id": "accounting",
                "document_id": document_id,
                "node_level": "child",
                "title": "Accounting recruitment notice",
                "heading_path": ["Open positions"],
                "content": "Accounting role responsibilities and qualifications",
                "retrieval_text": "Accounting recruitment notice\nAccounting role",
                "retrieval_keywords": ["position identifier"],
                "is_active": True,
            },
            {
                "_id": "cafeteria",
                "node_id": "cafeteria",
                "document_id": document_id,
                "node_level": "child",
                "title": "Accounting recruitment notice",
                "heading_path": ["Benefits"],
                "content": "Cafeteria and commuting benefits",
                "retrieval_text": "Accounting recruitment notice\nCafeteria benefits",
                "is_active": True,
            },
        ],
    )

    assert adapter.search_document_bm25_hits("documents-read", "Accounting")
    chunk_hits = adapter.search_chunk_bm25_hits("chunks-read", "Accounting")
    assert [hit.node_id for hit in chunk_hits] == ["accounting"]
    assert [
        hit.node_id for hit in adapter.search_chunk_bm25_hits("chunks-read", "position identifier")
    ] == ["accounting"]
    assert adapter.search_chunk_bm25_hits("chunks-read", "recruitment notice") == []


def test_http_search_uses_disjoint_document_and_chunk_fields() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"hits": {"hits": []}})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://opensearch.test")
    adapter = HttpOpenSearchAdapter(
        base_url="http://opensearch.test",
        username=None,
        password=None,
        verify=False,
        timeout_seconds=1,
        client=client,
    )

    adapter.search_document_bm25_hits("documents-read", "Accounting")
    adapter.search_chunk_bm25_hits("chunks-read", "Accounting")

    document_query = cast(dict[str, object], requests[0]["query"])
    document_multi = cast(dict[str, object], document_query["multi_match"])
    document_fields = cast(list[str], document_multi["fields"])
    chunk_bool = cast(dict[str, object], cast(dict[str, object], requests[1]["query"])["bool"])
    chunk_must = cast(list[dict[str, object]], chunk_bool["must"])
    chunk_multi = cast(dict[str, object], chunk_must[0]["multi_match"])
    chunk_fields = cast(list[str], chunk_multi["fields"])
    assert any(field.startswith("title") for field in document_fields)
    assert all(not field.startswith("title") for field in chunk_fields)
    assert all(not field.startswith("retrieval_text") for field in chunk_fields)
    assert any(field.startswith("content") for field in chunk_fields)
    assert any(field.startswith("retrieval_keywords") for field in chunk_fields)
    client.close()


def test_rerank_text_separates_document_scope_hierarchy_and_evidence() -> None:
    candidate = _candidate(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    text = candidate.rerank_text()

    assert "Source document: Title" in text
    assert "Hierarchy: Section" in text
    assert "Evidence:" in text
    assert "content" in text


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
    assert parent_context[0].reason == "auto_merged_parent"
    assert parent_context[0].supporting_child_ids == [first_id, second_id]
    assert used == 10

    child_context, used = assemble_context(
        [first], nodes, budget_tokens=8, parent_max_tokens=5, neighbor_limit=1
    )
    assert [value.role for value in child_context] == ["child", "neighbor"]
    assert used == 8


def test_context_assembly_merges_adjacent_hits_when_parent_ratio_is_low() -> None:
    parent_id = uuid.uuid4()
    child_ids = [uuid.uuid4() for _ in range(4)]
    document_id = uuid.uuid4()
    candidates = [
        _candidate(child_ids[1], document_id, parent_id, content="岗位名称与定员"),
        _candidate(child_ids[2], document_id, parent_id, content="岗位职责与任职要求"),
    ]
    candidates[0].previous_node_id = child_ids[0]
    candidates[0].next_node_id = child_ids[2]
    candidates[1].previous_node_id = child_ids[1]
    candidates[1].next_node_id = child_ids[3]
    nodes = {
        parent_id: NodeValue(
            parent_id,
            None,
            None,
            None,
            "竞聘公告",
            ["竞聘岗位"],
            "完整但很长的 parent",
            ["paragraph"],
            [],
            {},
            20,
        )
    }
    for index, child_id in enumerate(child_ids):
        nodes[child_id] = NodeValue(
            child_id,
            parent_id,
            child_ids[index - 1] if index else None,
            child_ids[index + 1] if index + 1 < len(child_ids) else None,
            "竞聘公告",
            ["竞聘岗位"],
            f"第 {index + 1} 个切片",
            ["paragraph"],
            [],
            {"child_ordinal": index},
            4,
        )

    context, used = assemble_context(
        candidates,
        nodes,
        budget_tokens=20,
        parent_max_tokens=20,
        neighbor_limit=1,
        parent_merge_min_children=2,
        parent_merge_ratio=0.75,
    )

    assert len(context) == 1
    assert context[0].role == "window"
    assert context[0].reason == "adjacent_selected_children"
    assert context[0].supporting_child_ids == child_ids[1:3]
    assert context[0].content == "第 2 个切片\n\n第 3 个切片"
    assert used == 8


def test_all_retrieval_modes_and_debug_trace_are_independently_runnable(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
) -> None:
    _, _, search_adapter = _ready_search_fixture(session_factory, storage)
    document_title = str(search_adapter.visible("rag-documents-read")[0]["title"])
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
    assert responses[RetrievalMode.BM25].usage["document_bm25_retries"] == 0
    assert responses[RetrievalMode.DENSE].usage["dense_retries"] == 0
    assert len(embedding.calls) == 3
    assert all(input_type == "query" for _, input_type in embedding.calls)
    assert len(reranker.calls) == 1
    assert responses[RetrievalMode.HYBRID_RERANK].debug
    rerank_debug = responses[RetrievalMode.HYBRID_RERANK].debug
    assert rerank_debug is not None
    debug_reranked = cast(list[dict[str, object]], rerank_debug["reranked"])
    debug_selected = cast(list[dict[str, object]], rerank_debug["selected"])
    assert all(value["mmr_score"] is None for value in debug_reranked)
    assert all(value["mmr_score"] is not None for value in debug_selected)

    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(RetrievalTrace)) == 4
        traces = list(db.scalars(select(RetrievalTrace)))
        assert all(trace.status is RetrievalTraceStatus.SUCCEEDED for trace in traces)
        assert all(trace.context_nodes_json for trace in traces)

    cast(FastAPI, client.app).dependency_overrides[get_retrieval_service] = lambda: service
    api_response = client.post(
        "/api/v1/retrieval/search",
        json={
            "query": f"  {document_title}  ",
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
    assert trace_response.json()["document_candidates_json"]
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
