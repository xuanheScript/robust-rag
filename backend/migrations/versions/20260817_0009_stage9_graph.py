"""Add stage 9 versioned knowledge graph and query traces.

Revision ID: 20260817_0009
Revises: 20260817_0008
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0009"
down_revision: str | None = "20260817_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column(
            "graph_status",
            sa.Enum(
                "pending",
                "running",
                "succeeded",
                "failed",
                "stale",
                "disabled",
                name="document_version_graph_status",
                native_enum=False,
            ),
            server_default="pending",
            nullable=False,
        ),
    )
    op.add_column("document_versions", sa.Column("graph_schema_version", sa.String(100)))
    op.add_column("document_versions", sa.Column("graph_projected_at", sa.DateTime(timezone=True)))
    op.create_index("ix_document_versions_graph_status", "document_versions", ["graph_status"])

    op.create_table(
        "graph_extraction_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.String(100), nullable=False),
        sa.Column("extractor_name", sa.String(255), nullable=False),
        sa.Column("extractor_version", sa.String(100), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("running", "succeeded", "failed", name="graph_run_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("parent_count", sa.Integer(), nullable=False),
        sa.Column("entity_count", sa.Integer(), nullable=False),
        sa.Column("relation_count", sa.Integer(), nullable=False),
        sa.Column("artifact_uri", sa.Text()),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.JSON()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_graph_runs_status"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id",
            "schema_version",
            "extractor_version",
            "input_hash",
            name="uq_graph_run_idempotency",
        ),
    )
    op.create_index(
        "ix_graph_runs_version_status", "graph_extraction_runs", ["document_version_id", "status"]
    )

    op.create_table(
        "graph_entities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_key", sa.String(700), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("primary_name", sa.String(500), nullable=False),
        sa.Column("normalized_name", sa.String(500), nullable=False),
        sa.Column("aliases_json", sa.JSON(), nullable=False),
        sa.Column("properties_json", sa.JSON(), nullable=False),
        sa.Column(
            "origin",
            sa.Enum("extracted", "manual", name="graph_entity_origin", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "review_status",
            sa.Enum(
                "unreviewed",
                "approved",
                "rejected",
                name="graph_entity_review_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("schema_version", sa.String(100), nullable=False),
        sa.Column("manual_lock", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("origin IN ('extracted', 'manual')", name="ck_graph_entities_origin"),
        sa.CheckConstraint(
            "review_status IN ('unreviewed', 'approved', 'rejected')",
            name="ck_graph_entities_review",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("schema_version", "canonical_key", name="uq_graph_entity_key"),
    )
    op.create_index("ix_graph_entities_entity_type", "graph_entities", ["entity_type"])
    op.create_index("ix_graph_entities_normalized_name", "graph_entities", ["normalized_name"])
    op.create_index(
        "ix_graph_entities_name_type", "graph_entities", ["normalized_name", "entity_type"]
    )

    op.create_table(
        "graph_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fact_key", sa.String(800), nullable=False),
        sa.Column("subject_entity_id", sa.Uuid(), nullable=False),
        sa.Column("predicate", sa.String(100), nullable=False),
        sa.Column("object_entity_id", sa.Uuid(), nullable=False),
        sa.Column("properties_json", sa.JSON(), nullable=False),
        sa.Column(
            "origin",
            sa.Enum("extracted", "manual", name="graph_fact_origin", native_enum=False),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float()),
        sa.Column(
            "review_status",
            sa.Enum(
                "unreviewed",
                "approved",
                "rejected",
                name="graph_fact_review_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("schema_version", sa.String(100), nullable=False),
        sa.Column("manual_lock", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("origin IN ('extracted', 'manual')", name="ck_graph_facts_origin"),
        sa.CheckConstraint(
            "review_status IN ('unreviewed', 'approved', 'rejected')", name="ck_graph_facts_review"
        ),
        sa.ForeignKeyConstraint(["object_entity_id"], ["graph_entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["subject_entity_id"], ["graph_entities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("schema_version", "fact_key", name="uq_graph_fact_key"),
    )
    op.create_index("ix_graph_facts_active", "graph_facts", ["active"])
    op.create_index("ix_graph_facts_predicate", "graph_facts", ["predicate"])
    op.create_index("ix_graph_facts_subject_entity_id", "graph_facts", ["subject_entity_id"])
    op.create_index("ix_graph_facts_object_entity_id", "graph_facts", ["object_entity_id"])
    op.create_index(
        "ix_graph_facts_subject_predicate", "graph_facts", ["subject_entity_id", "predicate"]
    )
    op.create_index(
        "ix_graph_facts_object_predicate", "graph_facts", ["object_entity_id", "predicate"]
    )

    op.create_table(
        "graph_fact_evidences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid()),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("source_node_id", sa.Uuid(), nullable=False),
        sa.Column("source_locators_json", sa.JSON(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["extraction_run_id"], ["graph_extraction_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["fact_id"], ["graph_facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_node_id"], ["retrieval_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fact_id", "document_version_id", "source_node_id", name="uq_graph_fact_evidence"
        ),
    )
    for column in (
        "fact_id",
        "extraction_run_id",
        "document_id",
        "document_version_id",
        "source_node_id",
        "active",
    ):
        op.create_index(f"ix_graph_fact_evidences_{column}", "graph_fact_evidences", [column])
    op.create_index(
        "ix_graph_evidence_version_active",
        "graph_fact_evidences",
        ["document_version_id", "active"],
    )

    op.create_table(
        "graph_correction_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(50), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "create",
                "update",
                "merge",
                "split",
                "approve",
                "reject",
                name="graph_correction_action",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "action IN ('create', 'update', 'merge', 'split', 'approve', 'reject')",
            name="ck_graph_corrections_action",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_graph_corrections_target", "graph_correction_audits", ["target_type", "target_id"]
    )
    op.create_index(
        "ix_graph_correction_audits_target_id", "graph_correction_audits", ["target_id"]
    )

    op.create_table(
        "graph_conflicts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(50), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("conflict_type", sa.String(100), nullable=False),
        sa.Column("current_json", sa.JSON(), nullable=False),
        sa.Column("proposed_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "resolved",
                "dismissed",
                name="graph_conflict_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved', 'dismissed')",
            name="ck_graph_conflicts_status",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_run_id"], ["graph_extraction_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extraction_run_id",
            "target_type",
            "target_id",
            name="uq_graph_conflict_run_target",
        ),
    )
    op.create_index(
        "ix_graph_conflicts_extraction_run_id", "graph_conflicts", ["extraction_run_id"]
    )
    op.create_index("ix_graph_conflicts_target_id", "graph_conflicts", ["target_id"])
    op.create_index(
        "ix_graph_conflicts_status_created", "graph_conflicts", ["status", "created_at"]
    )

    op.create_table(
        "graph_query_traces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("rewritten_question", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("generated_cypher", sa.Text()),
        sa.Column("validated_cypher", sa.Text()),
        sa.Column("validation_result_json", sa.JSON(), nullable=False),
        sa.Column("explain_summary_json", sa.JSON(), nullable=False),
        sa.Column("returned_row_count", sa.Integer(), nullable=False),
        sa.Column("source_node_ids_json", sa.JSON(), nullable=False),
        sa.Column("path_json", sa.JSON(), nullable=False),
        sa.Column("fallback_reason", sa.Text()),
        sa.Column("latency_ms", sa.Float()),
        sa.Column("error_code", sa.String(100)),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "fallback",
                "rejected",
                "failed",
                name="graph_query_trace_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'fallback', 'rejected', 'failed')",
            name="ck_graph_query_traces_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_graph_query_traces_started_status", "graph_query_traces", ["started_at", "status"]
    )

    op.add_column("retrieval_traces", sa.Column("graph_query_trace_id", sa.Uuid()))
    op.add_column("retrieval_traces", sa.Column("graph_fallback_reason", sa.Text()))
    op.add_column(
        "retrieval_traces",
        sa.Column("graph_candidates_json", sa.JSON(), server_default="[]", nullable=False),
    )
    op.create_foreign_key(
        "fk_retrieval_traces_graph_query_trace_id",
        "retrieval_traces",
        "graph_query_traces",
        ["graph_query_trace_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_retrieval_traces_graph_query_trace_id", "retrieval_traces", ["graph_query_trace_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_retrieval_traces_graph_query_trace_id", table_name="retrieval_traces")
    op.drop_constraint(
        "fk_retrieval_traces_graph_query_trace_id", "retrieval_traces", type_="foreignkey"
    )
    op.drop_column("retrieval_traces", "graph_candidates_json")
    op.drop_column("retrieval_traces", "graph_fallback_reason")
    op.drop_column("retrieval_traces", "graph_query_trace_id")
    op.drop_index("ix_graph_query_traces_started_status", table_name="graph_query_traces")
    op.drop_table("graph_query_traces")
    op.drop_index("ix_graph_conflicts_status_created", table_name="graph_conflicts")
    op.drop_index("ix_graph_conflicts_target_id", table_name="graph_conflicts")
    op.drop_index("ix_graph_conflicts_extraction_run_id", table_name="graph_conflicts")
    op.drop_table("graph_conflicts")
    op.drop_index("ix_graph_correction_audits_target_id", table_name="graph_correction_audits")
    op.drop_index("ix_graph_corrections_target", table_name="graph_correction_audits")
    op.drop_table("graph_correction_audits")
    op.drop_index("ix_graph_evidence_version_active", table_name="graph_fact_evidences")
    for column in (
        "active",
        "source_node_id",
        "document_version_id",
        "document_id",
        "extraction_run_id",
        "fact_id",
    ):
        op.drop_index(f"ix_graph_fact_evidences_{column}", table_name="graph_fact_evidences")
    op.drop_table("graph_fact_evidences")
    op.drop_index("ix_graph_facts_object_predicate", table_name="graph_facts")
    op.drop_index("ix_graph_facts_subject_predicate", table_name="graph_facts")
    op.drop_index("ix_graph_facts_object_entity_id", table_name="graph_facts")
    op.drop_index("ix_graph_facts_subject_entity_id", table_name="graph_facts")
    op.drop_index("ix_graph_facts_predicate", table_name="graph_facts")
    op.drop_index("ix_graph_facts_active", table_name="graph_facts")
    op.drop_table("graph_facts")
    op.drop_index("ix_graph_entities_name_type", table_name="graph_entities")
    op.drop_index("ix_graph_entities_normalized_name", table_name="graph_entities")
    op.drop_index("ix_graph_entities_entity_type", table_name="graph_entities")
    op.drop_table("graph_entities")
    op.drop_index("ix_graph_runs_version_status", table_name="graph_extraction_runs")
    op.drop_table("graph_extraction_runs")
    op.drop_index("ix_document_versions_graph_status", table_name="document_versions")
    op.drop_column("document_versions", "graph_projected_at")
    op.drop_column("document_versions", "graph_schema_version")
    op.drop_column("document_versions", "graph_status")
