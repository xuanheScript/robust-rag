import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from robust_rag.parsing.schemas import (
    BlockType,
    CanonicalBlock,
    CanonicalDocument,
    SourceLocator,
    SourceType,
)
from robust_rag.quality.deterministic import EvaluatorResult
from robust_rag.quality.dingo import (
    DingoAdapterError,
    DingoConfig,
    DingoPythonAdapter,
    FakeDingoAdapter,
)
from robust_rag.quality.engine import QualityConfig, QualityEngine
from robust_rag.quality.schemas import (
    DimensionScore,
    EvaluatorExecution,
    EvaluatorStatus,
    QualityDecision,
    QualityDimension,
    QualityEvidence,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueSource,
)


def _block(
    block_id: str,
    block_type: BlockType,
    text: str,
    *,
    parent_id: str | None,
    line: int | None = None,
    heading_path: list[str] | None = None,
    attributes: dict[str, object] | None = None,
) -> CanonicalBlock:
    locators = (
        [
            SourceLocator(
                source_type=SourceType.TEXT,
                line_start=line,
                line_end=line,
            )
        ]
        if line is not None
        else []
    )
    return CanonicalBlock(
        id=block_id,
        block_type=block_type,
        parent_id=parent_id,
        semantic_order=0,
        heading_path=heading_path or [],
        original_text=text,
        normalized_text=text,
        source_locators=locators,
        attributes=attributes or {},
        token_count=len(text),
    )


def quality_document() -> CanonicalDocument:
    return CanonicalDocument(
        document_id=str(uuid.uuid4()),
        document_version_id=str(uuid.uuid4()),
        title="Quality fixture",
        root_node_id="root",
        blocks=[
            _block("root", BlockType.DOCUMENT, "", parent_id=None),
            _block(
                "heading",
                BlockType.HEADING,
                "Policy",
                parent_id="root",
                line=1,
                attributes={"level": 1},
            ),
            _block(
                "paragraph-1",
                BlockType.PARAGRAPH,
                "The policy defines a complete and traceable business process.",
                parent_id="root",
                line=2,
                heading_path=["Policy"],
            ),
            _block(
                "paragraph-2",
                BlockType.PARAGRAPH,
                "所有关键事实均具有清晰来源,适合进入后续检索流程。",
                parent_id="root",
                line=3,
                heading_path=["Policy"],
            ),
        ],
    )


def _empty_dingo_result(evaluator_type: str) -> EvaluatorResult:
    return EvaluatorResult(
        scores=[],
        issues=[],
        execution=EvaluatorExecution(
            name=f"fake-{evaluator_type}",
            version="1.0",
            evaluator_type=evaluator_type,
            status=EvaluatorStatus.SUCCEEDED,
            duration_ms=1,
            issue_count=0,
        ),
    )


def _bad_llm_result() -> EvaluatorResult:
    evidence = QualityEvidence(metric="LLMTextQualityV5", value=0.0)
    return EvaluatorResult(
        scores=[
            DimensionScore(
                dimension=QualityDimension.RETRIEVAL_READINESS,
                score=0.0,
                evidence=[evidence],
            )
        ],
        issues=[
            QualityIssue(
                code="DINGO_LLMTEXTQUALITYV5",
                dimension=QualityDimension.RETRIEVAL_READINESS,
                severity=QualityIssueSeverity.HIGH,
                source=QualityIssueSource.DINGO_LLM,
                evaluator="fake-dingo-llm",
                evaluator_version="1.0",
                message="High-risk semantic quality issue",
                evidence=[evidence],
            )
        ],
        execution=EvaluatorExecution(
            name="fake-dingo-llm",
            version="1.0",
            evaluator_type="dingo_llm",
            status=EvaluatorStatus.SUCCEEDED,
            duration_ms=1,
            issue_count=1,
        ),
    )


class _DingoUsage(BaseModel):
    prompt_tokens: int = 11
    completion_tokens: int = 3
    total_tokens: int = 14
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    calls: int = 1


class _DingoDetail(BaseModel):
    metric: str
    status: bool
    score: float | None = None
    label: list[str] | None = None
    reason: list[str] | None = None
    usage: _DingoUsage | None = None


class _DingoData:
    def __init__(self, *, data_id: str, prompt: str, content: str) -> None:
        self.data_id = data_id
        self.prompt = prompt
        self.content = content


class _DingoArgs:
    def __init__(self, **values: object) -> None:
        self.values = values


class _BadDingoRule:
    @classmethod
    def eval(cls, data: _DingoData) -> _DingoDetail:
        del data
        return _DingoDetail(
            metric="RuleAbnormalChar",
            status=True,
            label=["QUALITY_BAD_EFFECTIVENESS.RuleAbnormalChar"],
            reason=["garbled text"],
        )


class _GoodDingoLLM:
    dynamic_config: object | None = None
    client: object | None = None

    @classmethod
    def eval(cls, data: _DingoData) -> _DingoDetail:
        del data
        return _DingoDetail(
            metric="LLMTextQualityV5",
            status=False,
            score=1,
            label=["QUALITY_GOOD"],
            reason=["suitable"],
            usage=_DingoUsage(),
        )


def test_quality_engine_passes_clean_document_and_records_skipped_dingo() -> None:
    result = QualityEngine().evaluate(quality_document())

    assert result.decision is QualityDecision.PASSED
    assert len(result.dimensions) == len(QualityDimension)
    assert result.overall_score == pytest.approx(1.0)
    assert [execution.status for execution in result.evaluator_executions[-2:]] == [
        EvaluatorStatus.SKIPPED,
        EvaluatorStatus.SKIPPED,
    ]


