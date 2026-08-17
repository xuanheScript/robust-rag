"""Add stage 3 cleaning run audit records.

Revision ID: 20260817_0003
Revises: 20260817_0002
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0003"
down_revision: str | None = "20260817_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cleaning_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_document_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_name", sa.String(length=255), nullable=False),
        sa.Column("pipeline_version", sa.String(length=100), nullable=False),
        sa.Column("config_version", sa.String(length=100), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                name="cleaning_run_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("input_artifact_uri", sa.Text(), nullable=False),
        sa.Column("output_artifact_uri", sa.Text(), nullable=True),
        sa.Column("report_artifact_uri", sa.Text(), nullable=True),
        sa.Column("input_content_hash", sa.String(length=64), nullable=False),
        sa.Column("output_content_hash", sa.String(length=64), nullable=True),
        sa.Column("input_block_count", sa.Integer(), nullable=False),
        sa.Column("output_block_count", sa.Integer(), nullable=True),
        sa.Column("changed_block_count", sa.Integer(), nullable=True),
        sa.Column("removed_block_count", sa.Integer(), nullable=True),
        sa.Column("issue_count", sa.Integer(), nullable=True),
        sa.Column("operator_executions", sa.JSON(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_cleaning_runs_status"
        ),
        sa.ForeignKeyConstraint(
            ["canonical_document_id"], ["canonical_documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cleaning_runs_document_version_id",
        "cleaning_runs",
        ["document_version_id"],
    )
    op.create_index(
        "ix_cleaning_runs_canonical_document_id",
        "cleaning_runs",
        ["canonical_document_id"],
    )
    op.create_index(
        "ix_cleaning_runs_output_content_hash", "cleaning_runs", ["output_content_hash"]
    )
    op.create_index(
        "ix_cleaning_runs_idempotency",
        "cleaning_runs",
        ["canonical_document_id", "pipeline_version", "config_version", "input_content_hash"],
    )
    op.create_index(
        "ix_cleaning_runs_version_status",
        "cleaning_runs",
        ["document_version_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_cleaning_runs_version_status", table_name="cleaning_runs")
    op.drop_index("ix_cleaning_runs_idempotency", table_name="cleaning_runs")
    op.drop_index("ix_cleaning_runs_output_content_hash", table_name="cleaning_runs")
    op.drop_index("ix_cleaning_runs_canonical_document_id", table_name="cleaning_runs")
    op.drop_index("ix_cleaning_runs_document_version_id", table_name="cleaning_runs")
    op.drop_table("cleaning_runs")
