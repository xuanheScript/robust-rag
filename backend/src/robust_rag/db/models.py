"""Persistent business facts and ingestion-stage audit models."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
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
    ChunkingRunStatus,
    CleaningRunStatus,
    ConversationStatus,
    DocumentStatus,
    EmbeddingBatchStatus,
    EvaluationRunStatus,
    EvaluationSampleStatus,
    GraphConflictStatus,
    GraphCorrectionAction,
    GraphOrigin,
    GraphProjectionStatus,
    GraphQueryTraceStatus,
    GraphReviewStatus,
    GraphRunStatus,
    JobStatus,
    JobType,
    MessageRole,
    MessageStatus,
    ModelInvocationStatus,
    ParseRunStatus,
    ProjectionRunStatus,
    ProjectionStatus,
    QualityAssessmentStatus,
    QualityDecisionValue,
    QualityReviewActionValue,
    RetrievalMode,
    RetrievalNodeLevel,
    RetrievalTraceStatus,
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
    graph_status: Mapped[GraphProjectionStatus] = mapped_column(
        enum_type(GraphProjectionStatus, "document_version_graph_status"),
        default=GraphProjectionStatus.PENDING,
        index=True,
    )
    graph_schema_version: Mapped[str | None] = mapped_column(String(100))
    graph_projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    cleaning_runs: Mapped[list["CleaningRun"]] = relationship(
        back_populates="document_version", cascade="all, delete-orphan"
    )
    quality_assessments: Mapped[list["QualityAssessment"]] = relationship(
        back_populates="document_version", cascade="all, delete-orphan"
    )
    quality_review_actions: Mapped[list["QualityReviewAction"]] = relationship(
        back_populates="document_version", cascade="all, delete-orphan"
    )
    chunking_runs: Mapped[list["ChunkingRun"]] = relationship(
        back_populates="document_version", cascade="all, delete-orphan"
    )
    retrieval_nodes: Mapped[list["RetrievalNode"]] = relationship(
        back_populates="document_version", cascade="all, delete-orphan"
    )
    embedding_runs: Mapped[list["EmbeddingRun"]] = relationship(
        back_populates="document_version", cascade="all, delete-orphan"
    )
    indexing_runs: Mapped[list["IndexingRun"]] = relationship(
        back_populates="document_version", cascade="all, delete-orphan"
    )
    graph_extraction_runs: Mapped[list["GraphExtractionRun"]] = relationship(
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
    cleaning_runs: Mapped[list["CleaningRun"]] = relationship(
        back_populates="canonical_document", cascade="all, delete-orphan"
    )
    chunking_runs: Mapped[list["ChunkingRun"]] = relationship(
        back_populates="canonical_document", cascade="all, delete-orphan"
    )


class CleaningRun(Base):
    __tablename__ = "cleaning_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_cleaning_runs_status"
        ),
        Index(
            "ix_cleaning_runs_idempotency",
            "canonical_document_id",
            "pipeline_version",
            "config_version",
            "input_content_hash",
        ),
        Index("ix_cleaning_runs_version_status", "document_version_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    canonical_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_documents.id", ondelete="CASCADE"), index=True
    )
    pipeline_name: Mapped[str] = mapped_column(String(255))
    pipeline_version: Mapped[str] = mapped_column(String(100))
    config_version: Mapped[str] = mapped_column(String(100))
    config_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[CleaningRunStatus] = mapped_column(
        enum_type(CleaningRunStatus, "cleaning_run_status"),
        default=CleaningRunStatus.RUNNING,
    )
    input_artifact_uri: Mapped[str] = mapped_column(Text)
    output_artifact_uri: Mapped[str | None] = mapped_column(Text)
    report_artifact_uri: Mapped[str | None] = mapped_column(Text)
    input_content_hash: Mapped[str] = mapped_column(String(64))
    output_content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    input_block_count: Mapped[int] = mapped_column(Integer)
    output_block_count: Mapped[int | None] = mapped_column(Integer)
    changed_block_count: Mapped[int | None] = mapped_column(Integer)
    removed_block_count: Mapped[int | None] = mapped_column(Integer)
    issue_count: Mapped[int | None] = mapped_column(Integer)
    operator_executions: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)

    document_version: Mapped[DocumentVersion] = relationship(back_populates="cleaning_runs")
    canonical_document: Mapped[CanonicalDocumentRecord] = relationship(
        back_populates="cleaning_runs"
    )
    quality_assessments: Mapped[list["QualityAssessment"]] = relationship(
        back_populates="cleaning_run", cascade="all, delete-orphan"
    )
    chunking_runs: Mapped[list["ChunkingRun"]] = relationship(
        back_populates="cleaning_run", cascade="all, delete-orphan"
    )


class QualityAssessment(Base):
    __tablename__ = "quality_assessments"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('document', 'retrieval_node')",
            name="ck_quality_assessments_target_type",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_quality_assessments_status",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('passed', 'warning', 'quarantined', 'rejected')",
            name="ck_quality_assessments_decision",
        ),
        Index(
            "ix_quality_assessments_idempotency",
            "cleaning_run_id",
            "engine_version",
            "rule_set_version",
            "policy_version",
            "input_content_hash",
        ),
        Index(
            "ix_quality_assessments_version_status",
            "document_version_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    cleaning_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cleaning_runs.id", ondelete="CASCADE"), index=True
    )
    target_type: Mapped[str] = mapped_column(String(50), default="document")
    target_id: Mapped[uuid.UUID] = mapped_column(index=True)
    evaluator: Mapped[str] = mapped_column(String(255))
    evaluator_version: Mapped[str] = mapped_column(String(100))
    engine_version: Mapped[str] = mapped_column(String(100))
    rule_set_version: Mapped[str] = mapped_column(String(100))
    policy_version: Mapped[str] = mapped_column(String(100))
    config_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_version: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[QualityAssessmentStatus] = mapped_column(
        enum_type(QualityAssessmentStatus, "quality_assessment_status"),
        default=QualityAssessmentStatus.RUNNING,
    )
    decision: Mapped[QualityDecisionValue | None] = mapped_column(
        enum_type(QualityDecisionValue, "quality_decision"), nullable=True
    )
    overall_score: Mapped[float | None] = mapped_column(Float)
    dimensions_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    issues_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    evaluator_executions_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    raw_result_uri: Mapped[str | None] = mapped_column(Text)
    input_content_hash: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)

    document_version: Mapped[DocumentVersion] = relationship(back_populates="quality_assessments")
    cleaning_run: Mapped[CleaningRun] = relationship(back_populates="quality_assessments")
    review_actions: Mapped[list["QualityReviewAction"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )
    chunking_runs: Mapped[list["ChunkingRun"]] = relationship(back_populates="quality_assessment")


class QualityReviewAction(Base):
    __tablename__ = "quality_review_actions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('release', 'reject', 'reevaluate')",
            name="ck_quality_review_actions_action",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_assessments.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[QualityReviewActionValue] = mapped_column(
        enum_type(QualityReviewActionValue, "quality_review_action")
    )
    actor: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(Text)
    previous_job_status: Mapped[str] = mapped_column(String(50))
    previous_version_status: Mapped[str] = mapped_column(String(50))
    previous_decision: Mapped[QualityDecisionValue] = mapped_column(
        enum_type(QualityDecisionValue, "quality_review_previous_decision")
    )
    quality_snapshot: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document_version: Mapped[DocumentVersion] = relationship(
        back_populates="quality_review_actions"
    )
    assessment: Mapped[QualityAssessment] = relationship(back_populates="review_actions")


class ChunkingRun(Base):
    __tablename__ = "chunking_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_chunking_runs_status",
        ),
        Index(
            "ix_chunking_runs_idempotency",
            "cleaning_run_id",
            "chunker_name",
            "chunker_version",
            "config_version",
            "input_content_hash",
        ),
        Index("ix_chunking_runs_version_status", "document_version_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    canonical_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_documents.id", ondelete="CASCADE"), index=True
    )
    cleaning_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cleaning_runs.id", ondelete="CASCADE"), index=True
    )
    quality_assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_assessments.id", ondelete="CASCADE"), index=True
    )
    chunker_name: Mapped[str] = mapped_column(String(255))
    chunker_version: Mapped[str] = mapped_column(String(100))
    config_version: Mapped[str] = mapped_column(String(100))
    config_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[ChunkingRunStatus] = mapped_column(
        enum_type(ChunkingRunStatus, "chunking_run_status"),
        default=ChunkingRunStatus.RUNNING,
    )
    input_artifact_uri: Mapped[str] = mapped_column(Text)
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    report_artifact_uri: Mapped[str | None] = mapped_column(Text)
    input_content_hash: Mapped[str] = mapped_column(String(64), index=True)
    parent_count: Mapped[int | None] = mapped_column(Integer)
    child_count: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)

    document_version: Mapped[DocumentVersion] = relationship(back_populates="chunking_runs")
    canonical_document: Mapped[CanonicalDocumentRecord] = relationship(
        back_populates="chunking_runs"
    )
    cleaning_run: Mapped[CleaningRun] = relationship(back_populates="chunking_runs")
    quality_assessment: Mapped[QualityAssessment] = relationship(back_populates="chunking_runs")
    retrieval_nodes: Mapped[list["RetrievalNode"]] = relationship(
        back_populates="chunking_run", cascade="all, delete-orphan"
    )
    embedding_runs: Mapped[list["EmbeddingRun"]] = relationship(
        back_populates="chunking_run", cascade="all, delete-orphan"
    )


class RetrievalNode(Base):
    __tablename__ = "retrieval_nodes"
    __table_args__ = (
        CheckConstraint("node_level IN ('parent', 'child')", name="ck_retrieval_nodes_level"),
        CheckConstraint(
            "quality_status IN ('passed', 'warning', 'quarantined', 'rejected')",
            name="ck_retrieval_nodes_quality_status",
        ),
        CheckConstraint(
            "embedding_status IN ('pending', 'succeeded', 'failed', 'stale')",
            name="ck_retrieval_nodes_embedding_status",
        ),
        CheckConstraint(
            "index_status IN ('pending', 'succeeded', 'failed', 'stale')",
            name="ck_retrieval_nodes_index_status",
        ),
        Index(
            "ix_retrieval_nodes_version_level",
            "document_version_id",
            "node_level",
        ),
        Index("ix_retrieval_nodes_parent", "parent_node_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    chunking_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunking_runs.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    canonical_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_documents.id", ondelete="CASCADE"), index=True
    )
    node_level: Mapped[RetrievalNodeLevel] = mapped_column(
        enum_type(RetrievalNodeLevel, "retrieval_node_level")
    )
    parent_node_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    previous_node_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    next_node_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    title: Mapped[str | None] = mapped_column(String(1000))
    heading_path: Mapped[list[str]] = mapped_column(JSON, default=list)
    content: Mapped[str] = mapped_column(Text)
    retrieval_text: Mapped[str] = mapped_column(Text)
    source_locators_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    source_block_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    content_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    language: Mapped[str | None] = mapped_column(String(50))
    token_count: Mapped[int] = mapped_column(Integer)
    quality_status: Mapped[QualityDecisionValue] = mapped_column(
        enum_type(QualityDecisionValue, "retrieval_node_quality_status")
    )
    quality_summary_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    chunker_name: Mapped[str] = mapped_column(String(255))
    chunker_version: Mapped[str] = mapped_column(String(100))
    chunking_config_version: Mapped[str] = mapped_column(String(100))
    retrieval_text_hash: Mapped[str] = mapped_column(String(64), index=True)
    attributes_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    embedding_status: Mapped[ProjectionStatus] = mapped_column(
        enum_type(ProjectionStatus, "retrieval_node_embedding_status"),
        default=ProjectionStatus.PENDING,
    )
    index_status: Mapped[ProjectionStatus] = mapped_column(
        enum_type(ProjectionStatus, "retrieval_node_index_status"),
        default=ProjectionStatus.PENDING,
    )
    embedding_provider: Mapped[str | None] = mapped_column(String(100))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    embedding_config_version: Mapped[str | None] = mapped_column(String(100))
    embedding_vector: Mapped[list[float] | None] = mapped_column(JSON)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chunking_run: Mapped[ChunkingRun] = relationship(back_populates="retrieval_nodes")
    document_version: Mapped[DocumentVersion] = relationship(back_populates="retrieval_nodes")


class EmbeddingRun(Base):
    __tablename__ = "embedding_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_embedding_runs_status"
        ),
        Index(
            "ix_embedding_runs_idempotency",
            "chunking_run_id",
            "provider",
            "model",
            "dimension",
            "config_version",
        ),
        Index("ix_embedding_runs_version_status", "document_version_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    chunking_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunking_runs.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(255))
    dimension: Mapped[int] = mapped_column(Integer)
    config_version: Mapped[str] = mapped_column(String(100))
    config_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[ProjectionRunStatus] = mapped_column(
        enum_type(ProjectionRunStatus, "embedding_run_status"),
        default=ProjectionRunStatus.RUNNING,
    )
    input_count: Mapped[int] = mapped_column(Integer, default=0)
    batch_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    provider_tokens: Mapped[int | None] = mapped_column(BigInteger)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)

    document_version: Mapped[DocumentVersion] = relationship(back_populates="embedding_runs")
    chunking_run: Mapped[ChunkingRun] = relationship(back_populates="embedding_runs")
    batches: Mapped[list["EmbeddingBatch"]] = relationship(
        back_populates="embedding_run",
        cascade="all, delete-orphan",
        order_by="EmbeddingBatch.batch_index",
    )
    indexing_runs: Mapped[list["IndexingRun"]] = relationship(back_populates="embedding_run")


class EmbeddingBatch(Base):
    __tablename__ = "embedding_batches"
    __table_args__ = (
        UniqueConstraint("embedding_run_id", "batch_index", name="uq_embedding_batch_index"),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_embedding_batches_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    embedding_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("embedding_runs.id", ondelete="CASCADE"), index=True
    )
    batch_index: Mapped[int] = mapped_column(Integer)
    node_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_count: Mapped[int] = mapped_column(Integer)
    estimated_tokens: Mapped[int] = mapped_column(BigInteger)
    provider_tokens: Mapped[int | None] = mapped_column(BigInteger)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[EmbeddingBatchStatus] = mapped_column(
        enum_type(EmbeddingBatchStatus, "embedding_batch_status"),
        default=EmbeddingBatchStatus.PENDING,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)

    embedding_run: Mapped[EmbeddingRun] = relationship(back_populates="batches")


class IndexingRun(Base):
    __tablename__ = "indexing_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_indexing_runs_status"
        ),
        Index(
            "ix_indexing_runs_idempotency",
            "embedding_run_id",
            "documents_index",
            "chunks_index",
            "config_version",
        ),
        Index("ix_indexing_runs_version_status", "document_version_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    embedding_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("embedding_runs.id", ondelete="CASCADE"), index=True
    )
    documents_index: Mapped[str] = mapped_column(String(255))
    chunks_index: Mapped[str] = mapped_column(String(255))
    documents_read_alias: Mapped[str] = mapped_column(String(255))
    chunks_read_alias: Mapped[str] = mapped_column(String(255))
    chunks_write_alias: Mapped[str] = mapped_column(String(255))
    config_version: Mapped[str] = mapped_column(String(100))
    config_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    capability_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[ProjectionRunStatus] = mapped_column(
        enum_type(ProjectionRunStatus, "indexing_run_status"),
        default=ProjectionRunStatus.RUNNING,
    )
    expected_document_count: Mapped[int] = mapped_column(Integer, default=1)
    expected_node_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_document_count: Mapped[int | None] = mapped_column(Integer)
    indexed_node_count: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)

    document_version: Mapped[DocumentVersion] = relationship(back_populates="indexing_runs")
    embedding_run: Mapped[EmbeddingRun] = relationship(back_populates="indexing_runs")


class GraphExtractionRun(Base):
    __tablename__ = "graph_extraction_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_graph_runs_status"
        ),
        UniqueConstraint(
            "document_version_id",
            "schema_version",
            "extractor_version",
            "input_hash",
            name="uq_graph_run_idempotency",
        ),
        Index("ix_graph_runs_version_status", "document_version_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    schema_version: Mapped[str] = mapped_column(String(100))
    extractor_name: Mapped[str] = mapped_column(String(255))
    extractor_version: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(255))
    prompt_version: Mapped[str] = mapped_column(String(100))
    input_hash: Mapped[str] = mapped_column(String(64))
    config_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[GraphRunStatus] = mapped_column(
        enum_type(GraphRunStatus, "graph_run_status"), default=GraphRunStatus.RUNNING
    )
    parent_count: Mapped[int] = mapped_column(Integer, default=0)
    entity_count: Mapped[int] = mapped_column(Integer, default=0)
    relation_count: Mapped[int] = mapped_column(Integer, default=0)
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    usage_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document_version: Mapped[DocumentVersion] = relationship(back_populates="graph_extraction_runs")
    evidences: Mapped[list["GraphFactEvidence"]] = relationship(back_populates="extraction_run")


class GraphEntityRecord(Base):
    __tablename__ = "graph_entities"
    __table_args__ = (
        UniqueConstraint("schema_version", "canonical_key", name="uq_graph_entity_key"),
        CheckConstraint("origin IN ('extracted', 'manual')", name="ck_graph_entities_origin"),
        CheckConstraint(
            "review_status IN ('unreviewed', 'approved', 'rejected')",
            name="ck_graph_entities_review",
        ),
        Index("ix_graph_entities_name_type", "normalized_name", "entity_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    canonical_key: Mapped[str] = mapped_column(String(700))
    entity_type: Mapped[str] = mapped_column(String(100), index=True)
    primary_name: Mapped[str] = mapped_column(String(500))
    normalized_name: Mapped[str] = mapped_column(String(500), index=True)
    aliases_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    properties_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    origin: Mapped[GraphOrigin] = mapped_column(
        enum_type(GraphOrigin, "graph_entity_origin"), default=GraphOrigin.EXTRACTED
    )
    review_status: Mapped[GraphReviewStatus] = mapped_column(
        enum_type(GraphReviewStatus, "graph_entity_review_status"),
        default=GraphReviewStatus.UNREVIEWED,
    )
    schema_version: Mapped[str] = mapped_column(String(100))
    manual_lock: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GraphFactRecord(Base):
    __tablename__ = "graph_facts"
    __table_args__ = (
        UniqueConstraint("schema_version", "fact_key", name="uq_graph_fact_key"),
        CheckConstraint("origin IN ('extracted', 'manual')", name="ck_graph_facts_origin"),
        CheckConstraint(
            "review_status IN ('unreviewed', 'approved', 'rejected')",
            name="ck_graph_facts_review",
        ),
        Index("ix_graph_facts_subject_predicate", "subject_entity_id", "predicate"),
        Index("ix_graph_facts_object_predicate", "object_entity_id", "predicate"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    fact_key: Mapped[str] = mapped_column(String(800))
    subject_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_entities.id", ondelete="RESTRICT"), index=True
    )
    predicate: Mapped[str] = mapped_column(String(100), index=True)
    object_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_entities.id", ondelete="RESTRICT"), index=True
    )
    properties_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    origin: Mapped[GraphOrigin] = mapped_column(
        enum_type(GraphOrigin, "graph_fact_origin"), default=GraphOrigin.EXTRACTED
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    review_status: Mapped[GraphReviewStatus] = mapped_column(
        enum_type(GraphReviewStatus, "graph_fact_review_status"),
        default=GraphReviewStatus.UNREVIEWED,
    )
    schema_version: Mapped[str] = mapped_column(String(100))
    manual_lock: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    subject: Mapped[GraphEntityRecord] = relationship(foreign_keys=[subject_entity_id])
    object: Mapped[GraphEntityRecord] = relationship(foreign_keys=[object_entity_id])
    evidences: Mapped[list["GraphFactEvidence"]] = relationship(
        back_populates="fact", cascade="all, delete-orphan"
    )


class GraphFactEvidence(Base):
    __tablename__ = "graph_fact_evidences"
    __table_args__ = (
        UniqueConstraint(
            "fact_id", "document_version_id", "source_node_id", name="uq_graph_fact_evidence"
        ),
        Index("ix_graph_evidence_version_active", "document_version_id", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    fact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_facts.id", ondelete="CASCADE"), index=True
    )
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("graph_extraction_runs.id", ondelete="SET NULL"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("retrieval_nodes.id", ondelete="CASCADE"), index=True
    )
    source_locators_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    excerpt: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    fact: Mapped[GraphFactRecord] = relationship(back_populates="evidences")
    extraction_run: Mapped[GraphExtractionRun | None] = relationship(back_populates="evidences")


class GraphCorrectionAudit(Base):
    __tablename__ = "graph_correction_audits"
    __table_args__ = (
        CheckConstraint(
            "action IN ('create', 'update', 'merge', 'split', 'approve', 'reject')",
            name="ck_graph_corrections_action",
        ),
        Index("ix_graph_corrections_target", "target_type", "target_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[uuid.UUID] = mapped_column(index=True)
    action: Mapped[GraphCorrectionAction] = mapped_column(
        enum_type(GraphCorrectionAction, "graph_correction_action")
    )
    actor: Mapped[str] = mapped_column(String(255), default="local-admin")
    reason: Mapped[str] = mapped_column(Text)
    before_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GraphConflictRecord(Base):
    __tablename__ = "graph_conflicts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'resolved', 'dismissed')",
            name="ck_graph_conflicts_status",
        ),
        UniqueConstraint(
            "extraction_run_id", "target_type", "target_id", name="uq_graph_conflict_run_target"
        ),
        Index("ix_graph_conflicts_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("graph_extraction_runs.id", ondelete="CASCADE"), index=True
    )
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[uuid.UUID] = mapped_column(index=True)
    conflict_type: Mapped[str] = mapped_column(String(100))
    current_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    proposed_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[GraphConflictStatus] = mapped_column(
        enum_type(GraphConflictStatus, "graph_conflict_status"),
        default=GraphConflictStatus.PENDING,
    )
    resolution_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GraphQueryTrace(Base):
    __tablename__ = "graph_query_traces"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'fallback', 'rejected', 'failed')",
            name="ck_graph_query_traces_status",
        ),
        Index("ix_graph_query_traces_started_status", "started_at", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    question: Mapped[str] = mapped_column(Text)
    rewritten_question: Mapped[str] = mapped_column(Text)
    schema_version: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(255))
    generated_cypher: Mapped[str | None] = mapped_column(Text)
    validated_cypher: Mapped[str | None] = mapped_column(Text)
    validation_result_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    explain_summary_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    returned_row_count: Mapped[int] = mapped_column(Integer, default=0)
    source_node_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    path_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    fallback_reason: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    error_code: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[GraphQueryTraceStatus] = mapped_column(
        enum_type(GraphQueryTraceStatus, "graph_query_trace_status"),
        default=GraphQueryTraceStatus.RUNNING,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetrievalTrace(Base):
    __tablename__ = "retrieval_traces"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('bm25', 'dense', 'hybrid', 'hybrid_rerank')",
            name="ck_retrieval_traces_mode",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'degraded', 'failed')",
            name="ck_retrieval_traces_status",
        ),
        Index("ix_retrieval_traces_started_status", "started_at", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    query_original: Mapped[str] = mapped_column(Text)
    query_normalized: Mapped[str] = mapped_column(Text)
    query_rewritten: Mapped[str] = mapped_column(Text)
    mode: Mapped[RetrievalMode] = mapped_column(enum_type(RetrievalMode, "retrieval_mode"))
    status: Mapped[RetrievalTraceStatus] = mapped_column(
        enum_type(RetrievalTraceStatus, "retrieval_trace_status"),
        default=RetrievalTraceStatus.RUNNING,
    )
    config_version: Mapped[str] = mapped_column(String(100))
    config_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    rewrite_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    embedding_provider: Mapped[str | None] = mapped_column(String(100))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    rerank_provider: Mapped[str | None] = mapped_column(String(100))
    rerank_model: Mapped[str | None] = mapped_column(String(255))
    rerank_fallback_reason: Mapped[str | None] = mapped_column(Text)
    graph_query_trace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("graph_query_traces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    graph_fallback_reason: Mapped[str | None] = mapped_column(Text)
    bm25_candidates_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    dense_candidates_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    graph_candidates_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    rrf_candidates_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    diversified_candidates_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    reranked_candidates_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    selected_children_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    context_nodes_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    context_budget_tokens: Mapped[int] = mapped_column(Integer)
    context_used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usage_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    latency_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'deleted')", name="ck_conversations_status"),
        Index("ix_conversations_updated_status", "updated_at", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[ConversationStatus] = mapped_column(
        enum_type(ConversationStatus, "conversation_status"),
        default=ConversationStatus.ACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        CheckConstraint(
            "status IN ('completed', 'streaming', 'refused', 'failed')",
            name="ck_messages_status",
        ),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[MessageRole] = mapped_column(enum_type(MessageRole, "message_role"))
    status: Mapped[MessageStatus] = mapped_column(enum_type(MessageStatus, "message_status"))
    content: Mapped[str] = mapped_column(Text, default="")
    query_original: Mapped[str | None] = mapped_column(Text)
    query_rewritten: Mapped[str | None] = mapped_column(Text)
    retrieval_trace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("retrieval_traces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    model_invocation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_invocations.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    citations: Mapped[list["Citation"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="Citation.citation_index",
    )
    model_invocation: Mapped["ModelInvocation | None"] = relationship(
        foreign_keys=[model_invocation_id], post_update=True
    )


class Citation(Base):
    __tablename__ = "citations"
    __table_args__ = (
        UniqueConstraint("message_id", "citation_index", name="uq_citation_message_index"),
        Index("ix_citations_node_id", "node_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    citation_index: Mapped[int] = mapped_column(Integer)
    source_label: Mapped[str] = mapped_column(String(50))
    node_id: Mapped[uuid.UUID] = mapped_column()
    document_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    document_name: Mapped[str] = mapped_column(String(500))
    heading_path: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_locators_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    excerpt: Mapped[str] = mapped_column(Text)
    snapshot_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped[Message] = relationship(back_populates="citations")


class ModelInvocation(Base):
    __tablename__ = "model_invocations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_model_invocations_status",
        ),
        Index("ix_model_invocations_created_purpose", "created_at", "purpose"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    purpose: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(255))
    endpoint: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[ModelInvocationStatus] = mapped_column(
        enum_type(ModelInvocationStatus, "model_invocation_status"),
        default=ModelInvocationStatus.RUNNING,
    )
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    trace_id: Mapped[str | None] = mapped_column(String(255), index=True)
    request_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    response_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationRun(Base):
    """A reproducible evaluation of one immutable golden dataset snapshot."""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_evaluation_runs_status",
        ),
        CheckConstraint(
            "retrieval_mode IN ('bm25', 'dense', 'hybrid', 'hybrid_rerank')",
            name="ck_evaluation_runs_retrieval_mode",
        ),
        Index("ix_evaluation_runs_created_status", "created_at", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_version: Mapped[str] = mapped_column(String(100), index=True)
    dataset_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[EvaluationRunStatus] = mapped_column(
        enum_type(EvaluationRunStatus, "evaluation_run_status"),
        default=EvaluationRunStatus.PENDING,
    )
    retrieval_mode: Mapped[RetrievalMode] = mapped_column(
        enum_type(RetrievalMode, "evaluation_retrieval_mode")
    )
    config_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    model_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    metric_config_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    metrics_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    regression_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    report_uri: Mapped[str | None] = mapped_column(Text)
    failure_samples_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    baseline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)

    results: Mapped[list["EvaluationSampleResult"]] = relationship(
        back_populates="evaluation_run",
        cascade="all, delete-orphan",
        order_by="EvaluationSampleResult.sample_id",
    )


class EvaluationSampleResult(Base):
    """Per-question evidence retained so aggregate regressions remain auditable."""

    __tablename__ = "evaluation_sample_results"
    __table_args__ = (
        UniqueConstraint("evaluation_run_id", "sample_id", name="uq_evaluation_sample_run"),
        CheckConstraint(
            "status IN ('succeeded', 'failed')", name="ck_evaluation_sample_results_status"
        ),
        Index("ix_evaluation_sample_results_run_status", "evaluation_run_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"), index=True
    )
    sample_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[EvaluationSampleStatus] = mapped_column(
        enum_type(EvaluationSampleStatus, "evaluation_sample_status")
    )
    question: Mapped[str] = mapped_column(Text)
    expected_answer: Mapped[str | None] = mapped_column(Text)
    generated_answer: Mapped[str | None] = mapped_column(Text)
    retrieved_document_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    retrieved_node_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    citation_locators_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    retrieval_trace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("retrieval_traces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    graph_query_trace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("graph_query_traces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    metrics_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    ragas_metrics_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    usage_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)

    evaluation_run: Mapped[EvaluationRun] = relationship(back_populates="results")
