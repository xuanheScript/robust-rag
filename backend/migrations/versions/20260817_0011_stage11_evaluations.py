"""Add stage 11 evaluation runs and per-sample evidence.

Revision ID: 20260817_0011
Revises: 20260817_0010
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0011"
down_revision: str | None = "20260817_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_version", sa.String(100), nullable=False),
        sa.Column("dataset_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("retrieval_mode", sa.String(30), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("model_snapshot", sa.JSON(), nullable=False),
        sa.Column("metric_config_json", sa.JSON(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("regression_json", sa.JSON(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float()),
        sa.Column("report_uri", sa.Text()),
        sa.Column("failure_samples_json", sa.JSON(), nullable=False),
        sa.Column("baseline_run_id", sa.Uuid()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("error", sa.JSON()),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_evaluation_runs_status",
        ),
        sa.CheckConstraint(
            "retrieval_mode IN ('bm25', 'dense', 'hybrid', 'hybrid_rerank')",
            name="ck_evaluation_runs_retrieval_mode",
        ),
        sa.ForeignKeyConstraint(["baseline_run_id"], ["evaluation_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evaluation_runs_dataset_version", "evaluation_runs", ["dataset_version"])
    op.create_index("ix_evaluation_runs_baseline_run_id", "evaluation_runs", ["baseline_run_id"])
    op.create_index(
        "ix_evaluation_runs_created_status", "evaluation_runs", ["created_at", "status"]
    )
    op.create_table(
        "evaluation_sample_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("sample_id", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expected_answer", sa.Text()),
        sa.Column("generated_answer", sa.Text()),
        sa.Column("retrieved_document_ids_json", sa.JSON(), nullable=False),
        sa.Column("retrieved_node_ids_json", sa.JSON(), nullable=False),
        sa.Column("citation_locators_json", sa.JSON(), nullable=False),
        sa.Column("retrieval_trace_id", sa.Uuid()),
        sa.Column("graph_query_trace_id", sa.Uuid()),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("ragas_metrics_json", sa.JSON(), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("latency_ms", sa.Float()),
        sa.Column("estimated_cost_usd", sa.Float()),
        sa.Column("error", sa.JSON()),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_evaluation_sample_results_status",
        ),
        sa.ForeignKeyConstraint(["evaluation_run_id"], ["evaluation_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["retrieval_trace_id"], ["retrieval_traces.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["graph_query_trace_id"], ["graph_query_traces.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evaluation_run_id", "sample_id", name="uq_evaluation_sample_run"),
    )
    op.create_index(
        "ix_evaluation_sample_results_evaluation_run_id",
        "evaluation_sample_results",
        ["evaluation_run_id"],
    )
    op.create_index(
        "ix_evaluation_sample_results_retrieval_trace_id",
        "evaluation_sample_results",
        ["retrieval_trace_id"],
    )
    op.create_index(
        "ix_evaluation_sample_results_graph_query_trace_id",
        "evaluation_sample_results",
        ["graph_query_trace_id"],
    )
    op.create_index(
        "ix_evaluation_sample_results_run_status",
        "evaluation_sample_results",
        ["evaluation_run_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("evaluation_sample_results")
    op.drop_table("evaluation_runs")
