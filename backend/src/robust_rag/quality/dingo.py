"""Dingo 2.5 Adapter isolated from internal quality domain contracts."""

import hashlib
import importlib
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol

from robust_rag.parsing.schemas import BlockType, CanonicalDocument
from robust_rag.quality.deterministic import EvaluatorResult
from robust_rag.quality.schemas import (
    DimensionScore,
    EvaluatorExecution,
    EvaluatorStatus,
    QualityDimension,
    QualityEvidence,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueSource,
    TokenUsage,
)

DINGO_ADAPTER_VERSION = "dingo-python/2.5.0-adapter/1.0.0"

RULE_IMPORTS = {
    "RuleAbnormalChar": (
        "dingo.model.rule.rule_common",
        "RuleAbnormalChar",
        QualityDimension.TEXT_INTEGRITY,
    ),
    "RuleAbnormalHtml": (
        "dingo.model.rule.rule_common",
        "RuleAbnormalHtml",
        QualityDimension.TEXT_INTEGRITY,
    ),
    "RuleContentNull": (
        "dingo.model.rule.rule_common",
        "RuleContentNull",
        QualityDimension.PARSE_COMPLETENESS,
    ),
    "RuleContentShort": (
        "dingo.model.rule.rule_common",
        "RuleContentShort",
        QualityDimension.INFORMATION_DENSITY,
    ),
}


class DingoAdapterError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class DingoAdapter(Protocol):
    version: str

    def evaluate_rules(self, document: CanonicalDocument) -> EvaluatorResult: ...

    def evaluate_llm(self, document: CanonicalDocument) -> EvaluatorResult: ...


@dataclass(frozen=True, slots=True)
class DingoConfig:
    rule_names: tuple[str, ...]
    llm_model: str
    llm_base_url: str
    llm_api_key: str | None
    llm_max_chars: int
    prompt_version: str = "dingo-llm-text-quality-v5/2.5.0"


class DingoPythonAdapter:
    """Call the official SDK while containing its global class configuration."""

    version = DINGO_ADAPTER_VERSION
    _llm_lock = threading.Lock()

    def __init__(self, config: DingoConfig) -> None:
        self.config = config

    def evaluate_rules(self, document: CanonicalDocument) -> EvaluatorResult:
        started = time.monotonic()
        content = _document_content(document)
        input_hash = _hash_text(content)
        data_type = self._load("dingo.io.input", "Data")
        data = data_type(
            data_id=document.document_version_id,
            prompt=document.title or "",
            content=content,
        )
        details: list[tuple[Any, QualityDimension]] = []
        try:
            for rule_name in self.config.rule_names:
                definition = RULE_IMPORTS.get(rule_name)
                if definition is None:
                    raise DingoAdapterError(
                        "DINGO_RULE_UNSUPPORTED",
                        f"Unsupported Dingo rule configured: {rule_name}",
                        retryable=False,
                    )
                module_name, class_name, dimension = definition
                rule = self._load(module_name, class_name)
                details.append((rule.eval(data), dimension))
        except DingoAdapterError:
            raise
        except Exception as exc:
            raise DingoAdapterError(
                "DINGO_RULE_EVALUATION_FAILED", str(exc), retryable=True
            ) from exc
        return _convert_results(
            name="dingo-rule-evaluator",
            evaluator_type="dingo_rule",
            source=QualityIssueSource.DINGO_RULE,
            severity=QualityIssueSeverity.WARNING,
            started=started,
            details=details,
            input_hash=input_hash,
            input_char_count=len(content),
        )

    def evaluate_llm(self, document: CanonicalDocument) -> EvaluatorResult:
        if not self.config.llm_api_key:
            raise DingoAdapterError(
                "DINGO_LLM_API_KEY_MISSING",
                "Dingo LLM evaluation is enabled but DINGO_LLM_API_KEY is missing",
                retryable=False,
            )
        started = time.monotonic()
        full_content = _document_content(document)
        content = _sample_content(document, self.config.llm_max_chars)
        input_hash = _hash_text(content)
        try:
            data_type = self._load("dingo.io.input", "Data")
            args_type = self._load("dingo.config.input_args", "EvaluatorLLMArgs")
            evaluator = self._load(
                "dingo.model.llm.text_quality.llm_text_quality_v5", "LLMTextQualityV5"
            )
            data = data_type(
                data_id=document.document_version_id,
                prompt=document.title or "",
                content=content,
            )
            with self._llm_lock:
                evaluator.dynamic_config = args_type(
                    key=self.config.llm_api_key,
                    api_url=self.config.llm_base_url,
                    model=self.config.llm_model,
                    temperature=0,
                )
                evaluator.client = None
                detail = evaluator.eval(data)
            _raise_if_dingo_wrapped_error(detail)
        except DingoAdapterError:
            raise
        except Exception as exc:
            raise DingoAdapterError(
                "DINGO_LLM_EVALUATION_FAILED", str(exc), retryable=True
            ) from exc
        result = _convert_results(
            name="dingo-llm-text-quality-v5",
            evaluator_type="dingo_llm",
            source=QualityIssueSource.DINGO_LLM,
            severity=QualityIssueSeverity.HIGH,
            started=started,
            details=[(detail, _llm_dimension(detail))],
            input_hash=input_hash,
            input_char_count=len(content),
            input_truncated=len(content) < len(full_content),
            model=self.config.llm_model,
            prompt_version=self.config.prompt_version,
        )
        return result

    @staticmethod
    def _load(module_name: str, attribute: str) -> Any:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            raise DingoAdapterError(
                "DINGO_NOT_INSTALLED",
                "Install the optional 'dingo' dependency before enabling Dingo",
                retryable=False,
            ) from exc
        try:
            return getattr(module, attribute)
        except AttributeError as exc:
            raise DingoAdapterError(
                "DINGO_CONTRACT_MISMATCH",
                f"Dingo 2.5 contract is missing {module_name}.{attribute}",
                retryable=False,
            ) from exc


