"""Online BM25/Dense/RRF/Rerank retrieval with durable explainability traces."""

from __future__ import annotations

import random
import re
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Protocol, TypeVar, cast

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.core.observability import observe
from robust_rag.core.settings import Settings, get_settings
from robust_rag.db.enums import (
    DocumentStatus,
    ProjectionStatus,
    RetrievalMode,
    RetrievalNodeLevel,
    RetrievalTraceStatus,
    VersionStatus,
)
from robust_rag.db.models import Document, DocumentVersion, RetrievalNode, RetrievalTrace
from robust_rag.db.session import SessionLocal
from robust_rag.graph.schemas import GraphQueryResult, GraphSearchHit
from robust_rag.indexing.embedding import EmbeddingAdapter, EmbeddingAdapterError, EmbeddingResponse
from robust_rag.indexing.embedding_service import build_embedding_adapter
from robust_rag.indexing.opensearch import (
    DocumentSearchHit,
    OpenSearchAdapter,
    OpenSearchAdapterError,
    SearchHit,
)
from robust_rag.indexing.rate_limit import (
    NoopVoyageRateLimiter,
    RateLimiterUnavailable,
    VoyageRateLimiter,
    build_voyage_rate_limiter,
)
from robust_rag.indexing.service import get_opensearch_adapter
from robust_rag.retrieval.context import assemble_context
from robust_rag.retrieval.fusion import (
    FusedRank,
    filter_rerank_candidates,
    fuse_document_hit_lists,
    fuse_query_hit_lists,
    fuse_relevance_scores,
    reciprocal_rank_fusion,
    select_mmr_candidates,
)
from robust_rag.retrieval.query import (
    IdentityQueryRewriter,
    QueryRewriter,
    QueryRewriteResult,
    normalize_query,
)
from robust_rag.retrieval.rerank import (
    RerankAdapter,
    RerankAdapterError,
    RerankResponse,
    UnavailableRerankAdapter,
    VoyageRerankAdapter,
)
from robust_rag.retrieval.schemas import (
    Candidate,
    ContextNodeRead,
    NodeValue,
    RetrievalSearchRequest,
    RetrievalSearchResponse,
    RetrievedChildRead,
)

_SearchResultT = TypeVar("_SearchResultT", SearchHit, DocumentSearchHit)


class RetrievalError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class GraphRetriever(Protocol):
    def search(
        self, question: str, *, rewritten_question: str | None = None
    ) -> GraphQueryResult: ...


@dataclass(frozen=True)
class Bm25StageResult:
    hits: list[SearchHit]
    document_hits: list[DocumentSearchHit]
    retries: int
    document_retries: int
    document_latency_ms: float
    chunk_latency_ms: float
    latency_ms: float


@dataclass(frozen=True)
class DenseStageResult:
    hits: list[SearchHit]
    embedding: EmbeddingResponse
    embedding_retries: int
    dense_retries: int
    embedding_latency_ms: float
    dense_latency_ms: float
    latency_ms: float


