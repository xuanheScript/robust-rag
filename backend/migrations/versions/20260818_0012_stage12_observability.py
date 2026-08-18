"""Align evaluation storage types for stage 12 migration checks.

Revision ID: 20260818_0012
Revises: 20260817_0011
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0012"
down_revision: str | None = "20260817_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


evaluation_run_status = sa.Enum(
    "pending",
    "running",
    "succeeded",
    "failed",
    name="evaluation_run_status",
    native_enum=False,
)
evaluation_retrieval_mode = sa.Enum(
    "bm25",
    "dense",
    "hybrid",
    "hybrid_rerank",
    name="evaluation_retrieval_mode",
    native_enum=False,
)
evaluation_sample_status = sa.Enum(
    "succeeded",
    "failed",
    name="evaluation_sample_status",
    native_enum=False,
)


def upgrade() -> None:
    op.alter_column(
        "evaluation_runs",
        "status",
        existing_type=sa.String(20),
        type_=evaluation_run_status,
        existing_nullable=False,
    )
    op.alter_column(
        "evaluation_runs",
        "retrieval_mode",
        existing_type=sa.String(30),
        type_=evaluation_retrieval_mode,
        existing_nullable=False,
    )
    op.alter_column(
        "evaluation_runs",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )
    op.alter_column(
        "evaluation_sample_results",
        "status",
        existing_type=sa.String(20),
        type_=evaluation_sample_status,
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "evaluation_sample_results",
        "status",
        existing_type=evaluation_sample_status,
        type_=sa.String(20),
        existing_nullable=False,
    )
    op.alter_column(
        "evaluation_runs",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=True,
    )
    op.alter_column(
        "evaluation_runs",
        "retrieval_mode",
        existing_type=evaluation_retrieval_mode,
        type_=sa.String(30),
        existing_nullable=False,
    )
    op.alter_column(
        "evaluation_runs",
        "status",
        existing_type=evaluation_run_status,
        type_=sa.String(20),
        existing_nullable=False,
    )
