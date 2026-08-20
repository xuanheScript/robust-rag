"""Make graph generation opt-in and persist every manual build request.

Revision ID: 20260819_0014
Revises: 20260819_0013
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0014"
down_revision: str | None = "20260819_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column("graph_active", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_index(
        "ix_document_versions_graph_active",
        "document_versions",
        ["graph_active"],
    )
    op.alter_column(
        "document_versions",
        "graph_status",
        type_=sa.String(length=20),
        existing_type=sa.String(length=9),
        existing_nullable=False,
        existing_server_default="pending",
    )
    op.execute(
        "UPDATE document_versions SET graph_active = true "
        "WHERE graph_projected_at IS NOT NULL "
        "AND graph_status IN ('pending', 'running', 'succeeded', 'failed')"
    )
    op.execute(
        "UPDATE document_versions SET graph_status = 'succeeded' "
        "WHERE graph_status = 'pending' "
        "AND graph_projected_at IS NOT NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM graph_extraction_runs r "
        "WHERE r.document_version_id = document_versions.id "
        "AND r.status = 'running'"
        ")"
    )
    op.execute(
        "UPDATE document_versions SET graph_status = 'not_requested' "
        "WHERE graph_status = 'disabled' OR ("
        "graph_status = 'pending' "
        "AND graph_projected_at IS NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM graph_extraction_runs r "
        "WHERE r.document_version_id = document_versions.id "
        "AND r.status = 'running'"
        "))"
    )
    op.alter_column(
        "document_versions",
        "graph_status",
        server_default="not_requested",
        existing_type=sa.String(length=20),
        existing_nullable=False,
    )

    op.create_table(
        "graph_build_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "request_type",
            sa.Enum(
                "generate",
                "rebuild",
                "retry",
                name="graph_build_request_type",
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
                name="graph_build_request_status",
                native_enum=False,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("requested_by", sa.String(255), server_default="local-admin", nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("force", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "projection_was_active", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("celery_task_id", sa.String(255)),
        sa.Column("parent_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "estimated_input_tokens", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("estimated_input_cost_usd", sa.Float()),
        sa.Column("actual_input_tokens", sa.Integer()),
        sa.Column("actual_output_tokens", sa.Integer()),
        sa.Column("actual_total_tokens", sa.Integer()),
        sa.Column("actual_cost_usd", sa.Float()),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="2", nullable=False),
        sa.Column("previous_graph_status", sa.String(50), nullable=False),
        sa.Column("error", sa.JSON()),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "request_type IN ('generate', 'rebuild', 'retry')",
            name="ck_graph_build_requests_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_graph_build_requests_status",
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_graph_build_requests_idempotency_key"
        ),
    )
    op.create_index(
        "ix_graph_build_requests_batch",
        "graph_build_requests",
        ["batch_id", "created_at"],
    )
    op.create_index(
        "ix_graph_build_requests_document_id",
        "graph_build_requests",
        ["document_id"],
    )
    op.create_index(
        "ix_graph_build_requests_document_version_id",
        "graph_build_requests",
        ["document_version_id"],
    )
    op.create_index(
        "ix_graph_build_requests_status", "graph_build_requests", ["status"]
    )
    op.create_index(
        "ix_graph_build_requests_version_status",
        "graph_build_requests",
        ["document_version_id", "status"],
    )
    op.add_column("graph_extraction_runs", sa.Column("build_request_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_graph_extraction_runs_build_request_id",
        "graph_extraction_runs",
        "graph_build_requests",
        ["build_request_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_graph_extraction_runs_build_request_id",
        "graph_extraction_runs",
        ["build_request_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_graph_extraction_runs_build_request_id", table_name="graph_extraction_runs"
    )
    op.drop_constraint(
        "fk_graph_extraction_runs_build_request_id",
        "graph_extraction_runs",
        type_="foreignkey",
    )
    op.drop_column("graph_extraction_runs", "build_request_id")
    op.drop_index("ix_graph_build_requests_version_status", table_name="graph_build_requests")
    op.drop_index("ix_graph_build_requests_status", table_name="graph_build_requests")
    op.drop_index(
        "ix_graph_build_requests_document_version_id", table_name="graph_build_requests"
    )
    op.drop_index("ix_graph_build_requests_document_id", table_name="graph_build_requests")
    op.drop_index("ix_graph_build_requests_batch", table_name="graph_build_requests")
    op.drop_table("graph_build_requests")
    op.execute(
        "UPDATE document_versions SET graph_status = 'pending' "
        "WHERE graph_status IN ('not_requested', 'hidden')"
    )
    op.alter_column(
        "document_versions",
        "graph_status",
        server_default="pending",
        existing_type=sa.String(length=20),
        existing_nullable=False,
    )
    op.alter_column(
        "document_versions",
        "graph_status",
        type_=sa.String(length=9),
        existing_type=sa.String(length=20),
        existing_nullable=False,
        existing_server_default="pending",
    )
    op.drop_index("ix_document_versions_graph_active", table_name="document_versions")
    op.drop_column("document_versions", "graph_active")
