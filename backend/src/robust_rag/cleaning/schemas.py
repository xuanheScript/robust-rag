"""Versioned contracts for deterministic cleaning runs and audit reports."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CLEANING_REPORT_SCHEMA_VERSION = "cleaning-report/1.0"


class CleaningIssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"


class CleaningIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: CleaningIssueSeverity
    operator_name: str
    operator_version: str
    message: str
    block_ids: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class OperatorExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    config: dict[str, Any] = Field(default_factory=dict)
    changed_block_ids: list[str] = Field(default_factory=list)
    removed_block_ids: list[str] = Field(default_factory=list)
    issue_count: int = Field(ge=0)


class CleaningReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CLEANING_REPORT_SCHEMA_VERSION
    cleaning_run_id: str
    document_id: str
    document_version_id: str
    canonical_document_id: str
    pipeline_name: str
    pipeline_version: str
    config_version: str
    config_snapshot: dict[str, Any]
    input_content_hash: str
    output_content_hash: str
    input_block_count: int = Field(ge=0)
    output_block_count: int = Field(ge=0)
    changed_block_count: int = Field(ge=0)
    removed_block_count: int = Field(ge=0)
    operator_executions: list[OperatorExecution]
    issues: list[CleaningIssue]


class CleaningComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_run_id: str
    compared_run_id: str
    same_output: bool
    base_output_hash: str
    compared_output_hash: str
    added_block_ids: list[str]
    removed_block_ids: list[str]
    normalized_text_changed_block_ids: list[str]
    base_issue_count: int = Field(ge=0)
    compared_issue_count: int = Field(ge=0)
