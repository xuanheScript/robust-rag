"""Add stage 4 quality assessments and manual review audit.

Revision ID: 20260817_0004
Revises: 20260817_0003
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0004"
down_revision: str | None = "20260817_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quality_assessments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("cleaning_run_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(length=50), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("evaluator", sa.String(length=255), nullable=False),
        sa.Column("evaluator_version", sa.String(length=100), nullable=False),
        sa.Column("engine_version", sa.String(length=100), nullable=False),
        sa.Column("rule_set_version", sa.String(length=100), nullable=False),
        sa.Column("policy_version", sa.String(length=100), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("prompt_version", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                name="quality_assessment_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.Enum(
                "passed",
                "warning",
                "quarantined",
                "rejected",
                name="quality_decision",
                native_enum=False,
            ),
            nullable=True,
        ),
        sa.Column("overall_score", sa.Float(), nullable=True),
        sa.Column("dimensions_json", sa.JSON(), nullable=False),
        sa.Column("issues_json", sa.JSON(), nullable=False),
        sa.Column("evaluator_executions_json", sa.JSON(), nullable=False),
        sa.Column("raw_result_uri", sa.Text(), nullable=True),
        sa.Column("input_content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "target_type IN ('document', 'retrieval_node')",
            name="ck_quality_assessments_target_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_quality_assessments_status",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('passed', 'warning', 'quarantined', 'rejected')",
            name="ck_quality_assessments_decision",
        ),
        sa.ForeignKeyConstraint(["cleaning_run_id"], ["cleaning_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_quality_assessments_document_version_id",
        "quality_assessments",
        ["document_version_id"],
    )
    op.create_index(
        "ix_quality_assessments_cleaning_run_id",
        "quality_assessments",
        ["cleaning_run_id"],
    )
    op.create_index("ix_quality_assessments_target_id", "quality_assessments", ["target_id"])
    op.create_index(
        "ix_quality_assessments_input_content_hash",
        "quality_assessments",
        ["input_content_hash"],
    )
    op.create_index(
        "ix_quality_assessments_idempotency",
        "quality_assessments",
        [
            "cleaning_run_id",
            "engine_version",
            "rule_set_version",
            "policy_version",
            "input_content_hash",
        ],
    )
    op.create_index(
        "ix_quality_assessments_version_status",
        "quality_assessments",
        ["document_version_id", "status"],
    )

    op.create_table(
        "quality_review_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "release",
                "reject",
                "reevaluate",
                name="quality_review_action",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_job_status", sa.String(length=50), nullable=False),
        sa.Column("previous_version_status", sa.String(length=50), nullable=False),
        sa.Column(
            "previous_decision",
            sa.Enum(
                "passed",
                "warning",
                "quarantined",
                "rejected",
                name="quality_review_previous_decision",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("quality_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "action IN ('release', 'reject', 'reevaluate')",
            name="ck_quality_review_actions_action",
        ),
        sa.ForeignKeyConstraint(["assessment_id"], ["quality_assessments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_quality_review_actions_document_version_id",
        "quality_review_actions",
        ["document_version_id"],
    )
    op.create_index(
        "ix_quality_review_actions_assessment_id",
        "quality_review_actions",
        ["assessment_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_quality_review_actions_assessment_id", table_name="quality_review_actions")
    op.drop_index(
        "ix_quality_review_actions_document_version_id",
        table_name="quality_review_actions",
    )
    op.drop_table("quality_review_actions")
    op.drop_index("ix_quality_assessments_version_status", table_name="quality_assessments")
    op.drop_index("ix_quality_assessments_idempotency", table_name="quality_assessments")
    op.drop_index("ix_quality_assessments_input_content_hash", table_name="quality_assessments")
    op.drop_index("ix_quality_assessments_target_id", table_name="quality_assessments")
    op.drop_index("ix_quality_assessments_cleaning_run_id", table_name="quality_assessments")
    op.drop_index("ix_quality_assessments_document_version_id", table_name="quality_assessments")
    op.drop_table("quality_assessments")
