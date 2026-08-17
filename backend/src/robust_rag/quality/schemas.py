"""Versioned domain contracts for document quality evaluation."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

QUALITY_REPORT_SCHEMA_VERSION = "quality-report/1.0"


class QualityDimension(StrEnum):
    PARSE_COMPLETENESS = "parse_completeness"
    TEXT_INTEGRITY = "text_integrity"
    STRUCTURE_INTEGRITY = "structure_integrity"
    DUPLICATION = "duplication"
    INFORMATION_DENSITY = "information_density"
    CONTEXT_COMPLETENESS = "context_completeness"
    SOURCE_TRACEABILITY = "source_traceability"
    RETRIEVAL_READINESS = "retrieval_readiness"


class QualityDecision(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


class QualityIssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class QualityIssueSource(StrEnum):
    SCHEMA = "schema"
    DETERMINISTIC = "deterministic"
    DINGO_RULE = "dingo_rule"
    DINGO_LLM = "dingo_llm"


class EvaluatorStatus(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


class QualityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    value: Any
    threshold: Any | None = None
    block_ids: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class QualityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    dimension: QualityDimension
    severity: QualityIssueSeverity
    source: QualityIssueSource
    evaluator: str
    evaluator_version: str
    message: str
    evidence: list[QualityEvidence] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class DimensionScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: QualityDimension
    score: float = Field(ge=0, le=1)
    evidence: list[QualityEvidence] = Field(default_factory=list)


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    calls: int = Field(default=1, ge=1)


class EvaluatorExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    evaluator_type: str
    status: EvaluatorStatus
    model: str | None = None
    prompt_version: str | None = None
    duration_ms: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    input_hash: str | None = None
    input_char_count: int | None = Field(default=None, ge=0)
    input_truncated: bool = False
    usage: TokenUsage | None = None
    raw_results: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class QualityEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: QualityDecision
    overall_score: float = Field(ge=0, le=1)
    dimensions: list[DimensionScore]
    issues: list[QualityIssue]
    evaluator_executions: list[EvaluatorExecution]


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = QUALITY_REPORT_SCHEMA_VERSION
    assessment_id: str
    document_id: str
    document_version_id: str
    cleaning_run_id: str
    target_type: str = "document"
    target_id: str
    engine_name: str
    engine_version: str
    rule_set_version: str
    policy_version: str
    config_snapshot: dict[str, Any]
    input_content_hash: str
    decision: QualityDecision
    overall_score: float = Field(ge=0, le=1)
    dimensions: list[DimensionScore]
    issues: list[QualityIssue]
    evaluator_executions: list[EvaluatorExecution]


class QualityReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)
    actor: str = Field(default="local-admin", min_length=1, max_length=255)