class FakeDingoAdapter:
    """Deterministic no-cost adapter for tests and local contract exercises."""

    version = "fake-dingo/1.0.0"

    def __init__(
        self,
        *,
        rule_result: EvaluatorResult,
        llm_result: EvaluatorResult,
        rule_error: DingoAdapterError | None = None,
        llm_error: DingoAdapterError | None = None,
    ) -> None:
        self.rule_result = rule_result
        self.llm_result = llm_result
        self.rule_error = rule_error
        self.llm_error = llm_error

    def evaluate_rules(self, document: CanonicalDocument) -> EvaluatorResult:
        del document
        if self.rule_error is not None:
            raise self.rule_error
        return self.rule_result

    def evaluate_llm(self, document: CanonicalDocument) -> EvaluatorResult:
        del document
        if self.llm_error is not None:
            raise self.llm_error
        return self.llm_result


def skipped_dingo_execution(*, evaluator_type: str, reason: str) -> EvaluatorExecution:
    name = "dingo-rule-evaluator" if evaluator_type == "dingo_rule" else "dingo-llm-text-quality-v5"
    return EvaluatorExecution(
        name=name,
        version=DINGO_ADAPTER_VERSION,
        evaluator_type=evaluator_type,
        status=EvaluatorStatus.SKIPPED,
        duration_ms=0,
        issue_count=0,
        error={"code": "EVALUATOR_DISABLED", "message": reason, "retryable": False},
    )


