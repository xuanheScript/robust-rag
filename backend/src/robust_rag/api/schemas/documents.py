"""Document and ingestion job response models."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from robust_rag.db.enums import (
    DocumentStatus,
    JobStatus,
    JobType,
    ParseRunStatus,
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


class JobListResponse(BaseModel):
    items: list[JobRead]
    total: int
