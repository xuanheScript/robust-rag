"""Add stage 5 chunking runs and retrieval nodes.

Revision ID: 20260817_0005
Revises: 20260817_0004
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0005"
down_revision: str | None = "20260817_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chunking_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_document_id", sa.Uuid(), nullable=False),
        sa.Column("cleaning_run_id", sa.Uuid(), nullable=False),
        sa.Column("quality_assessment_id", sa.Uuid(), nullable=False),
        sa.Column("chunker_name", sa.String(length=255), nullable=False),
        sa.Column("chunker_version", sa.String(length=100), nullable=False),
        sa.Column("config_version", sa.String(length=100), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                name="chunking_run_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("input_artifact_uri", sa.Text(), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=True),
        sa.Column("report_artifact_uri", sa.Text(), nullable=True),
        sa.Column("input_content_hash", sa.String(length=64), nullable=False),
        sa.Column("parent_count", sa.Integer(), nullable=True),
        sa.Column("child_count", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_chunking_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_document_id"], ["canonical_documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["cleaning_run_id"], ["cleaning_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["quality_assessment_id"], ["quality_assessments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chunking_runs_document_version_id", "chunking_runs", ["document_version_id"]
    )
    op.create_index(
        "ix_chunking_runs_canonical_document_id", "chunking_runs", ["canonical_document_id"]
    )
    op.create_index("ix_chunking_runs_cleaning_run_id", "chunking_runs", ["cleaning_run_id"])
    op.create_index(
        "ix_chunking_runs_quality_assessment_id", "chunking_runs", ["quality_assessment_id"]
    )
    op.create_index("ix_chunking_runs_input_content_hash", "chunking_runs", ["input_content_hash"])
    op.create_index(
        "ix_chunking_runs_idempotency",
        "chunking_runs",
        [
            "cleaning_run_id",
            "chunker_name",
            "chunker_version",
            "config_version",
            "input_content_hash",
        ],
    )
    op.create_index(
        "ix_chunking_runs_version_status",
        "chunking_runs",
        ["document_version_id", "status"],
    )

    op.create_table(
        "retrieval_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chunking_run_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_document_id", sa.Uuid(), nullable=False),
        sa.Column(
            "node_level",
            sa.Enum("parent", "child", name="retrieval_node_level", native_enum=False),
            nullable=False,
        ),
        sa.Column("parent_node_id", sa.Uuid(), nullable=True),
        sa.Column("previous_node_id", sa.Uuid(), nullable=True),
        sa.Column("next_node_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=1000), nullable=True),
        sa.Column("heading_path", sa.JSON(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("retrieval_text", sa.Text(), nullable=False),
        sa.Column("source_locators_json", sa.JSON(), nullable=False),
        sa.Column("source_block_ids", sa.JSON(), nullable=False),
        sa.Column("content_types", sa.JSON(), nullable=False),
        sa.Column("language", sa.String(length=50), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column(
            "quality_status",
            sa.Enum(
                "passed",
                "warning",
                "quarantined",
                "rejected",
                name="retrieval_node_quality_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("quality_summary_json", sa.JSON(), nullable=False),
        sa.Column("chunker_name", sa.String(length=255), nullable=False),
        sa.Column("chunker_version", sa.String(length=100), nullable=False),
        sa.Column("chunking_config_version", sa.String(length=100), nullable=False),
        sa.Column("retrieval_text_hash", sa.String(length=64), nullable=False),
        sa.Column("attributes_json", sa.JSON(), nullable=False),
        sa.Column(
            "embedding_status",
            sa.Enum(
                "pending",
                "succeeded",
                "failed",
                "stale",
                name="retrieval_node_embedding_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "index_status",
            sa.Enum(
                "pending",
                "succeeded",
                "failed",
                "stale",
                name="retrieval_node_index_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("node_level IN ('parent', 'child')", name="ck_retrieval_nodes_level"),
        sa.CheckConstraint(
            "quality_status IN ('passed', 'warning', 'quarantined', 'rejected')",
            name="ck_retrieval_nodes_quality_status",
        ),
        sa.CheckConstraint(
            "embedding_status IN ('pending', 'succeeded', 'failed', 'stale')",
            name="ck_retrieval_nodes_embedding_status",
        ),
        sa.CheckConstraint(
            "index_status IN ('pending', 'succeeded', 'failed', 'stale')",
            name="ck_retrieval_nodes_index_status",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_document_id"], ["canonical_documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["chunking_run_id"], ["chunking_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_retrieval_nodes_chunking_run_id", "retrieval_nodes", ["chunking_run_id"])
    op.create_index("ix_retrieval_nodes_document_id", "retrieval_nodes", ["document_id"])
    op.create_index(
        "ix_retrieval_nodes_document_version_id", "retrieval_nodes", ["document_version_id"]
    )
    op.create_index(
        "ix_retrieval_nodes_canonical_document_id",
        "retrieval_nodes",
        ["canonical_document_id"],
    )
    op.create_index(
        "ix_retrieval_nodes_retrieval_text_hash",
        "retrieval_nodes",
        ["retrieval_text_hash"],
    )
    op.create_index(
        "ix_retrieval_nodes_version_level",
        "retrieval_nodes",
        ["document_version_id", "node_level"],
    )
    op.create_index("ix_retrieval_nodes_parent", "retrieval_nodes", ["parent_node_id"])


def downgrade() -> None:
    op.drop_index("ix_retrieval_nodes_parent", table_name="retrieval_nodes")
    op.drop_index("ix_retrieval_nodes_version_level", table_name="retrieval_nodes")
    op.drop_index("ix_retrieval_nodes_retrieval_text_hash", table_name="retrieval_nodes")
    op.drop_index("ix_retrieval_nodes_canonical_document_id", table_name="retrieval_nodes")
    op.drop_index("ix_retrieval_nodes_document_version_id", table_name="retrieval_nodes")
    op.drop_index("ix_retrieval_nodes_document_id", table_name="retrieval_nodes")
    op.drop_index("ix_retrieval_nodes_chunking_run_id", table_name="retrieval_nodes")
    op.drop_table("retrieval_nodes")
    op.drop_index("ix_chunking_runs_version_status", table_name="chunking_runs")
    op.drop_index("ix_chunking_runs_idempotency", table_name="chunking_runs")
    op.drop_index("ix_chunking_runs_input_content_hash", table_name="chunking_runs")
    op.drop_index("ix_chunking_runs_quality_assessment_id", table_name="chunking_runs")
    op.drop_index("ix_chunking_runs_cleaning_run_id", table_name="chunking_runs")
    op.drop_index("ix_chunking_runs_canonical_document_id", table_name="chunking_runs")
    op.drop_index("ix_chunking_runs_document_version_id", table_name="chunking_runs")
    op.drop_table("chunking_runs")
