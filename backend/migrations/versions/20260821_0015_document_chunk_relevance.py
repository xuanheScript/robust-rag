"""Persist the separated document-level retrieval signal.

Revision ID: 20260821_0015
Revises: 20260819_0014
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260821_0015"
down_revision: str | None = "20260819_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "retrieval_traces",
        sa.Column(
            "document_candidates_json",
            sa.JSON(),
            server_default="[]",
            nullable=False,
        ),
    )
    op.alter_column(
        "retrieval_traces",
        "document_candidates_json",
        server_default=None,
        existing_type=sa.JSON(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.drop_column("retrieval_traces", "document_candidates_json")
