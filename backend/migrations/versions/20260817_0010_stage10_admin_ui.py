"""Add stage 10 graph conflict resolution audit fields.

Revision ID: 20260817_0010
Revises: 20260817_0009
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0010"
down_revision: str | None = "20260817_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "graph_conflicts",
        sa.Column("resolution_json", sa.JSON(), server_default="{}", nullable=False),
    )
    op.add_column("graph_conflicts", sa.Column("resolved_by", sa.String(255)))


def downgrade() -> None:
    op.drop_column("graph_conflicts", "resolved_by")
    op.drop_column("graph_conflicts", "resolution_json")
