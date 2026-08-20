"""Graph extraction, gateway, and administration contracts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from robust_rag.db.enums import (
    GraphBuildRequestStatus,
    GraphBuildRequestType,
    GraphConflictStatus,
    GraphRunStatus,
)


class GraphBuildSelection(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    force: bool = False


class GraphBuildCreateRequest(GraphBuildSelection):
    requested_by: str = Field(default="local-admin", min_length=1, max_length=255)


class GraphBuildPreviewItem(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID | None = None
    display_name: str
    graph_status: str | None = None
    graph_active: bool = False
    eligible: bool
    reason: str | None = None
    parent_count: int = 0
    estimated_input_tokens: int = 0


class GraphBuildPreviewResponse(BaseModel):
    items: list[GraphBuildPreviewItem]
    eligible_count: int
    parent_count: int
    estimated_calls: int
    estimated_input_tokens: int
    estimated_input_cost_usd: float | None = None


class GraphBuildRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    batch_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    request_type: GraphBuildRequestType
    status: GraphBuildRequestStatus
    requested_by: str
    force: bool
    projection_was_active: bool
    celery_task_id: str | None
    parent_count: int
    estimated_input_tokens: int
    estimated_input_cost_usd: float | None
    actual_input_tokens: int | None
    actual_output_tokens: int | None
    actual_total_tokens: int | None
    actual_cost_usd: float | None
    attempt: int
    max_attempts: int
    previous_graph_status: str
    error: dict[str, object] | None
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class GraphBuildBatchResponse(BaseModel):
    batch_id: uuid.UUID
    requests: list[GraphBuildRequestRead]


class ExtractedEntity(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class ExtractedTriplet(BaseModel):
    subject: ExtractedEntity
    predicate: str
    object: ExtractedEntity
    confidence: float | None = Field(default=None, ge=0, le=1)
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphExtractionArtifact(BaseModel):
    schema_version: str
    input_hash: str
    triplets_by_source: dict[str, list[ExtractedTriplet]]
    rejected_candidates: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class GraphParentOutcome:
    source_node_id: str | None
    status: Literal["succeeded", "failed"]
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    status_code: int | None = None
    attempt_count: int = 1
    candidate_triplet_count: int = 0
    accepted_triplet_count: int = 0
    candidate_type_combinations: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "source_node_id": self.source_node_id,
                "status": self.status,
                "latency_ms": self.latency_ms,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
                "response_id": self.response_id,
                "finish_reason": self.finish_reason,
                "error_code": self.error_code,
                "error_type": self.error_type,
                "error_message": self.error_message,
                "retryable": self.retryable,
                "status_code": self.status_code,
                "attempt_count": self.attempt_count,
                "candidate_triplet_count": self.candidate_triplet_count,
                "accepted_triplet_count": self.accepted_triplet_count,
                "rejected_triplet_count": max(
                    0, self.candidate_triplet_count - self.accepted_triplet_count
                ),
                "candidate_type_combinations": list(self.candidate_type_combinations),
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class GraphExtractionBatch:
    triplets_by_source: dict[str, list[ExtractedTriplet]]
    parent_outcomes: list[GraphParentOutcome] = field(default_factory=list)


@dataclass(frozen=True)
class GraphSearchHit:
    node_id: str
    rank: int
    score: float
    path: list[dict[str, object]] = field(default_factory=list)

    def snapshot(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "rank": self.rank,
            "score": self.score,
            "path": self.path,
        }


@dataclass(frozen=True)
class GraphQueryResult:
    trace_id: uuid.UUID
    hits: list[GraphSearchHit]
    fallback_reason: str | None = None


class GraphEntityCreate(BaseModel):
    entity_type: str
    primary_name: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=3, max_length=2000)


class GraphEntityUpdate(BaseModel):
    primary_name: str | None = Field(default=None, min_length=1, max_length=500)
    aliases: list[str] | None = None
    properties: dict[str, Any] | None = None
    reason: str = Field(min_length=3, max_length=2000)


class GraphEntityMergeRequest(BaseModel):
    target_entity_id: uuid.UUID
    source_entity_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=3, max_length=2000)


class GraphEntitySplitRequest(BaseModel):
    entity_type: str
    primary_name: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list)
    fact_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=3, max_length=2000)


class GraphFactCreate(BaseModel):
    subject_entity_id: uuid.UUID
    predicate: str
    object_entity_id: uuid.UUID
    properties: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=3, max_length=2000)


class GraphFactUpdate(BaseModel):
    subject_entity_id: uuid.UUID | None = None
    predicate: str | None = None
    object_entity_id: uuid.UUID | None = None
    properties: dict[str, Any] | None = None
    reason: str = Field(min_length=3, max_length=2000)


class GraphReviewRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class GraphConflictResolveRequest(BaseModel):
    resolution: str = Field(min_length=3, max_length=2000)
    actor: str = Field(default="local-admin", min_length=1, max_length=255)


class GraphEntityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    canonical_key: str
    entity_type: str
    primary_name: str
    normalized_name: str
    aliases_json: list[str]
    properties_json: dict[str, Any]
    origin: str
    review_status: str
    schema_version: str
    manual_lock: bool


class GraphFactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject_entity_id: uuid.UUID
    predicate: str
    object_entity_id: uuid.UUID
    properties_json: dict[str, Any]
    origin: str
    confidence: float | None
    review_status: str
    schema_version: str
    manual_lock: bool
    active: bool


class GraphEvidenceRead(BaseModel):
    fact_id: uuid.UUID
    source_node_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    source_locators: list[dict[str, Any]]
    excerpt: str


class DocumentGraphRead(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    parent_count: int
    entities: list[GraphEntityRead]
    facts: list[GraphFactRead]
    evidence: list[GraphEvidenceRead]


class GraphExtractionRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_version_id: uuid.UUID
    schema_version: str
    extractor_name: str
    extractor_version: str
    model: str
    prompt_version: str
    input_hash: str
    attempt: int
    status: GraphRunStatus
    parent_count: int
    entity_count: int
    relation_count: int
    artifact_uri: str | None
    usage_json: dict[str, object]
    error: dict[str, object] | None
    started_at: datetime
    finished_at: datetime | None


class GraphRebuildResponse(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    status: Literal["queued"] = "queued"
    task_id: str


class GraphConflictRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    extraction_run_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    conflict_type: str
    current_json: dict[str, object]
    proposed_json: dict[str, object]
    status: GraphConflictStatus
    resolution_json: dict[str, object]
    resolved_by: str | None
