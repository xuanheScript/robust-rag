"""Internal and API contracts for stage 7 retrieval."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from robust_rag.db.enums import RetrievalMode, RetrievalTraceStatus


@dataclass
class Candidate:
    node_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    parent_node_id: uuid.UUID | None
    previous_node_id: uuid.UUID | None
    next_node_id: uuid.UUID | None
    title: str | None
    heading_path: list[str]
    content: str
    retrieval_text: str
    content_types: list[str]
    source_locators: list[dict[str, object]]
    attributes: dict[str, object]
    token_count: int
    bm25_rank: int | None = None
    bm25_score: float | None = None
    dense_rank: int | None = None
    dense_score: float | None = None
    graph_rank: int | None = None
    graph_score: float | None = None
    graph_path: list[dict[str, object]] = field(default_factory=list)
    rrf_score: float = 0
    rerank_score: float | None = None
    exact_match: bool = False
    final_rank: int | None = None
    selection_reasons: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, object]:
        return {
            "node_id": str(self.node_id),
            "document_id": str(self.document_id),
            "document_version_id": str(self.document_version_id),
            "parent_node_id": str(self.parent_node_id) if self.parent_node_id else None,
            "bm25_rank": self.bm25_rank,
            "bm25_score": self.bm25_score,
            "dense_rank": self.dense_rank,
            "dense_score": self.dense_score,
            "graph_rank": self.graph_rank,
            "graph_score": self.graph_score,
            "graph_path": self.graph_path,
            "rrf_score": self.rrf_score,
            "rerank_score": self.rerank_score,
            "exact_match": self.exact_match,
            "final_rank": self.final_rank,
            "selection_reasons": self.selection_reasons,
            "title": self.title,
            "heading_path": self.heading_path,
            "content_types": self.content_types,
        }

    def rerank_text(self) -> str:
        values = [
            f"Title: {self.title}" if self.title else "",
            f"Heading: {' > '.join(self.heading_path)}" if self.heading_path else "",
            f"Content types: {', '.join(self.content_types)}" if self.content_types else "",
            self.content,
        ]
        return "\n".join(value for value in values if value)


@dataclass(frozen=True)
class NodeValue:
    node_id: uuid.UUID
    parent_node_id: uuid.UUID | None
    previous_node_id: uuid.UUID | None
    next_node_id: uuid.UUID | None
    title: str | None
    heading_path: list[str]
    content: str
    content_types: list[str]
    source_locators: list[dict[str, object]]
    attributes: dict[str, object]
    token_count: int


class RetrievalSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10000)
    mode: RetrievalMode = RetrievalMode.HYBRID_RERANK
    top_k: int | None = Field(default=None, ge=1, le=100)
    context_budget_tokens: int | None = Field(default=None, ge=1, le=100000)
    debug: bool = False


class RetrievedChildRead(BaseModel):
    node_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    parent_node_id: uuid.UUID | None
    title: str | None
    heading_path: list[str]
    content: str
    content_types: list[str]
    source_locators: list[dict[str, object]]
    bm25_rank: int | None
    bm25_score: float | None
    dense_rank: int | None
    dense_score: float | None
    graph_rank: int | None
    graph_score: float | None
    graph_path: list[dict[str, object]]
    rrf_score: float
    rerank_score: float | None
    final_rank: int
    exact_match: bool


class ContextNodeRead(BaseModel):
    node_id: uuid.UUID
    role: str
    reason: str
    supporting_child_ids: list[uuid.UUID]
    title: str | None
    heading_path: list[str]
    content: str
    content_types: list[str]
    source_locators: list[dict[str, object]]
    token_count: int


class RetrievalSearchResponse(BaseModel):
    trace_id: uuid.UUID
    status: RetrievalTraceStatus
    mode: RetrievalMode
    query_original: str
    query_normalized: str
    query_rewritten: str
    children: list[RetrievedChildRead]
    context_nodes: list[ContextNodeRead]
    context_budget_tokens: int
    context_used_tokens: int
    rerank_fallback_reason: str | None
    graph_query_trace_id: uuid.UUID | None
    graph_fallback_reason: str | None
    usage: dict[str, object]
    latency_ms: dict[str, object]
    debug: dict[str, object] | None = None


class RetrievalTraceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    query_original: str
    query_normalized: str
    query_rewritten: str
    mode: RetrievalMode
    status: RetrievalTraceStatus
    config_version: str
    config_snapshot: dict[str, object]
    rewrite_snapshot: dict[str, object]
    embedding_provider: str | None
    embedding_model: str | None
    embedding_dimension: int | None
    rerank_provider: str | None
    rerank_model: str | None
    rerank_fallback_reason: str | None
    graph_query_trace_id: uuid.UUID | None
    graph_fallback_reason: str | None
    bm25_candidates_json: list[dict[str, object]]
    dense_candidates_json: list[dict[str, object]]
    graph_candidates_json: list[dict[str, object]]
    rrf_candidates_json: list[dict[str, object]]
    diversified_candidates_json: list[dict[str, object]]
    reranked_candidates_json: list[dict[str, object]]
    selected_children_json: list[dict[str, object]]
    context_nodes_json: list[dict[str, object]]
    context_budget_tokens: int
    context_used_tokens: int
    usage_json: dict[str, object]
    latency_json: dict[str, object]
    started_at: datetime
    finished_at: datetime | None
    error: dict[str, object] | None
