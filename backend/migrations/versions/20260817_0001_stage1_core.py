"""Create stage 1 document and ingestion tables.

Revision ID: 20260817_0001
Revises:
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=500), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "deleted",
                name="document_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active', 'deleted')", name="ck_documents_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_status", "documents", ["status"])

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "uploaded",
                "parsing",
                "cleaning",
                "document_evaluating",
                "chunking",
                "chunk_evaluating",
                "embedding",
                "indexing",
                "ready",
                "failed",
                "quarantined",
                "superseded",
                name="version_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('uploaded', 'parsing', 'cleaning', 'document_evaluating', "
            "'chunking', 'chunk_evaluating', 'embedding', 'indexing', 'ready', "
            "'failed', 'quarantined', 'superseded')",
            name="ck_document_versions_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "sha256", name="uq_version_document_sha256"),
        sa.UniqueConstraint("document_id", "version_number", name="uq_version_document_number"),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_index("ix_document_versions_sha256", "document_versions", ["sha256"])
    op.create_index("ix_document_versions_status", "document_versions", ["status"])
    op.create_foreign_key(
        "fk_documents_current_version_id",
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "job_type",
            sa.Enum(
                "ingestion",
                "reprocess",
                name="job_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "quarantined",
                name="job_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "current_stage",
            sa.Enum(
                "upload",
                "parsing",
                "cleaning",
                "document_evaluating",
                "chunking",
                "chunk_evaluating",
                "embedding",
                "indexing",
                name="stage_name",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("progress_current", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("job_type IN ('ingestion', 'reprocess')", name="ck_jobs_type"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'quarantined')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint(
            "current_stage IN ('upload', 'parsing', 'cleaning', 'document_evaluating', "
            "'chunking', 'chunk_evaluating', 'embedding', 'indexing')",
            name="ck_jobs_current_stage",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ingestion_jobs_document_version_id", "ingestion_jobs", ["document_version_id"]
    )
    op.create_index("ix_ingestion_jobs_status", "ingestion_jobs", ["status"])
    op.create_index(
        "ix_ingestion_jobs_recovery",
        "ingestion_jobs",
        ["status", "current_stage", "updated_at"],
    )

    op.create_table(
        "stage_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage_name",
            sa.Enum(
                "upload",
                "parsing",
                "cleaning",
                "document_evaluating",
                "chunking",
                "chunk_evaluating",
                "embedding",
                "indexing",
                name="stage_run_name",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("implementation_name", sa.String(length=255), nullable=False),
        sa.Column("implementation_version", sa.String(length=100), nullable=False),
        sa.Column("config_version", sa.String(length=100), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "succeeded",
                "failed",
                "skipped",
                name="stage_run_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_artifact_uri", sa.Text(), nullable=True),
        sa.Column("output_artifact_uri", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["ingestion_jobs.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "stage_name IN ('upload', 'parsing', 'cleaning', 'document_evaluating', "
            "'chunking', 'chunk_evaluating', 'embedding', 'indexing')",
            name="ck_stage_runs_name",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
            name="ck_stage_runs_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "stage_name",
            "config_version",
            "attempt",
            name="uq_stage_run_idempotency",
        ),
    )
    op.create_index("ix_stage_runs_job_id", "stage_runs", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_stage_runs_job_id", table_name="stage_runs")
    op.drop_table("stage_runs")
    op.drop_index("ix_ingestion_jobs_recovery", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_status", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_document_version_id", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.drop_constraint("fk_documents_current_version_id", "documents", type_="foreignkey")
    op.drop_index("ix_document_versions_status", table_name="document_versions")
    op.drop_index("ix_document_versions_sha256", table_name="document_versions")
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_table("documents")