class RetrievalService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        search_adapter: OpenSearchAdapter,
        embedding_adapter: EmbeddingAdapter,
        rerank_adapter: RerankAdapter,
        query_rewriter: QueryRewriter,
        settings: Settings,
        graph_retriever: GraphRetriever | None = None,
        embedding_rate_limiter: VoyageRateLimiter | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.session_factory = session_factory
        self.search_adapter = search_adapter
        self.embedding_adapter = embedding_adapter
        self.rerank_adapter = rerank_adapter
        self.query_rewriter = query_rewriter
        self.settings = settings
        self.graph_retriever = graph_retriever
        self.embedding_rate_limiter = embedding_rate_limiter or NoopVoyageRateLimiter()
        self.sleeper = sleeper
        self.jitter = jitter

    @property
    def config_snapshot(self) -> dict[str, object]:
        return {
            "query_max_chars": self.settings.retrieval_query_max_chars,
            "bm25_top_k": self.settings.retrieval_bm25_top_k,
            "document_bm25_top_k": self.settings.retrieval_document_bm25_top_k,
            "dense_top_k": self.settings.retrieval_dense_top_k,
            "rrf_top_k": self.settings.retrieval_rrf_top_k,
            "rrf_rank_constant": self.settings.retrieval_rrf_rank_constant,
            "bm25_weight": self.settings.retrieval_bm25_weight,
            "dense_weight": self.settings.retrieval_dense_weight,
            "document_weight": self.settings.retrieval_document_weight,
            "graph_enabled": self.graph_retriever is not None and self.settings.graph_query_enabled,
            "graph_weight": self.settings.graph_rrf_weight,
            "sibling_duplicate_similarity_threshold": (
                self.settings.retrieval_sibling_duplicate_similarity_threshold
            ),
            "min_rrf_score_ratio": self.settings.retrieval_min_rrf_score_ratio,
            "rerank_candidate_top_k": self.settings.retrieval_rerank_candidate_top_k,
            "final_child_top_k": self.settings.retrieval_final_child_top_k,
            "mmr_lambda": self.settings.retrieval_mmr_lambda,
            "relevance_rerank_weight": self.settings.retrieval_relevance_rerank_weight,
            "relevance_rrf_weight": self.settings.retrieval_relevance_rrf_weight,
            "relevance_lexical_weight": self.settings.retrieval_relevance_lexical_weight,
            "relevance_scope_weight": self.settings.retrieval_relevance_scope_weight,
            "context_candidate_top_k": self.settings.retrieval_context_candidate_top_k,
            "rerank_fallback_enabled": self.settings.retrieval_rerank_fallback_enabled,
            "context_max_tokens": self.settings.retrieval_context_max_tokens,
            "context_parent_max_tokens": self.settings.retrieval_context_parent_max_tokens,
            "context_neighbor_limit": self.settings.retrieval_context_neighbor_limit,
            "parent_merge_min_children": self.settings.retrieval_parent_merge_min_children,
            "parent_merge_ratio": self.settings.retrieval_parent_merge_ratio,
            "chunks_read_alias": self.settings.opensearch_chunks_read_alias,
            "documents_read_alias": self.settings.opensearch_documents_read_alias,
            "chunk_lexical_fields": ["heading_path", "content", "retrieval_keywords"],
            "document_lexical_fields": ["title", "original_filename"],
            "chunk_dense_text_contract": "scoped_chunk_v3",
            "chunk_dense_embedding_config_version": (self.settings.voyage_embedding_config_version),
        }

    def search(
        self,
        request: RetrievalSearchRequest,
        *,
        rewrite_override: QueryRewriteResult | None = None,
        use_graph: bool | None = None,
    ) -> RetrievalSearchResponse:
        total_started = time.perf_counter()
        normalized = normalize_query(
            request.query, max_chars=self.settings.retrieval_query_max_chars
        )
        rewrite = rewrite_override or self.query_rewriter.rewrite(normalized)
        lexical_queries = rewrite.lexical_search_queries(normalized)
        dense_queries = rewrite.dense_search_queries(normalized)
        budget = min(
            request.context_budget_tokens or self.settings.retrieval_context_max_tokens,
            self.settings.retrieval_context_max_tokens,
        )
        graph_requested = self.settings.graph_query_enabled if use_graph is None else use_graph
        graph_enabled = graph_requested and self.graph_retriever is not None
        trace_id = self._create_trace(
            request,
            normalized,
            rewrite.query,
            rewrite.snapshot(),
            budget,
            graph_requested=graph_requested,
            graph_enabled=graph_enabled,
        )
        latency: dict[str, object] = {}
        usage: dict[str, object] = {}
        document_hits: list[DocumentSearchHit] = []
        bm25_hits: list[SearchHit] = []
        dense_hits: list[SearchHit] = []
        graph_hits: list[GraphSearchHit] = []
        graph_query_trace_id: uuid.UUID | None = None
        graph_fallback_reason: str | None = None
        rerank_fallback_reason: str | None = None
        trace_status = RetrievalTraceStatus.SUCCEEDED

        try:
            needs_bm25 = request.mode in {
                RetrievalMode.BM25,
                RetrievalMode.HYBRID,
                RetrievalMode.HYBRID_RERANK,
            }
            needs_dense = request.mode in {
                RetrievalMode.DENSE,
                RetrievalMode.HYBRID,
                RetrievalMode.HYBRID_RERANK,
            }
            bm25_result: Bm25StageResult | None = None
            dense_result: DenseStageResult | None = None

            if needs_bm25 and needs_dense:
                fanout_started = time.perf_counter()
                with observe(
                    "retrieval.lexical_dense_fanout",
                    input={
                        "lexical_queries": lexical_queries,
                        "dense_queries": dense_queries,
                        "document_bm25_top_k": self.settings.retrieval_document_bm25_top_k,
                        "bm25_top_k": self.settings.retrieval_bm25_top_k,
                        "dense_top_k": self.settings.retrieval_dense_top_k,
                    },
                    metadata={"retrieval_trace_id": str(trace_id), "parallel": True},
                ) as fanout_span:
                    dense_context = copy_context()
                    with ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="retrieval-dense",
                    ) as executor:
                        dense_future = executor.submit(
                            dense_context.run,
                            self._run_dense_queries,
                            dense_queries,
                            trace_id,
                        )
                        bm25_result = self._run_bm25_queries(lexical_queries, trace_id)
                        dense_result = dense_future.result()
                    fanout_latency_ms = _elapsed_ms(fanout_started)
                    estimated_savings_ms = round(
                        max(
                            bm25_result.latency_ms + dense_result.latency_ms - fanout_latency_ms,
                            0,
                        ),
                        3,
                    )
                    fanout_span.update(
                        output={
                            "document_hit_count": len(bm25_result.document_hits),
                            "bm25_hit_count": len(bm25_result.hits),
                            "dense_hit_count": len(dense_result.hits),
                        },
                        metadata={
                            "latency_ms": fanout_latency_ms,
                            "estimated_savings_ms": estimated_savings_ms,
                        },
                    )
                latency["lexical_dense_fanout"] = fanout_latency_ms
                latency["parallel_savings_estimate"] = estimated_savings_ms
            elif needs_bm25:
                bm25_result = self._run_bm25_queries(lexical_queries, trace_id)
            elif needs_dense:
                dense_result = self._run_dense_queries(dense_queries, trace_id)

            if bm25_result is not None:
                document_hits = bm25_result.document_hits
                bm25_hits = bm25_result.hits
                usage["document_bm25_retries"] = bm25_result.document_retries
                usage["document_bm25_query_count"] = len(lexical_queries)
                usage["bm25_retries"] = bm25_result.retries
                usage["bm25_query_count"] = len(lexical_queries)
                latency["document_bm25"] = bm25_result.document_latency_ms
                latency["chunk_bm25"] = bm25_result.chunk_latency_ms
                latency["bm25"] = bm25_result.latency_ms

            if dense_result is not None:
                dense_hits = dense_result.hits
                embedding = dense_result.embedding
                usage["query_embedding_tokens"] = embedding.total_tokens
                usage["query_embedding_retries"] = dense_result.embedding_retries
                usage["dense_retries"] = dense_result.dense_retries
                usage["dense_query_count"] = len(dense_queries)
                latency["query_embedding"] = dense_result.embedding_latency_ms
                latency["dense"] = dense_result.dense_latency_ms
                latency["dense_pipeline"] = dense_result.latency_ms
                if (
                    embedding.total_tokens is not None
                    and self.settings.voyage_embedding_price_per_million_tokens is not None
                ):
                    usage["query_embedding_cost_usd"] = (
                        embedding.total_tokens
                        * self.settings.voyage_embedding_price_per_million_tokens
                        / 1_000_000
                    )

            if graph_enabled and request.mode in {
                RetrievalMode.HYBRID,
                RetrievalMode.HYBRID_RERANK,
            }:
                started = time.perf_counter()
                with observe(
                    "retrieval.graph",
                    as_type="retriever",
                    input={"query": rewrite.query},
                    metadata={"retrieval_trace_id": str(trace_id)},
                ) as span:
                    assert self.graph_retriever is not None
                    graph_result = self.graph_retriever.search(
                        request.query, rewritten_question=rewrite.query
                    )
                    span.update(
                        output={"hit_count": len(graph_result.hits)},
                        metadata={
                            "fallback_reason": graph_result.fallback_reason,
                            "graph_query_trace_id": str(graph_result.trace_id),
                            "hit_count": len(graph_result.hits),
                        },
                        level="WARNING" if graph_result.fallback_reason else "DEFAULT",
                        status_message=graph_result.fallback_reason,
                    )
                graph_hits = graph_result.hits
                graph_query_trace_id = graph_result.trace_id
                graph_fallback_reason = graph_result.fallback_reason
                if graph_fallback_reason:
                    trace_status = RetrievalTraceStatus.DEGRADED
                latency["graph"] = _elapsed_ms(started)

            started = time.perf_counter()
            node_document_ids = self._node_document_ids(
                [
                    *[hit.node_id for hit in bm25_hits],
                    *[hit.node_id for hit in dense_hits],
                    *[hit.node_id for hit in graph_hits],
                ]
            )
            fused = reciprocal_rank_fusion(
                bm25_hits,
                dense_hits,
                graph_hits,
                rank_constant=self.settings.retrieval_rrf_rank_constant,
                bm25_weight=self.settings.retrieval_bm25_weight,
                dense_weight=self.settings.retrieval_dense_weight,
                graph_weight=self.settings.graph_rrf_weight,
                document_hits=document_hits,
                node_document_ids=node_document_ids,
                document_weight=self.settings.retrieval_document_weight,
                limit=self.settings.retrieval_rrf_top_k,
            )
            candidates = self._hydrate_candidates(
                fused,
                [normalized, rewrite.query, rewrite.semantic_query or rewrite.query],
            )
            rerank_candidates, excluded = filter_rerank_candidates(
                candidates,
                limit=self.settings.retrieval_rerank_candidate_top_k,
                sibling_similarity_threshold=(
                    self.settings.retrieval_sibling_duplicate_similarity_threshold
                ),
                min_rrf_score_ratio=self.settings.retrieval_min_rrf_score_ratio,
            )
            filtered_snapshot = [
                *[dict(candidate.snapshot(), selected=True) for candidate in rerank_candidates],
                *[dict(value, selected=False) for value in excluded],
            ]
            latency["fusion_and_candidate_filter"] = _elapsed_ms(started)

            reranked: list[Candidate] = []
            rerank_query = rewrite.semantic_query or rewrite.query
            if request.mode is RetrievalMode.HYBRID_RERANK and rerank_candidates:
                started = time.perf_counter()
                try:
                    with observe(
                        "retrieval.rerank",
                        as_type="retriever",
                        input={"query": rerank_query, "candidate_count": len(rerank_candidates)},
                        metadata={
                            "retrieval_trace_id": str(trace_id),
                            "model": self.rerank_adapter.model,
                        },
                        model=self.rerank_adapter.model,
                    ) as span:
                        response, rerank_retries = self._rerank_with_retry(
                            rerank_query,
                            [candidate.rerank_text() for candidate in rerank_candidates],
                        )
                        reranked = self._apply_rerank(rerank_candidates, response)
                        span.update(
                            output={
                                "result_count": len(reranked),
                                "top_results": [
                                    candidate.snapshot() for candidate in reranked[:10]
                                ],
                            },
                            metadata={
                                "retry_count": rerank_retries,
                                "total_tokens": response.total_tokens,
                            },
                            usage_details=(
                                {"total": response.total_tokens}
                                if response.total_tokens is not None
                                else None
                            ),
                        )
                    usage["rerank_tokens"] = response.total_tokens
                    usage["rerank_retries"] = rerank_retries
                    if (
                        response.total_tokens is not None
                        and self.settings.voyage_rerank_price_per_million_tokens is not None
                    ):
                        usage["rerank_cost_usd"] = (
                            response.total_tokens
                            * self.settings.voyage_rerank_price_per_million_tokens
                            / 1_000_000
                        )
                except RerankAdapterError as exc:
                    if not self.settings.retrieval_rerank_fallback_enabled:
                        raise
                    rerank_fallback_reason = exc.code
                    trace_status = RetrievalTraceStatus.DEGRADED
                    reranked = list(rerank_candidates)
                    with observe(
                        "retrieval.rerank_fallback",
                        as_type="retriever",
                        metadata={
                            "retrieval_trace_id": str(trace_id),
                            "fallback_reason": exc.code,
                        },
                    ) as fallback_span:
                        fallback_span.update(
                            output={"result_count": len(reranked)},
                            level="WARNING",
                            status_message=exc.code,
                        )
                latency["rerank"] = _elapsed_ms(started)
            else:
                reranked = list(rerank_candidates)
            cross_encoder_snapshot = [candidate.snapshot() for candidate in reranked]
            started = time.perf_counter()
            with observe(
                "retrieval.relevance_fusion",
                as_type="retriever",
                input={
                    "candidate_count": len(reranked),
                    "entities": list(rewrite.entities),
                },
                metadata={
                    "retrieval_trace_id": str(trace_id),
                    "rerank_weight": self.settings.retrieval_relevance_rerank_weight,
                    "rrf_weight": self.settings.retrieval_relevance_rrf_weight,
                    "lexical_weight": self.settings.retrieval_relevance_lexical_weight,
                    "scope_weight": self.settings.retrieval_relevance_scope_weight,
                },
            ) as fusion_span:
                reranked = fuse_relevance_scores(
                    reranked,
                    entities=rewrite.entities,
                    rerank_weight=self.settings.retrieval_relevance_rerank_weight,
                    rrf_weight=self.settings.retrieval_relevance_rrf_weight,
                    lexical_weight=self.settings.retrieval_relevance_lexical_weight,
                    scope_weight=self.settings.retrieval_relevance_scope_weight,
                )
                fusion_span.update(
                    output={
                        "result_count": len(reranked),
                        "top_results": [candidate.snapshot() for candidate in reranked[:20]],
                    }
                )
            latency["relevance_fusion"] = _elapsed_ms(started)
            reranked_snapshot = [candidate.snapshot() for candidate in reranked]

            final_limit = min(
                request.top_k or self.settings.retrieval_final_child_top_k,
                self.settings.retrieval_final_child_top_k,
            )
            context_candidate_limit = min(
                max(final_limit, self.settings.retrieval_context_candidate_top_k),
                len(reranked),
            )
            started = time.perf_counter()
            with observe(
                "retrieval.mmr",
                as_type="retriever",
                input={
                    "candidate_count": len(reranked),
                    "limit": context_candidate_limit,
                },
                metadata={
                    "retrieval_trace_id": str(trace_id),
                    "lambda": self.settings.retrieval_mmr_lambda,
                },
            ) as mmr_span:
                context_candidates = select_mmr_candidates(
                    reranked,
                    limit=context_candidate_limit,
                    relevance_weight=self.settings.retrieval_mmr_lambda,
                )
                mmr_span.update(
                    output={
                        "result_count": len(context_candidates),
                        "top_results": [candidate.snapshot() for candidate in context_candidates],
                    }
                )
            latency["mmr"] = _elapsed_ms(started)
            nodes = self._load_context_nodes(context_candidates)
            started = time.perf_counter()
            with observe(
                "retrieval.context_assembly",
                as_type="retriever",
                input={
                    "candidate_count": len(context_candidates),
                    "context_limit": final_limit,
                    "token_budget": budget,
                },
                metadata={"retrieval_trace_id": str(trace_id)},
            ) as context_span:
                context_nodes, context_used = assemble_context(
                    context_candidates,
                    nodes,
                    budget_tokens=budget,
                    parent_max_tokens=self.settings.retrieval_context_parent_max_tokens,
                    neighbor_limit=self.settings.retrieval_context_neighbor_limit,
                    parent_merge_min_children=self.settings.retrieval_parent_merge_min_children,
                    parent_merge_ratio=self.settings.retrieval_parent_merge_ratio,
                    max_context_nodes=final_limit,
                )
                context_span.update(
                    output={
                        "context_count": len(context_nodes),
                        "used_tokens": context_used,
                        "nodes": [
                            {
                                "node_id": str(value.node_id),
                                "role": value.role,
                                "reason": value.reason,
                                "supporting_child_ids": [
                                    str(child_id) for child_id in value.supporting_child_ids
                                ],
                                "title": value.title,
                                "heading_path": value.heading_path,
                            }
                            for value in context_nodes
                        ],
                    }
                )
            selected = self._context_representatives(context_nodes, context_candidates)
            for rank, candidate in enumerate(selected, start=1):
                candidate.final_rank = rank
                candidate.selection_reasons.append("context_selected")
            latency["context_assembly"] = _elapsed_ms(started)
            latency["total"] = _elapsed_ms(total_started)
            stage_values: dict[str, list[dict[str, object]]] = {
                "queries": [
                    {
                        "original": normalized,
                        "standalone": rewrite.query,
                        "semantic": rewrite.semantic_query or rewrite.query,
                        "lexical": lexical_queries,
                        "dense": dense_queries,
                    }
                ],
                "documents": [_document_hit_snapshot(hit) for hit in document_hits],
                "bm25": [_hit_snapshot(hit) for hit in bm25_hits],
                "dense": [_hit_snapshot(hit) for hit in dense_hits],
                "graph": [hit.snapshot() for hit in graph_hits],
                "rrf": [value.snapshot() for value in fused],
                # `diversified` is retained as the persisted Stage 7 field for API compatibility.
                "filtered": filtered_snapshot,
                "diversified": filtered_snapshot,
                "cross_encoder": cross_encoder_snapshot,
                "reranked": reranked_snapshot,
                "context_candidates": [candidate.snapshot() for candidate in context_candidates],
                "selected": [candidate.snapshot() for candidate in selected],
                "context": [value.model_dump(mode="json") for value in context_nodes],
            }
            self._complete_trace(
                trace_id=trace_id,
                status=trace_status,
                stage_values=stage_values,
                context_used=context_used,
                usage=usage,
                latency=latency,
                rerank_fallback_reason=rerank_fallback_reason,
                graph_query_trace_id=graph_query_trace_id,
                graph_fallback_reason=graph_fallback_reason,
            )
        except (EmbeddingAdapterError, OpenSearchAdapterError, RerankAdapterError) as exc:
            latency["total"] = _elapsed_ms(total_started)
            error = _external_error(exc)
            self._fail_trace(trace_id, error, latency)
            raise RetrievalError(
                str(error["code"]),
                str(error["message"]),
                retryable=bool(error["retryable"]),
            ) from exc

        return RetrievalSearchResponse(
            trace_id=trace_id,
            status=trace_status,
            mode=request.mode,
            query_original=request.query,
            query_normalized=normalized,
            query_rewritten=rewrite.query,
            children=[self._child_read(candidate) for candidate in selected],
            context_nodes=context_nodes,
            context_budget_tokens=budget,
            context_used_tokens=context_used,
            rerank_fallback_reason=rerank_fallback_reason,
            graph_query_trace_id=graph_query_trace_id,
            graph_fallback_reason=graph_fallback_reason,
            usage=usage,
            latency_ms=latency,
            debug=cast(dict[str, object], stage_values) if request.debug else None,
        )

    def _run_bm25_stage(self, query: str, trace_id: uuid.UUID) -> Bm25StageResult:
        started = time.perf_counter()
        document_started = time.perf_counter()
        with observe(
            "retrieval.document_bm25",
            as_type="retriever",
            input={"query": query, "top_k": self.settings.retrieval_document_bm25_top_k},
            metadata={"retrieval_trace_id": str(trace_id), "signal_level": "document"},
        ) as document_span:
            document_hits, document_retries = self._search_with_retry(
                lambda: self.search_adapter.search_document_bm25_hits(
                    self.settings.opensearch_documents_read_alias,
                    query,
                    self.settings.retrieval_document_bm25_top_k,
                )
            )
            document_latency_ms = _elapsed_ms(document_started)
            document_span.update(
                output={
                    "hit_count": len(document_hits),
                    "top_hits": [_document_hit_snapshot(hit) for hit in document_hits[:10]],
                },
                metadata={
                    "retry_count": document_retries,
                    "latency_ms": document_latency_ms,
                },
            )

        chunk_started = time.perf_counter()
        with observe(
            "retrieval.chunk_bm25",
            as_type="retriever",
            input={"query": query, "top_k": self.settings.retrieval_bm25_top_k},
            metadata={"retrieval_trace_id": str(trace_id), "signal_level": "chunk"},
        ) as span:
            hits, retries = self._search_with_retry(
                lambda: self.search_adapter.search_chunk_bm25_hits(
                    self.settings.opensearch_chunks_read_alias,
                    query,
                    self.settings.retrieval_bm25_top_k,
                )
            )
            chunk_latency_ms = _elapsed_ms(chunk_started)
            span.update(
                output={
                    "hit_count": len(hits),
                    "top_hits": [_hit_snapshot(hit) for hit in hits[:10]],
                },
                metadata={"retry_count": retries, "latency_ms": chunk_latency_ms},
            )
        return Bm25StageResult(
            hits=hits,
            document_hits=document_hits,
            retries=retries,
            document_retries=document_retries,
            document_latency_ms=document_latency_ms,
            chunk_latency_ms=chunk_latency_ms,
            latency_ms=_elapsed_ms(started),
        )

    def _run_bm25_queries(self, queries: list[str], trace_id: uuid.UUID) -> Bm25StageResult:
        started = time.perf_counter()
        results = [self._run_bm25_stage(query, trace_id) for query in queries]
        hits = (
            results[0].hits
            if len(results) == 1
            else fuse_query_hit_lists(
                [result.hits for result in results],
                rank_constant=self.settings.retrieval_rrf_rank_constant,
                limit=self.settings.retrieval_bm25_top_k,
            )
        )
        document_hits = (
            results[0].document_hits
            if len(results) == 1
            else fuse_document_hit_lists(
                [result.document_hits for result in results],
                rank_constant=self.settings.retrieval_rrf_rank_constant,
                limit=self.settings.retrieval_document_bm25_top_k,
            )
        )
        return Bm25StageResult(
            hits=hits,
            document_hits=document_hits,
            retries=sum(result.retries for result in results),
            document_retries=sum(result.document_retries for result in results),
            document_latency_ms=sum(result.document_latency_ms for result in results),
            chunk_latency_ms=sum(result.chunk_latency_ms for result in results),
            latency_ms=_elapsed_ms(started),
        )

    def _run_dense_stage(self, query: str, trace_id: uuid.UUID) -> DenseStageResult:
        pipeline_started = time.perf_counter()
        with observe(
            "retrieval.dense_pipeline",
            input={"query": query, "top_k": self.settings.retrieval_dense_top_k},
            metadata={"retrieval_trace_id": str(trace_id)},
        ) as pipeline_span:
            embedding_started = time.perf_counter()
            with observe(
                "retrieval.query_embedding",
                as_type="embedding",
                input={"query": query},
                metadata={"retrieval_trace_id": str(trace_id)},
                model=self.embedding_adapter.model,
            ) as embedding_span:
                embedding, embedding_retries = self._embed_query_with_retry(query)
                embedding_latency_ms = _elapsed_ms(embedding_started)
                embedding_span.update(
                    output={
                        "vector_count": len(embedding.vectors),
                        "dimension": len(embedding.vectors[0]) if embedding.vectors else 0,
                    },
                    metadata={
                        "retry_count": embedding_retries,
                        "latency_ms": embedding_latency_ms,
                    },
                    usage_details=(
                        {"total": embedding.total_tokens}
                        if embedding.total_tokens is not None
                        else None
                    ),
                )

            dense_started = time.perf_counter()
            with observe(
                "retrieval.dense",
                as_type="retriever",
                input={"query": query, "top_k": self.settings.retrieval_dense_top_k},
                metadata={"retrieval_trace_id": str(trace_id)},
            ) as dense_span:
                hits, dense_retries = self._search_with_retry(
                    lambda: self.search_adapter.search_dense_hits(
                        self.settings.opensearch_chunks_read_alias,
                        embedding.vectors[0],
                        self.settings.retrieval_dense_top_k,
                        self.settings.voyage_embedding_config_version,
                    )
                )
                dense_latency_ms = _elapsed_ms(dense_started)
                dense_span.update(
                    output={
                        "hit_count": len(hits),
                        "top_hits": [_hit_snapshot(hit) for hit in hits[:10]],
                    },
                    metadata={"retry_count": dense_retries, "latency_ms": dense_latency_ms},
                )

            pipeline_latency_ms = _elapsed_ms(pipeline_started)
            pipeline_span.update(
                output={"hit_count": len(hits)},
                metadata={
                    "latency_ms": pipeline_latency_ms,
                    "embedding_latency_ms": embedding_latency_ms,
                    "dense_latency_ms": dense_latency_ms,
                },
            )
        return DenseStageResult(
            hits=hits,
            embedding=embedding,
            embedding_retries=embedding_retries,
            dense_retries=dense_retries,
            embedding_latency_ms=embedding_latency_ms,
            dense_latency_ms=dense_latency_ms,
            latency_ms=pipeline_latency_ms,
        )

    def _run_dense_queries(self, queries: list[str], trace_id: uuid.UUID) -> DenseStageResult:
        started = time.perf_counter()
        results = [self._run_dense_stage(query, trace_id) for query in queries]
        hits = (
            results[0].hits
            if len(results) == 1
            else fuse_query_hit_lists(
                [result.hits for result in results],
                rank_constant=self.settings.retrieval_rrf_rank_constant,
                limit=self.settings.retrieval_dense_top_k,
            )
        )
        token_values = [result.embedding.total_tokens for result in results]
        total_tokens = (
            sum(value for value in token_values if value is not None)
            if all(value is not None for value in token_values)
            else None
        )
        return DenseStageResult(
            hits=hits,
            embedding=EmbeddingResponse(vectors=[], total_tokens=total_tokens),
            embedding_retries=sum(result.embedding_retries for result in results),
            dense_retries=sum(result.dense_retries for result in results),
            embedding_latency_ms=sum(result.embedding_latency_ms for result in results),
            dense_latency_ms=sum(result.dense_latency_ms for result in results),
            latency_ms=_elapsed_ms(started),
        )

    def _create_trace(
        self,
        request: RetrievalSearchRequest,
        normalized: str,
        rewritten: str,
        rewrite_snapshot: dict[str, object],
        budget: int,
        *,
        graph_requested: bool,
        graph_enabled: bool,
    ) -> uuid.UUID:
        with self.session_factory.begin() as db:
            trace = RetrievalTrace(
                query_original=request.query,
                query_normalized=normalized,
                query_rewritten=rewritten,
                mode=request.mode,
                status=RetrievalTraceStatus.RUNNING,
                config_version=self.settings.retrieval_config_version,
                config_snapshot={
                    **self.config_snapshot,
                    "request_top_k": request.top_k,
                    "effective_final_child_top_k": min(
                        request.top_k or self.settings.retrieval_final_child_top_k,
                        self.settings.retrieval_final_child_top_k,
                    ),
                    "effective_final_context_top_k": min(
                        request.top_k or self.settings.retrieval_final_child_top_k,
                        self.settings.retrieval_final_child_top_k,
                    ),
                    "request_context_budget_tokens": request.context_budget_tokens,
                    "effective_context_budget_tokens": budget,
                    "graph_requested": graph_requested,
                    "graph_enabled_for_request": graph_enabled,
                },
                rewrite_snapshot=rewrite_snapshot,
                embedding_provider=(
                    self.embedding_adapter.provider
                    if request.mode is not RetrievalMode.BM25
                    else None
                ),
                embedding_model=(
                    self.embedding_adapter.model if request.mode is not RetrievalMode.BM25 else None
                ),
                embedding_dimension=(
                    self.embedding_adapter.dimension
                    if request.mode is not RetrievalMode.BM25
                    else None
                ),
                rerank_provider=(
                    self.rerank_adapter.provider
                    if request.mode is RetrievalMode.HYBRID_RERANK
                    else None
                ),
                rerank_model=(
                    self.rerank_adapter.model
                    if request.mode is RetrievalMode.HYBRID_RERANK
                    else None
                ),
                context_budget_tokens=budget,
                started_at=datetime.now(UTC),
            )
            db.add(trace)
            db.flush()
            return trace.id

    def _hydrate_candidates(self, fused: list[FusedRank], queries: list[str]) -> list[Candidate]:
        node_ids: list[uuid.UUID] = []
        for value in fused:
            try:
                node_ids.append(uuid.UUID(value.node_id))
            except ValueError:
                continue
        with self.session_factory() as db:
            nodes = list(
                db.scalars(
                    select(RetrievalNode)
                    .join(DocumentVersion, RetrievalNode.document_version_id == DocumentVersion.id)
                    .join(Document, RetrievalNode.document_id == Document.id)
                    .where(
                        RetrievalNode.id.in_(node_ids),
                        RetrievalNode.node_level.in_(
                            [RetrievalNodeLevel.CHILD, RetrievalNodeLevel.PARENT]
                        ),
                        RetrievalNode.index_status == ProjectionStatus.SUCCEEDED,
                        DocumentVersion.status == VersionStatus.READY,
                        Document.current_version_id == RetrievalNode.document_version_id,
                        Document.status == DocumentStatus.ACTIVE,
                    )
                )
            )
        by_id = {str(node.id): node for node in nodes}
        candidates: list[Candidate] = []
        for rank in fused:
            node = by_id.get(rank.node_id)
            if node is None:
                continue
            candidates.append(
                Candidate(
                    node_id=node.id,
                    document_id=node.document_id,
                    document_version_id=node.document_version_id,
                    parent_node_id=node.parent_node_id,
                    previous_node_id=node.previous_node_id,
                    next_node_id=node.next_node_id,
                    title=node.title,
                    heading_path=node.heading_path,
                    content=node.content,
                    retrieval_text=node.retrieval_text,
                    content_types=node.content_types,
                    source_locators=node.source_locators_json,
                    attributes=node.attributes_json,
                    token_count=node.token_count,
                    embedding=node.embedding_vector,
                    document_rank=rank.document_rank,
                    document_score=rank.document_score,
                    document_rrf_score=rank.document_rrf_score,
                    bm25_rank=rank.bm25_rank,
                    bm25_score=rank.bm25_score,
                    dense_rank=rank.dense_rank,
                    dense_score=rank.dense_score,
                    graph_rank=rank.graph_rank,
                    graph_score=rank.graph_score,
                    graph_path=rank.graph_path,
                    chunk_rrf_score=rank.chunk_rrf_score,
                    rrf_score=rank.rrf_score,
                    exact_match=any(_is_exact_match(query, node) for query in queries),
                )
            )
        return candidates

    def _node_document_ids(self, node_ids: list[str]) -> dict[str, str]:
        parsed: list[uuid.UUID] = []
        for node_id in dict.fromkeys(node_ids):
            try:
                parsed.append(uuid.UUID(node_id))
            except ValueError:
                continue
        if not parsed:
            return {}
        with self.session_factory() as db:
            rows = list(
                db.execute(
                    select(RetrievalNode.id, RetrievalNode.document_id).where(
                        RetrievalNode.id.in_(parsed)
                    )
                )
            )
        return {str(node_id): str(document_id) for node_id, document_id in rows}

    def _load_context_nodes(self, selected: list[Candidate]) -> dict[uuid.UUID, NodeValue]:
        node_ids: set[uuid.UUID] = set()
        parent_ids: set[uuid.UUID] = set()
        for candidate in selected:
            node_ids.add(candidate.node_id)
            if candidate.parent_node_id is not None:
                parent_ids.add(candidate.parent_node_id)
            node_ids.update(
                value
                for value in (
                    candidate.parent_node_id,
                    candidate.previous_node_id,
                    candidate.next_node_id,
                )
                if value is not None
            )
        with self.session_factory() as db:
            conditions = [RetrievalNode.id.in_(node_ids)]
            if parent_ids:
                conditions.append(RetrievalNode.parent_node_id.in_(parent_ids))
            nodes = list(db.scalars(select(RetrievalNode).where(or_(*conditions))))
        return {
            node.id: NodeValue(
                node_id=node.id,
                parent_node_id=node.parent_node_id,
                previous_node_id=node.previous_node_id,
                next_node_id=node.next_node_id,
                title=node.title,
                heading_path=node.heading_path,
                content=node.content,
                content_types=node.content_types,
                source_locators=node.source_locators_json,
                attributes=node.attributes_json,
                token_count=node.token_count,
            )
            for node in nodes
        }

    def _embed_query_with_retry(self, query: str) -> tuple[EmbeddingResponse, int]:
        estimated_tokens = max((len(query) + 3) // 4, 1)
        for retry_count in range(self.settings.voyage_embedding_max_retries + 1):
            try:
                wait_seconds = self.embedding_rate_limiter.reserve(estimated_tokens)
            except (RateLimiterUnavailable, ValueError) as exc:
                raise EmbeddingAdapterError(
                    "VOYAGE_RATE_LIMITER_ERROR",
                    str(exc),
                    retryable=True,
                ) from exc
            if wait_seconds > 0:
                self.sleeper(wait_seconds)
            try:
                return self.embedding_adapter.embed([query], input_type="query"), retry_count
            except EmbeddingAdapterError as exc:
                if not exc.retryable or retry_count >= self.settings.voyage_embedding_max_retries:
                    raise
                if exc.status_code == 429:
                    self.sleeper(
                        exc.retry_after_seconds
                        or self.settings.voyage_embedding_rate_limit_fallback_seconds
                    )
                else:
                    self._sleep_backoff(
                        retry_count,
                        self.settings.voyage_embedding_retry_base_seconds,
                        self.settings.voyage_embedding_retry_max_seconds,
                    )
        raise AssertionError("retry loop must return or raise")

    def _rerank_with_retry(self, query: str, documents: list[str]) -> tuple[RerankResponse, int]:
        for retry_count in range(self.settings.voyage_rerank_max_retries + 1):
            try:
                return (
                    self.rerank_adapter.rerank(query, documents, top_k=len(documents)),
                    retry_count,
                )
            except RerankAdapterError as exc:
                if not exc.retryable or retry_count >= self.settings.voyage_rerank_max_retries:
                    raise
                self._sleep_backoff(
                    retry_count,
                    self.settings.voyage_rerank_retry_base_seconds,
                    self.settings.voyage_rerank_retry_max_seconds,
                )
        raise AssertionError("retry loop must return or raise")

    def _search_with_retry(
        self, operation: Callable[[], list[_SearchResultT]]
    ) -> tuple[list[_SearchResultT], int]:
        for retry_count in range(self.settings.opensearch_max_retries + 1):
            try:
                return operation(), retry_count
            except OpenSearchAdapterError as exc:
                if not exc.retryable or retry_count >= self.settings.opensearch_max_retries:
                    raise
                self._sleep_backoff(
                    retry_count,
                    self.settings.opensearch_retry_base_seconds,
                    self.settings.opensearch_retry_max_seconds,
                )
        raise AssertionError("retry loop must return or raise")

    def _sleep_backoff(self, retry_count: int, base: float, maximum: float) -> None:
        self.sleeper(min(maximum, base * (2**retry_count) * (0.5 + self.jitter())))

    @staticmethod
    def _apply_rerank(candidates: list[Candidate], response: RerankResponse) -> list[Candidate]:
        reranked: list[Candidate] = []
        for item in response.results:
            candidate = candidates[item.index]
            candidate.rerank_score = item.relevance_score
            candidate.selection_reasons.append("reranked")
            reranked.append(candidate)
        return reranked

    @staticmethod
    def _context_representatives(
        context_nodes: list[ContextNodeRead], candidates: list[Candidate]
    ) -> list[Candidate]:
        by_id = {candidate.node_id: candidate for candidate in candidates}
        selected: list[Candidate] = []
        seen: set[uuid.UUID] = set()
        for context in context_nodes:
            representative = next(
                (by_id[child_id] for child_id in context.supporting_child_ids if child_id in by_id),
                None,
            )
            if representative is not None and representative.node_id not in seen:
                selected.append(representative)
                seen.add(representative.node_id)
        return selected

    def _complete_trace(
        self,
        *,
        trace_id: uuid.UUID,
        status: RetrievalTraceStatus,
        stage_values: dict[str, list[dict[str, object]]],
        context_used: int,
        usage: dict[str, object],
        latency: dict[str, object],
        rerank_fallback_reason: str | None,
        graph_query_trace_id: uuid.UUID | None,
        graph_fallback_reason: str | None,
    ) -> None:
        with self.session_factory.begin() as db:
            trace = db.get(RetrievalTrace, trace_id)
            if trace is None:
                raise RuntimeError("Retrieval trace disappeared before completion")
            trace.status = status
            trace.document_candidates_json = stage_values["documents"]
            trace.bm25_candidates_json = stage_values["bm25"]
            trace.dense_candidates_json = stage_values["dense"]
            trace.graph_candidates_json = stage_values["graph"]
            trace.rrf_candidates_json = stage_values["rrf"]
            trace.diversified_candidates_json = stage_values["diversified"]
            trace.reranked_candidates_json = stage_values["reranked"]
            trace.selected_children_json = stage_values["selected"]
            trace.context_nodes_json = stage_values["context"]
            trace.context_used_tokens = context_used
            trace.usage_json = usage
            trace.latency_json = latency
            trace.rerank_fallback_reason = rerank_fallback_reason
            trace.graph_query_trace_id = graph_query_trace_id
            trace.graph_fallback_reason = graph_fallback_reason
            trace.finished_at = datetime.now(UTC)

    def _fail_trace(
        self, trace_id: uuid.UUID, error: dict[str, object], latency: dict[str, object]
    ) -> None:
        with self.session_factory.begin() as db:
            trace = db.get(RetrievalTrace, trace_id)
            if trace is not None:
                trace.status = RetrievalTraceStatus.FAILED
                trace.error = error
                trace.latency_json = latency
                trace.finished_at = datetime.now(UTC)

    @staticmethod
    def _child_read(candidate: Candidate) -> RetrievedChildRead:
        return RetrievedChildRead(
            node_id=candidate.node_id,
            document_id=candidate.document_id,
            document_version_id=candidate.document_version_id,
            parent_node_id=candidate.parent_node_id,
            title=candidate.title,
            heading_path=candidate.heading_path,
            content=candidate.content,
            content_types=candidate.content_types,
            source_locators=candidate.source_locators,
            document_rank=candidate.document_rank,
            document_score=candidate.document_score,
            document_rrf_score=candidate.document_rrf_score,
            bm25_rank=candidate.bm25_rank,
            bm25_score=candidate.bm25_score,
            dense_rank=candidate.dense_rank,
            dense_score=candidate.dense_score,
            graph_rank=candidate.graph_rank,
            graph_score=candidate.graph_score,
            graph_path=candidate.graph_path,
            chunk_rrf_score=candidate.chunk_rrf_score,
            rrf_score=candidate.rrf_score,
            rerank_score=candidate.rerank_score,
            relevance_score=candidate.relevance_score,
            final_rank=candidate.final_rank or 0,
            exact_match=candidate.exact_match,
        )


def build_rerank_adapter(settings: Settings) -> RerankAdapter:
    if settings.voyage_api_key is None:
        return UnavailableRerankAdapter(settings.voyage_rerank_model)
    return VoyageRerankAdapter(
        api_key=settings.voyage_api_key.get_secret_value(),
        model=settings.voyage_rerank_model,
        base_url=settings.voyage_base_url,
        timeout_seconds=settings.voyage_timeout_seconds,
    )


@lru_cache(maxsize=1)
def get_retrieval_service() -> RetrievalService:
    settings = get_settings()
    from robust_rag.graph.factory import get_graph_query_gateway

    return RetrievalService(
        session_factory=SessionLocal,
        search_adapter=get_opensearch_adapter(),
        embedding_adapter=build_embedding_adapter(settings),
        rerank_adapter=build_rerank_adapter(settings),
        query_rewriter=IdentityQueryRewriter(),
        settings=settings,
        graph_retriever=get_graph_query_gateway(),
        embedding_rate_limiter=build_voyage_rate_limiter(settings),
    )


def _hit_snapshot(hit: SearchHit) -> dict[str, object]:
    return {
        "node_id": hit.node_id,
        "document_id": hit.document_id,
        "rank": hit.rank,
        "score": hit.score,
    }


def _document_hit_snapshot(hit: DocumentSearchHit) -> dict[str, object]:
    return {"document_id": hit.document_id, "rank": hit.rank, "score": hit.score}


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _is_exact_match(query: str, node: RetrievalNode) -> bool:
    normalized_query = query.casefold()
    heading = " > ".join(node.heading_path)
    haystack = f"{heading}\n{node.content}".casefold()
    if normalized_query in haystack:
        return True
    identifiers = re.findall(r"(?=\S*[0-9])[\w./-]{3,}", normalized_query)
    return any(re.search(rf"(?<!\w){re.escape(value)}(?!\w)", haystack) for value in identifiers)


def _external_error(
    error: EmbeddingAdapterError | OpenSearchAdapterError | RerankAdapterError,
) -> dict[str, object]:
    return {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
        "status_code": error.status_code,
    }
