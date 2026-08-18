"""QualityEngine orchestration and multi-dimensional admission policy."""

from collections import defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from robust_rag.parsing.schemas import CanonicalDocument
from robust_rag.quality.deterministic import DeterministicRuleEvaluator, SchemaValidator
from robust_rag.quality.dingo import DingoAdapter, skipped_dingo_execution
from robust_rag.quality.schemas import (
    DimensionScore,
    EvaluatorExecution,
    QualityDecision,
    QualityDimension,
    QualityEvaluationResult,
    QualityEvidence,
    QualityIssue,
    QualityIssueSeverity,
)


class QualityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_set_version: str = "stage4-quality-rules-v1"
    policy_version: str = "stage4-quality-policy-v1"
    corruption_warning_ratio: float = Field(default=0.001, ge=0, le=1)
    corruption_quarantine_ratio: float = Field(default=0.05, ge=0, le=1)
    corruption_reject_ratio: float = Field(default=0.30, ge=0, le=1)
    duplicate_quarantine_ratio: float = Field(default=0.50, ge=0, le=1)
    missing_locator_quarantine_ratio: float = Field(default=0.30, ge=0, le=1)
    empty_page_quarantine_ratio: float = Field(default=0.50, ge=0, le=1)
    parser_confidence_warning: float = Field(default=0.50, ge=0, le=1)
    low_confidence_quarantine_ratio: float = Field(default=0.50, ge=0, le=1)
    information_density_warning: float = Field(default=0.40, ge=0, le=1)
    information_density_quarantine: float = Field(default=0.20, ge=0, le=1)
    sparse_extraction_min_bytes: int = Field(default=1_048_576, ge=1)
    sparse_extraction_min_chars_per_mb: int = Field(default=100, ge=1)
    reject_parse_threshold: float = Field(default=0.05, ge=0, le=1)
    reject_text_threshold: float = Field(default=0.20, ge=0, le=1)
    quarantine_dimension_threshold: float = Field(default=0.50, ge=0, le=1)
    warning_dimension_threshold: float = Field(default=0.80, ge=0, le=1)
    dingo_rule_enabled: bool = False
    dingo_llm_enabled: bool = False


class QualityPolicyEngine:
    name = "multi-dimensional-quality-policy"
    version = "1.0.0"

    def __init__(self, config: QualityConfig) -> None:
        self.config = config

    def decide(
        self, dimensions: list[DimensionScore], issues: list[QualityIssue]
    ) -> QualityDecision:
        by_dimension = {score.dimension: score.score for score in dimensions}
        if any(issue.severity is QualityIssueSeverity.CRITICAL for issue in issues):
            return QualityDecision.REJECTED
        if (
            by_dimension[QualityDimension.PARSE_COMPLETENESS] < self.config.reject_parse_threshold
            or by_dimension[QualityDimension.TEXT_INTEGRITY] < self.config.reject_text_threshold
        ):
            return QualityDecision.REJECTED
        if any(issue.severity is QualityIssueSeverity.HIGH for issue in issues):
            return QualityDecision.QUARANTINED
        if any(score.score < self.config.quarantine_dimension_threshold for score in dimensions):
            return QualityDecision.QUARANTINED
        if any(issue.severity is QualityIssueSeverity.WARNING for issue in issues):
            return QualityDecision.WARNING
        if any(score.score < self.config.warning_dimension_threshold for score in dimensions):
            return QualityDecision.WARNING
        return QualityDecision.PASSED


class QualityEngine:
    name = "quality-engine"
    version = "1.1.0"

    def __init__(
        self,
        config: QualityConfig | None = None,
        dingo_adapter: DingoAdapter | None = None,
    ) -> None:
        self.config = config or QualityConfig()
        self.dingo_adapter = dingo_adapter
        self.schema_validator = SchemaValidator()
        self.deterministic_evaluator = DeterministicRuleEvaluator(self.config)
        self.policy = QualityPolicyEngine(self.config)

    @property
    def config_snapshot(self) -> dict[str, Any]:
        return self.config.model_dump(mode="json")

    def evaluate(self, document: CanonicalDocument) -> QualityEvaluationResult:
        results = [
            self.schema_validator.evaluate(document),
            self.deterministic_evaluator.evaluate(document),
        ]
        executions: list[EvaluatorExecution] = [result.execution for result in results]

        if self.config.dingo_rule_enabled:
            if self.dingo_adapter is None:
                raise RuntimeError("Dingo rule evaluation is enabled without an adapter")
            dingo_rule_result = self.dingo_adapter.evaluate_rules(document)
            results.append(dingo_rule_result)
            executions.append(dingo_rule_result.execution)
        else:
            executions.append(
                skipped_dingo_execution(
                    evaluator_type="dingo_rule", reason="DINGO_RULE_ENABLED is false"
                )
            )

        if self.config.dingo_llm_enabled:
            if self.dingo_adapter is None:
                raise RuntimeError("Dingo LLM evaluation is enabled without an adapter")
            dingo_llm_result = self.dingo_adapter.evaluate_llm(document)
            results.append(dingo_llm_result)
            executions.append(dingo_llm_result.execution)
        else:
            executions.append(
                skipped_dingo_execution(
                    evaluator_type="dingo_llm", reason="DINGO_LLM_ENABLED is false"
                )
            )

        issues = [issue for result in results for issue in result.issues]
        dimensions = self._merge_dimensions(
            [score for result in results for score in result.scores]
        )
        decision = self.policy.decide(dimensions, issues)
        overall_score = sum(score.score for score in dimensions) / len(dimensions)
        return QualityEvaluationResult(
            decision=decision,
            overall_score=overall_score,
            dimensions=dimensions,
            issues=issues,
            evaluator_executions=executions,
        )

    @staticmethod
    def _merge_dimensions(scores: list[DimensionScore]) -> list[DimensionScore]:
        values: dict[QualityDimension, list[float]] = defaultdict(list)
        evidence: dict[QualityDimension, list[QualityEvidence]] = defaultdict(list)
        for score in scores:
            values[score.dimension].append(score.score)
            evidence[score.dimension].extend(score.evidence)
        merged: list[DimensionScore] = []
        base_dimensions = [
            dimension
            for dimension in QualityDimension
            if dimension is not QualityDimension.RETRIEVAL_READINESS
        ]
        for dimension in base_dimensions:
            dimension_values = values.get(dimension, [1.0])
            merged.append(
                DimensionScore(
                    dimension=dimension,
                    score=min(dimension_values),
                    evidence=evidence[dimension],
                )
            )
        base_average = sum(score.score for score in merged) / len(merged)
        retrieval_values = [base_average, *values.get(QualityDimension.RETRIEVAL_READINESS, [])]
        merged.append(
            DimensionScore(
                dimension=QualityDimension.RETRIEVAL_READINESS,
                score=min(retrieval_values),
                evidence=[
                    QualityEvidence(metric="base_dimension_average", value=base_average),
                    *evidence[QualityDimension.RETRIEVAL_READINESS],
                ],
            )
        )
        return merged
