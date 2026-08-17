"""Document and ingestion job response models."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from robust_rag.db.enums import (
    ChunkingRunStatus,
    CleaningRunStatus,
    DocumentStatus,
    GraphProjectionStatus,
    JobStatus,
    JobType,
    ParseRunStatus,
    ProjectionStatus,
    QualityAssessmentStatus,
    QualityDecisionValue,
    QualityReviewActionValue,
    RetrievalNodeLevel,
    StageName,
    StageRunStatus,
    VersionStatus,
)


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    status: DocumentStatus
    current_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class DocumentVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    version_number: int
    original_filename: str
    mime_type: str
    file_size: int
    sha256: str
    storage_uri: str
    status: VersionStatus
    uploaded_at: datetime
    ready_at: datetime | None
    superseded_at: datetime | None
    graph_status: GraphProjectionStatus
    graph_schema_version: str | None
    graph_projected_at: datetime | None


class StageRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    stage_name: StageName
    implementation_name: str
    implementation_version: str
    config_version: str
    status: StageRunStatus
    attempt: int
    input_artifact_uri: str | None
    output_artifact_uri: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, object] | None
    created_at: datetime


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_version_id: uuid.UUID
    job_type: JobType
    status: JobStatus
    current_stage: StageName
    progress_current: int
    progress_total: int
    attempt: int
    celery_task_id: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobDetail(JobRead):
    stage_runs: list[StageRunRead]


class UploadResponse(BaseModel):
    document: DocumentRead
    version: DocumentVersionRead
    job: JobRead
    warnings: list[str]


class DocumentListResponse(BaseModel):
    items: list[DocumentRead]
    total: int


class ParseRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_version_id: uuid.UUID
    parser_name: str
    parser_version: str
    parser_mode: str
    parser_config: dict[str, object]
    status: ParseRunStatus
    artifact_uri: str | None
    started_at: datetime
    finished_at: datetime | None
    error: dict[str, object] | None


class CanonicalDocumentRecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_version_id: uuid.UUID
    parse_run_id: uuid.UUID
    schema_version: str
    artifact_uri: str
    language: str | None
    title: str | None
    block_count: int
    content_hash: str
    created_at: datetime


class CleaningRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_version_id: uuid.UUID
    canonical_document_id: uuid.UUID
    pipeline_name: str
    pipeline_version: str
    config_version: str
    config_snapshot: dict[str, object]
    status: CleaningRunStatus
    input_artifact_uri: str
    output_artifact_uri: str | None
    report_artifact_uri: str | None
    input_content_hash: str
    output_content_hash: str | None
    input_block_count: int
    output_block_count: int | None
    changed_block_count: int | None
    removed_block_count: int | None
    issue_count: int | None
    operator_executions: list[dict[str, object]]
    started_at: datetime
    finished_at: datetime | None
    error: dict[str, object] | None


class QualityAssessmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_version_id: uuid.UUID
    cleaning_run_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    evaluator: str
    evaluator_version: str
    engine_version: str
    rule_set_version: str
    policy_version: str
    config_snapshot: dict[str, object]
    model: str | None
    prompt_version: str | None
    status: QualityAssessmentStatus
    decision: QualityDecisionValue | None
    overall_score: float | None
    dimensions_json: list[dict[str, object]]
    issues_json: list[dict[str, object]]
    evaluator_executions_json: list[dict[str, object]]
    raw_result_uri: str | None
    input_content_hash: str
    started_at: datetime
    finished_at: datetime | None
    error: dict[str, object] | None


class QualityReviewActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_version_id: uuid.UUID
    assessment_id: uuid.UUID
    action: QualityReviewActionValue
    actor: str
    reason: str
    previous_job_status: str
    previous_version_status: str
    previous_decision: QualityDecisionValue
    quality_snapshot: dict[str, object]
    created_at: datetime


class QualityReviewResponse(BaseModel):
    action: QualityReviewActionRead
    job: JobRead


class ChunkingRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_version_id: uuid.UUID
    canonical_document_id: uuid.UUID
    cleaning_run_id: uuid.UUID
    quality_assessment_id: uuid.UUID
    chunker_name: str
    chunker_version: str
    config_version: str
    config_snapshot: dict[str, object]
    status: ChunkingRunStatus
    input_artifact_uri: str
    artifact_uri: str | None
    report_artifact_uri: str | None
    input_content_hash: str
    parent_count: int | None
    child_count: int | None
    total_tokens: int | None
    started_at: datetime
    finished_at: datetime | None
    error: dict[str, object] | None


class RetrievalNodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_id: uuid.UUID = Field(validation_alias="id")
    chunking_run_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    canonical_document_id: uuid.UUID
    node_level: RetrievalNodeLevel
    parent_node_id: uuid.UUID | None
    previous_node_id: uuid.UUID | None
    next_node_id: uuid.UUID | None
    title: str | None
    heading_path: list[str]
    content: str
    retrieval_text: str
    source_locators_json: list[dict[str, object]]
    source_block_ids: list[str]
    content_types: list[str]
    language: str | None
    token_count: int
    quality_status: QualityDecisionValue
    quality_summary_json: dict[str, object]
    chunker_name: str
    chunker_version: str
    chunking_config_version: str
    retrieval_text_hash: str
    attributes_json: dict[str, object]
    embedding_status: ProjectionStatus
    index_status: ProjectionStatus
    embedding_provider: str | None
    embedding_model: str | None
    embedding_dimension: int | None
    embedding_config_version: str | None
    embedded_at: datetime | None
    created_at: datetime


class JobListResponse(BaseModel):
    items: list[JobRead]
    total: int
