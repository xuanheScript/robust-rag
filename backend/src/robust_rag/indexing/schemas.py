"""API contracts for embedding/indexing audit and projection administration."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from robust_rag.db.enums import EmbeddingBatchStatus, ProjectionRunStatus


class EmbeddingBatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    batch_index: int
    node_ids_json: list[str]
    input_count: int
    estimated_tokens: int
    provider_tokens: int | None
    retry_count: int
    status: EmbeddingBatchStatus
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, object] | None


class EmbeddingRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_version_id: uuid.UUID
    chunking_run_id: uuid.UUID
    provider: str
    model: str
    dimension: int
    config_version: str
    config_snapshot: dict[str, object]
    status: ProjectionRunStatus
    input_count: int
    batch_count: int
    estimated_tokens: int
    provider_tokens: int | None
    estimated_cost_usd: float | None
    started_at: datetime
    finished_at: datetime | None
    error: dict[str, object] | None
    batches: list[EmbeddingBatchRead]


class IndexingRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_version_id: uuid.UUID
    embedding_run_id: uuid.UUID
    documents_index: str
    chunks_index: str
    documents_read_alias: str
    chunks_read_alias: str
    chunks_write_alias: str
    config_version: str
    config_snapshot: dict[str, object]
    capability_snapshot: dict[str, object]
    status: ProjectionRunStatus
    expected_document_count: int
    expected_node_count: int
    indexed_document_count: int | None
    indexed_node_count: int | None
    started_at: datetime
    finished_at: datetime | None
    error: dict[str, object] | None


class SearchCapabilitiesRead(BaseModel):
    version: str
    plugins: list[str]
    knn_available: bool
    icu_available: bool
    neural_search_available: bool


class ProjectionRebuildRequest(BaseModel):
    document_id: uuid.UUID | None = None


class ProjectionMutationResponse(BaseModel):
    documents: int = 0
    nodes: int = 0
    versions: int = 0


class DocumentDeletionResponse(BaseModel):
    document_id: uuid.UUID
    status: str
    versions: int
    nodes: int


class DocumentRestoreResponse(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    status: str
    documents: int
    nodes: int
    graph_entities: int = 0
    graph_facts: int = 0
    graph_evidences: int = 0
    graph_warning: str | None = None


class DocumentPurgeRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=500)


class DocumentPurgeResponse(BaseModel):
    document_id: uuid.UUID
    status: str
    versions: int
    nodes: int
    graph_versions: int = 0
    graph_evidences: int = 0
    graph_facts: int = 0
    artifacts_deleted: int
    artifact_errors: list[str] = Field(default_factory=list)


class AliasSwitchRequest(BaseModel):
    documents_index: str = Field(min_length=1, max_length=255, pattern=r"^[a-z0-9._-]+$")
    chunks_index: str = Field(min_length=1, max_length=255, pattern=r"^[a-z0-9._-]+$")


class AliasSwitchResponse(BaseModel):
    documents_index: str
    chunks_index: str
