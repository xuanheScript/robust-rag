"""Parser-neutral contracts for parent/child retrieval nodes."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from robust_rag.db.enums import RetrievalNodeLevel
from robust_rag.parsing.schemas import SourceLocator
from robust_rag.quality.schemas import QualityDecision

RETRIEVAL_NODE_SCHEMA_VERSION = "retrieval-node/1.0"
CHUNKING_ARTIFACT_SCHEMA_VERSION = "chunking-artifact/1.0"
CHUNKING_REPORT_SCHEMA_VERSION = "chunking-report/1.0"


class RetrievalNodeData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = RETRIEVAL_NODE_SCHEMA_VERSION
    node_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    canonical_document_id: uuid.UUID
    node_level: RetrievalNodeLevel
    parent_node_id: uuid.UUID | None = None
    previous_node_id: uuid.UUID | None = None
    next_node_id: uuid.UUID | None = None
    title: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    content: str
    retrieval_text: str
    source_locators: list[SourceLocator] = Field(default_factory=list)
    source_block_ids: list[str] = Field(default_factory=list)
    content_types: list[str] = Field(default_factory=list)
    language: str | None = None
    token_count: int = Field(ge=0)
    quality_status: QualityDecision
    quality_summary: dict[str, Any] = Field(default_factory=dict)
    chunker_name: str
    chunker_version: str
    chunking_config_version: str
    retrieval_text_hash: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class ChunkingArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CHUNKING_ARTIFACT_SCHEMA_VERSION
    chunking_run_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    canonical_document_id: uuid.UUID
    cleaning_run_id: uuid.UUID
    quality_assessment_id: uuid.UUID
    chunker_name: str
    chunker_version: str
    config_version: str
    config_snapshot: dict[str, Any]
    input_content_hash: str
    nodes: list[RetrievalNodeData]


class ChunkingReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CHUNKING_REPORT_SCHEMA_VERSION
    chunking_run_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    canonical_document_id: uuid.UUID
    cleaning_run_id: uuid.UUID
    quality_assessment_id: uuid.UUID
    chunker_name: str
    chunker_version: str
    config_version: str
    config_snapshot: dict[str, Any]
    input_content_hash: str
    parent_count: int = Field(ge=0)
    child_count: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    source_block_count: int = Field(ge=0)
