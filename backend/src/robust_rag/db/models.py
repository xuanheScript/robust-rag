"""Stage 1 persistence models."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from robust_rag.db.base import Base
from robust_rag.db.enums import (
    DocumentStatus,
    JobStatus,
    JobType,
    ParseRunStatus,
    StageName,
    StageRunStatus,
    VersionStatus,
)


def enum_type(enum_class: type[StrEnum], name: str) -> Enum:
    """Build a portable string-backed enum with stable lowercase values."""

    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'deleted')", name="ck_documents_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(500))
    status: Mapped[DocumentStatus] = mapped_column(
        enum_type(DocumentStatus, "document_status"), default=DocumentStatus.ACTIVE, index=True
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "document_versions.id",
            name="fk_documents_current_version_id",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        foreign_keys="DocumentVersion.document_id",
        order_by="DocumentVersion.version_number",
    )
    current_version: Mapped["DocumentVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_version_document_number"),
        UniqueConstraint("document_id", "sha256", name="uq_version_document_sha256"),
        CheckConstraint(
            "status IN ('uploaded', 'parsing', 'cleaning', 'document_evaluating', "
            "'chunking', 'chunk_evaluating', 'embedding', 'indexing', 'ready', "
            "'failed', 'quarantined', 'superseded')",
            name="ck_document_versions_status",
        ),
        Index("ix_document_versions_sha256", "sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    version_number: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(255))
    file_size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_uri: Mapped[str] = mapped_column(Text)
    status: Mapped[VersionStatus] = mapped_column(
        enum_type(VersionStatus, "version_status"), default=VersionStatus.UPLOADED, index=True
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document: Mapped[Document] = relationship(back_populates="versions", foreign_keys=[document_id])
    jobs: Mapped[list["IngestionJob"]] = relationship(
        back_populates="document_version", cascade="all, delete-orphan"
    )
    parse_runs: Mapped[list["ParseRun"]] = relationship(
        back_populates="document_version", cascade="all, delete-orphan"
    )
    canonical_documents: Mapped[list["CanonicalDocumentRecord"]] = relationship(
        back_populates="document_version", cascade="all, delete-orphan"
    )


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint("job_type IN ('ingestion', 'reprocess')", name="ck_jobs_type"),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'quarantined')",
            name="ck_jobs_status",
        ),
        CheckConstraint(
            "current_stage IN ('upload', 'parsing', 'cleaning', 'document_evaluating', "
            "'chunking', 'chunk_evaluating', 'embedding', 'indexing')",
            name="ck_jobs_current_stage",
        ),
        Index("ix_ingestion_jobs_recovery", "status", "current_stage", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    job_type: Mapped[JobType] = mapped_column(
        enum_type(JobType, "job_type"), default=JobType.INGESTION
    )
    status: Mapped[JobStatus] = mapped_column(
        enum_type(JobStatus, "job_status"), default=JobStatus.PENDING, index=True
    )
    current_stage: Mapped[StageName] = mapped_column(
        enum_type(StageName, "stage_name"), default=StageName.UPLOAD
    )
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=8)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    celery_task_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    document_version: Mapped[DocumentVersion] = relationship(back_populates="jobs")
    stage_runs: Mapped[list["StageRun"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="StageRun.created_at"
    )


class StageRun(Base):
    __tablename__ = "stage_runs"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "stage_name",
            "config_version",
            "attempt",
            name="uq_stage_run_idempotency",
        ),
        CheckConstraint(
            "stage_name IN ('upload', 'parsing', 'cleaning', 'document_evaluating', "
            "'chunking', 'chunk_evaluating', 'embedding', 'indexing')",
            name="ck_stage_runs_name",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
            name="ck_stage_runs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), index=True
    )
    stage_name: Mapped[StageName] = mapped_column(enum_type(StageName, "stage_run_name"))
    implementation_name: Mapped[str] = mapped_column(String(255))
    implementation_version: Mapped[str] = mapped_column(String(100))
    config_version: Mapped[str] = mapped_column(String(100))
    config_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[StageRunStatus] = mapped_column(
        enum_type(StageRunStatus, "stage_run_status"), default=StageRunStatus.PENDING
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    input_artifact_uri: Mapped[str | None] = mapped_column(Text)
    output_artifact_uri: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped[IngestionJob] = relationship(back_populates="stage_runs")


class ParseRun(Base):
    __tablename__ = "parse_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_parse_runs_status"
        ),
        Index("ix_parse_runs_version_status", "document_version_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    parser_name: Mapped[str] = mapped_column(String(255))
    parser_version: Mapped[str] = mapped_column(String(100))
    parser_mode: Mapped[str] = mapped_column(String(100))
    parser_config: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[ParseRunStatus] = mapped_column(
        enum_type(ParseRunStatus, "parse_run_status"), default=ParseRunStatus.RUNNING
    )
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)

    document_version: Mapped[DocumentVersion] = relationship(back_populates="parse_runs")
    canonical_document: Mapped["CanonicalDocumentRecord | None"] = relationship(
        back_populates="parse_run", uselist=False
    )


class CanonicalDocumentRecord(Base):
    __tablename__ = "canonical_documents"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "schema_version", name="uq_canonical_version_schema"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    parse_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("parse_runs.id", ondelete="CASCADE"), unique=True
    )
    schema_version: Mapped[str] = mapped_column(String(50))
    artifact_uri: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(50))
    title: Mapped[str | None] = mapped_column(String(1000))
    block_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document_version: Mapped[DocumentVersion] = relationship(back_populates="canonical_documents")
    parse_run: Mapped[ParseRun] = relationship(back_populates="canonical_document")
