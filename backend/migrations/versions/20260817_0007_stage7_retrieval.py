"""Add stage 7 durable retrieval traces.

Revision ID: 20260817_0007
Revises: 20260817_0006
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0007"
down_revision: str | None = "20260817_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retrieval_traces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("query_original", sa.Text(), nullable=False),
        sa.Column("query_normalized", sa.Text(), nullable=False),
        sa.Column("query_rewritten", sa.Text(), nullable=False),
        sa.Column(
            "mode",
            sa.Enum(
                "bm25",
                "dense",
                "hybrid",
                "hybrid_rerank",
                name="retrieval_mode",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "degraded",
                "failed",
                name="retrieval_trace_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("config_version", sa.String(100), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("rewrite_snapshot", sa.JSON(), nullable=False),
        sa.Column("embedding_provider", sa.String(100)),
        sa.Column("embedding_model", sa.String(255)),
        sa.Column("embedding_dimension", sa.Integer()),
        sa.Column("rerank_provider", sa.String(100)),
        sa.Column("rerank_model", sa.String(255)),
        sa.Column("rerank_fallback_reason", sa.Text()),
        sa.Column("bm25_candidates_json", sa.JSON(), nullable=False),
        sa.Column("dense_candidates_json", sa.JSON(), nullable=False),
        sa.Column("rrf_candidates_json", sa.JSON(), nullable=False),
        sa.Column("diversified_candidates_json", sa.JSON(), nullable=False),
        sa.Column("reranked_candidates_json", sa.JSON(), nullable=False),
        sa.Column("selected_children_json", sa.JSON(), nullable=False),
        sa.Column("context_nodes_json", sa.JSON(), nullable=False),
        sa.Column("context_budget_tokens", sa.Integer(), nullable=False),
        sa.Column("context_used_tokens", sa.Integer(), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("latency_json", sa.JSON(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.JSON()),
        sa.CheckConstraint(
            "mode IN ('bm25', 'dense', 'hybrid', 'hybrid_rerank')",
            name="ck_retrieval_traces_mode",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'degraded', 'failed')",
            name="ck_retrieval_traces_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_retrieval_traces_started_status", "retrieval_traces", ["started_at", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_retrieval_traces_started_status", table_name="retrieval_traces")
    op.drop_table("retrieval_traces")
