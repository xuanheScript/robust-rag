"""Preserve every graph extraction attempt for diagnosis and recovery.

Revision ID: 20260819_0013
Revises: 20260818_0012
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0013"
down_revision: str | None = "20260818_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "graph_extraction_runs",
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
    )
    op.drop_constraint("uq_graph_run_idempotency", "graph_extraction_runs", type_="unique")
    op.create_unique_constraint(
        "uq_graph_run_idempotency",
        "graph_extraction_runs",
        [
            "document_version_id",
            "schema_version",
            "extractor_version",
            "input_hash",
            "attempt",
        ],
    )


def downgrade() -> None:
    op.drop_constraint("uq_graph_run_idempotency", "graph_extraction_runs", type_="unique")
    op.create_unique_constraint(
        "uq_graph_run_idempotency",
        "graph_extraction_runs",
        ["document_version_id", "schema_version", "extractor_version", "input_hash"],
    )
    op.drop_column("graph_extraction_runs", "attempt")