def _convert_results(
    *,
    name: str,
    evaluator_type: str,
    source: QualityIssueSource,
    severity: QualityIssueSeverity,
    started: float,
    details: list[tuple[Any, QualityDimension]],
    input_hash: str,
    input_char_count: int,
    input_truncated: bool = False,
    model: str | None = None,
    prompt_version: str | None = None,
) -> EvaluatorResult:
    issues: list[QualityIssue] = []
    scores_by_dimension: dict[QualityDimension, list[float]] = defaultdict(list)
    evidence_by_dimension: dict[QualityDimension, list[QualityEvidence]] = defaultdict(list)
    raw_results: list[dict[str, Any]] = []
    usages: list[Any] = []
    for detail, dimension in details:
        raw = detail.model_dump(mode="json") if hasattr(detail, "model_dump") else dict(detail)
        raw_results.append(raw)
        status = bool(getattr(detail, "status", False))
        score_value = getattr(detail, "score", None)
        score = float(score_value) if score_value is not None else (0.0 if status else 1.0)
        score = min(1.0, max(0.0, score))
        labels = [str(value) for value in (getattr(detail, "label", None) or [])]
        reasons = [str(value) for value in (getattr(detail, "reason", None) or [])]
        evidence = QualityEvidence(
            metric=str(getattr(detail, "metric", name)),
            value=score,
            details={"reasons": reasons},
        )
        scores_by_dimension[dimension].append(score)
        evidence_by_dimension[dimension].append(evidence)
        if status:
            issues.append(
                QualityIssue(
                    code=f"DINGO_{str(getattr(detail, 'metric', name)).upper()}",
                    dimension=dimension,
                    severity=severity,
                    source=source,
                    evaluator=name,
                    evaluator_version=DINGO_ADAPTER_VERSION,
                    message=reasons[0] if reasons else "Dingo reported a quality issue",
                    evidence=[evidence],
                    labels=labels,
                )
            )
        usage = getattr(detail, "usage", None)
        if usage is not None:
            usages.append(usage)
    scores = [
        DimensionScore(
            dimension=dimension,
            score=min(values),
            evidence=evidence_by_dimension[dimension],
        )
        for dimension, values in scores_by_dimension.items()
    ]
    return EvaluatorResult(
        scores=scores,
        issues=issues,
        execution=EvaluatorExecution(
            name=name,
            version=DINGO_ADAPTER_VERSION,
            evaluator_type=evaluator_type,
            status=EvaluatorStatus.SUCCEEDED,
            model=model,
            prompt_version=prompt_version,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            issue_count=len(issues),
            input_hash=input_hash,
            input_char_count=input_char_count,
            input_truncated=input_truncated,
            usage=_merge_usage(usages),
            raw_results=raw_results,
        ),
    )


def _merge_usage(usages: list[Any]) -> TokenUsage | None:
    if not usages:
        return None

    def total(field: str) -> int | None:
        values = [getattr(usage, field, None) for usage in usages]
        if all(value is None for value in values):
            return None
        return sum(int(value or 0) for value in values)

    return TokenUsage(
        input_tokens=total("prompt_tokens"),
        output_tokens=total("completion_tokens"),
        total_tokens=total("total_tokens"),
        reasoning_tokens=total("reasoning_tokens"),
        cached_tokens=total("cached_tokens"),
        calls=sum(int(getattr(usage, "calls", 1) or 1) for usage in usages),
    )


def _raise_if_dingo_wrapped_error(detail: Any) -> None:
    labels = [str(value) for value in (getattr(detail, "label", None) or [])]
    exception_labels = [label for label in labels if label.startswith("QUALITY_BAD.")]
    if not exception_labels:
        return
    reasons = [str(value) for value in (getattr(detail, "reason", None) or [])]
    raise DingoAdapterError(
        "DINGO_LLM_PROVIDER_ERROR",
        reasons[0] if reasons else exception_labels[0],
        retryable=True,
    )


def _llm_dimension(detail: Any) -> QualityDimension:
    labels = " ".join(str(value).lower() for value in (getattr(detail, "label", None) or []))
    if "similarity" in labels or "duplicate" in labels:
        return QualityDimension.DUPLICATION
    if "completeness" in labels or "formula" in labels or "table" in labels or "code" in labels:
        return QualityDimension.CONTEXT_COMPLETENESS
    if "effectiveness" in labels or "garbled" in labels or "words_stuck" in labels:
        return QualityDimension.TEXT_INTEGRITY
    return QualityDimension.RETRIEVAL_READINESS


def _document_content(document: CanonicalDocument) -> str:
    return "\n\n".join(
        block.normalized_text
        for block in document.blocks
        if block.block_type is not BlockType.DOCUMENT and block.normalized_text.strip()
    )


def _sample_content(document: CanonicalDocument, max_chars: int) -> str:
    blocks = [
        block.normalized_text
        for block in document.blocks
        if block.block_type is not BlockType.DOCUMENT and block.normalized_text.strip()
    ]
    full = "\n\n".join(blocks)
    if len(full) <= max_chars:
        return full
    half = max(1, max_chars // 2)
    return f"{full[:half]}\n\n[... deterministic middle truncation ...]\n\n{full[-half:]}"[
        :max_chars
    ]


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