def test_quality_engine_warns_for_limited_duplication() -> None:
    document = quality_document()
    document.blocks[-1].attributes["cleaning"] = {"flags": ["exact_duplicate"]}
    engine = QualityEngine(QualityConfig(duplicate_quarantine_ratio=0.8))

    result = engine.evaluate(document)

    assert result.decision is QualityDecision.WARNING
    assert "DUPLICATION_DETECTED" in {issue.code for issue in result.issues}


def test_quality_engine_quarantines_sparse_extraction_from_large_source() -> None:
    document = quality_document().model_copy(update={"metadata": {"source_file_size": 5_869_778}})

    result = QualityEngine().evaluate(document)

    assert result.decision is QualityDecision.QUARANTINED
    assert "SUSPICIOUSLY_SPARSE_EXTRACTION" in {issue.code for issue in result.issues}


def test_small_text_corruption_warns_without_quarantine() -> None:
    document = quality_document()
    paragraph = document.blocks[-1]
    extended_text = f"{paragraph.normalized_text}{'正常内容' * 300}\ufffd"
    document.blocks[-1] = paragraph.model_copy(
        update={"original_text": extended_text, "normalized_text": extended_text}
    )
    engine = QualityEngine(
        QualityConfig(
            corruption_warning_ratio=0.0001,
            corruption_quarantine_ratio=0.05,
        )
    )

    result = engine.evaluate(document)

    assert result.decision is QualityDecision.WARNING
    assert "TEXT_CORRUPTION_DETECTED" in {issue.code for issue in result.issues}


def test_quality_engine_quarantines_missing_sources_and_dingo_high_risk() -> None:
    document = quality_document()
    for block in document.blocks[1:]:
        block.source_locators = []
    missing_source = QualityEngine().evaluate(document)
    assert missing_source.decision is QualityDecision.QUARANTINED
    assert "SOURCE_TRACEABILITY_LOW" in {issue.code for issue in missing_source.issues}

    adapter = FakeDingoAdapter(
        rule_result=_empty_dingo_result("dingo_rule"),
        llm_result=_bad_llm_result(),
    )
    dingo_result = QualityEngine(
        QualityConfig(dingo_llm_enabled=True), dingo_adapter=adapter
    ).evaluate(quality_document())
    assert dingo_result.decision is QualityDecision.QUARANTINED
    assert dingo_result.evaluator_executions[-1].status is EvaluatorStatus.SUCCEEDED


def test_quality_engine_rejects_empty_text_and_invalid_structure() -> None:
    empty = quality_document()
    for block in empty.blocks[1:]:
        block.original_text = ""
        block.normalized_text = ""
    result = QualityEngine().evaluate(empty)
    assert result.decision is QualityDecision.REJECTED
    assert "NO_VALID_TEXT" in {issue.code for issue in result.issues}

    cyclic = quality_document()
    cyclic.blocks[1].parent_id = cyclic.blocks[1].id
    result = QualityEngine().evaluate(cyclic)
    assert result.decision is QualityDecision.REJECTED
    assert "BLOCK_PARENT_CYCLE" in {issue.code for issue in result.issues}


def test_fake_dingo_failure_remains_a_runtime_failure() -> None:
    failure = DingoAdapterError("DINGO_TIMEOUT", "timed out", retryable=True)
    adapter = FakeDingoAdapter(
        rule_result=_empty_dingo_result("dingo_rule"),
        llm_result=_empty_dingo_result("dingo_llm"),
        rule_error=failure,
    )
    engine = QualityEngine(QualityConfig(dingo_rule_enabled=True), dingo_adapter=adapter)

    with pytest.raises(DingoAdapterError) as captured:
        engine.evaluate(quality_document())

    assert captured.value.code == "DINGO_TIMEOUT"
    assert captured.value.retryable is True


def test_dingo_adapter_converts_official_status_score_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imports: dict[tuple[str, str], Any] = {
        ("dingo.io.input", "Data"): _DingoData,
        ("dingo.model.rule.rule_common", "RuleAbnormalChar"): _BadDingoRule,
        ("dingo.config.input_args", "EvaluatorLLMArgs"): _DingoArgs,
        (
            "dingo.model.llm.text_quality.llm_text_quality_v5",
            "LLMTextQualityV5",
        ): _GoodDingoLLM,
    }
    monkeypatch.setattr(
        DingoPythonAdapter,
        "_load",
        staticmethod(lambda module, attribute: imports[(module, attribute)]),
    )
    adapter = DingoPythonAdapter(
        DingoConfig(
            rule_names=("RuleAbnormalChar",),
            llm_model="fixture-model",
            llm_base_url="http://llm.test/v1",
            llm_api_key="fixture-key",
            llm_max_chars=1000,
        )
    )

    rule_result = adapter.evaluate_rules(quality_document())
    llm_result = adapter.evaluate_llm(quality_document())

    assert rule_result.scores[0].score == 0
    assert rule_result.issues[0].code == "DINGO_RULEABNORMALCHAR"
    assert llm_result.scores[0].score == 1
    assert llm_result.issues == []
    assert llm_result.execution.usage is not None
    assert llm_result.execution.usage.total_tokens == 14
