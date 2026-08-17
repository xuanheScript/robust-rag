"""Add stage 8 conversations, citations, and model invocation traces.

Revision ID: 20260817_0008
Revises: 20260817_0007
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0008"
down_revision: str | None = "20260817_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(500)),
        sa.Column(
            "status",
            sa.Enum("active", "deleted", name="conversation_status", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('active', 'deleted')", name="ck_conversations_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_updated_status", "conversations", ["updated_at", "status"])

    op.create_table(
        "model_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(100)),
        sa.Column(
            "status",
            sa.Enum(
                "running", "succeeded", "failed", name="model_invocation_status", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("latency_ms", sa.Float()),
        sa.Column("estimated_cost_usd", sa.Float()),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(255)),
        sa.Column("request_snapshot", sa.JSON(), nullable=False),
        sa.Column("response_snapshot", sa.JSON(), nullable=False),
        sa.Column("error", sa.JSON()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_model_invocations_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_invocations_created_purpose",
        "model_invocations",
        ["created_at", "purpose"],
    )
    op.create_index("ix_model_invocations_trace_id", "model_invocations", ["trace_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum("user", "assistant", name="message_role", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "completed",
                "streaming",
                "refused",
                "failed",
                name="message_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("query_original", sa.Text()),
        sa.Column("query_rewritten", sa.Text()),
        sa.Column("retrieval_trace_id", sa.Uuid()),
        sa.Column("model_invocation_id", sa.Uuid()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.JSON()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        sa.CheckConstraint(
            "status IN ('completed', 'streaming', 'refused', 'failed')",
            name="ck_messages_status",
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["model_invocation_id"], ["model_invocations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["retrieval_trace_id"], ["retrieval_traces.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index(
        "ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]
    )
    op.create_index("ix_messages_retrieval_trace_id", "messages", ["retrieval_trace_id"])

    op.create_table(
        "citations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("citation_index", sa.Integer(), nullable=False),
        sa.Column("source_label", sa.String(50), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid()),
        sa.Column("document_version_id", sa.Uuid()),
        sa.Column("document_name", sa.String(500), nullable=False),
        sa.Column("heading_path", sa.JSON(), nullable=False),
        sa.Column("source_locators_json", sa.JSON(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "citation_index", name="uq_citation_message_index"),
    )
    op.create_index("ix_citations_message_id", "citations", ["message_id"])
    op.create_index("ix_citations_node_id", "citations", ["node_id"])


def downgrade() -> None:
    op.drop_index("ix_citations_node_id", table_name="citations")
    op.drop_index("ix_citations_message_id", table_name="citations")
    op.drop_table("citations")
    op.drop_index("ix_messages_retrieval_trace_id", table_name="messages")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_model_invocations_trace_id", table_name="model_invocations")
    op.drop_index("ix_model_invocations_created_purpose", table_name="model_invocations")
    op.drop_table("model_invocations")
    op.drop_index("ix_conversations_updated_status", table_name="conversations")
    op.drop_table("conversations")
