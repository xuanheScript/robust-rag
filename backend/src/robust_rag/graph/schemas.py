"""Graph extraction, gateway, and administration contracts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from robust_rag.db.enums import GraphConflictStatus, GraphRunStatus


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
    status: GraphRunStatus
    parent_count: int
    entity_count: int
    relation_count: int
    artifact_uri: str | None
    error: dict[str, object] | None


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
