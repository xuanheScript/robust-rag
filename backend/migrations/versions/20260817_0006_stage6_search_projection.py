"""Add stage 6 embedding and OpenSearch projection audit data.

Revision ID: 20260817_0006
Revises: 20260817_0005
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0006"
down_revision: str | None = "20260817_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("retrieval_nodes", sa.Column("embedding_provider", sa.String(100)))
    op.add_column("retrieval_nodes", sa.Column("embedding_model", sa.String(255)))
    op.add_column("retrieval_nodes", sa.Column("embedding_dimension", sa.Integer()))
    op.add_column("retrieval_nodes", sa.Column("embedding_config_version", sa.String(100)))
    op.add_column("retrieval_nodes", sa.Column("embedding_vector", sa.JSON()))
    op.add_column("retrieval_nodes", sa.Column("embedded_at", sa.DateTime(timezone=True)))

    op.create_table(
        "embedding_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("chunking_run_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("config_version", sa.String(100), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running", "succeeded", "failed", name="embedding_run_status", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("input_count", sa.Integer(), nullable=False),
        sa.Column("batch_count", sa.Integer(), nullable=False),
        sa.Column("estimated_tokens", sa.BigInteger(), nullable=False),
        sa.Column("provider_tokens", sa.BigInteger()),
        sa.Column("estimated_cost_usd", sa.Float()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.JSON()),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_embedding_runs_status"
        ),
        sa.ForeignKeyConstraint(["chunking_run_id"], ["chunking_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_embedding_runs_document_version_id", "embedding_runs", ["document_version_id"]
    )
    op.create_index("ix_embedding_runs_chunking_run_id", "embedding_runs", ["chunking_run_id"])
    op.create_index(
        "ix_embedding_runs_idempotency",
        "embedding_runs",
        ["chunking_run_id", "provider", "model", "dimension", "config_version"],
    )
    op.create_index(
        "ix_embedding_runs_version_status",
        "embedding_runs",
        ["document_version_id", "status"],
    )

    op.create_table(
        "embedding_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("embedding_run_id", sa.Uuid(), nullable=False),
        sa.Column("batch_index", sa.Integer(), nullable=False),
        sa.Column("node_ids_json", sa.JSON(), nullable=False),
        sa.Column("input_count", sa.Integer(), nullable=False),
        sa.Column("estimated_tokens", sa.BigInteger(), nullable=False),
        sa.Column("provider_tokens", sa.BigInteger()),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "succeeded",
                "failed",
                name="embedding_batch_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.JSON()),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_embedding_batches_status",
        ),
        sa.ForeignKeyConstraint(["embedding_run_id"], ["embedding_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("embedding_run_id", "batch_index", name="uq_embedding_batch_index"),
    )
    op.create_index(
        "ix_embedding_batches_embedding_run_id", "embedding_batches", ["embedding_run_id"]
    )

    op.create_table(
        "indexing_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_run_id", sa.Uuid(), nullable=False),
        sa.Column("documents_index", sa.String(255), nullable=False),
        sa.Column("chunks_index", sa.String(255), nullable=False),
        sa.Column("documents_read_alias", sa.String(255), nullable=False),
        sa.Column("chunks_read_alias", sa.String(255), nullable=False),
        sa.Column("chunks_write_alias", sa.String(255), nullable=False),
        sa.Column("config_version", sa.String(100), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("capability_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running", "succeeded", "failed", name="indexing_run_status", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("expected_document_count", sa.Integer(), nullable=False),
        sa.Column("expected_node_count", sa.Integer(), nullable=False),
        sa.Column("indexed_document_count", sa.Integer()),
        sa.Column("indexed_node_count", sa.Integer()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.JSON()),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_indexing_runs_status"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["embedding_run_id"], ["embedding_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_indexing_runs_document_version_id", "indexing_runs", ["document_version_id"]
    )
    op.create_index("ix_indexing_runs_embedding_run_id", "indexing_runs", ["embedding_run_id"])
    op.create_index(
        "ix_indexing_runs_idempotency",
        "indexing_runs",
        ["embedding_run_id", "documents_index", "chunks_index", "config_version"],
    )
    op.create_index(
        "ix_indexing_runs_version_status",
        "indexing_runs",
        ["document_version_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_indexing_runs_version_status", table_name="indexing_runs")
    op.drop_index("ix_indexing_runs_idempotency", table_name="indexing_runs")
    op.drop_index("ix_indexing_runs_embedding_run_id", table_name="indexing_runs")
    op.drop_index("ix_indexing_runs_document_version_id", table_name="indexing_runs")
    op.drop_table("indexing_runs")
    op.drop_index("ix_embedding_batches_embedding_run_id", table_name="embedding_batches")
    op.drop_table("embedding_batches")
    op.drop_index("ix_embedding_runs_version_status", table_name="embedding_runs")
    op.drop_index("ix_embedding_runs_idempotency", table_name="embedding_runs")
    op.drop_index("ix_embedding_runs_chunking_run_id", table_name="embedding_runs")
    op.drop_index("ix_embedding_runs_document_version_id", table_name="embedding_runs")
    op.drop_table("embedding_runs")
    op.drop_column("retrieval_nodes", "embedded_at")
    op.drop_column("retrieval_nodes", "embedding_vector")
    op.drop_column("retrieval_nodes", "embedding_config_version")
    op.drop_column("retrieval_nodes", "embedding_dimension")
    op.drop_column("retrieval_nodes", "embedding_model")
    op.drop_column("retrieval_nodes", "embedding_provider")
