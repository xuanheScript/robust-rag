"""Add stage 2 parsing and canonical document records.

Revision ID: 20260817_0002
Revises: 20260817_0001
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0002"
down_revision: str | None = "20260817_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "parse_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("parser_name", sa.String(length=255), nullable=False),
        sa.Column("parser_version", sa.String(length=100), nullable=False),
        sa.Column("parser_mode", sa.String(length=100), nullable=False),
        sa.Column("parser_config", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("running", "succeeded", "failed", name="parse_run_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("artifact_uri", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_parse_runs_status"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_parse_runs_document_version_id", "parse_runs", ["document_version_id"])
    op.create_index("ix_parse_runs_version_status", "parse_runs", ["document_version_id", "status"])

    op.create_table(
        "canonical_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.String(length=50), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=50), nullable=True),
        sa.Column("title", sa.String(length=1000), nullable=True),
        sa.Column("block_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["parse_run_id"], ["parse_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parse_run_id"),
        sa.UniqueConstraint(
            "document_version_id", "schema_version", name="uq_canonical_version_schema"
        ),
    )
    op.create_index(
        "ix_canonical_documents_document_version_id",
        "canonical_documents",
        ["document_version_id"],
    )
    op.create_index("ix_canonical_documents_content_hash", "canonical_documents", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_canonical_documents_content_hash", table_name="canonical_documents")
    op.drop_index("ix_canonical_documents_document_version_id", table_name="canonical_documents")
    op.drop_table("canonical_documents")
    op.drop_index("ix_parse_runs_version_status", table_name="parse_runs")
    op.drop_index("ix_parse_runs_document_version_id", table_name="parse_runs")
    op.drop_table("parse_runs")
