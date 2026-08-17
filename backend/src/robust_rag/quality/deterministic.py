"""Schema and deterministic document quality evaluators."""

import re
import time
from dataclasses import dataclass
from typing import Any

from robust_rag.parsing.schemas import BlockType, CanonicalBlock, CanonicalDocument, SourceType
from robust_rag.quality.schemas import (
    DimensionScore,
    EvaluatorExecution,
    EvaluatorStatus,
    QualityDimension,
    QualityEvidence,
    QualityIssue,
    QualityIssueSeverity,
    QualityIssueSource,
)

CONTAINER_TYPES = {
    BlockType.DOCUMENT,
    BlockType.SECTION,
    BlockType.PAGE,
    BlockType.SLIDE,
    BlockType.SHEET,
}


@dataclass(frozen=True, slots=True)
class EvaluatorResult:
    scores: list[DimensionScore]
    issues: list[QualityIssue]
    execution: EvaluatorExecution


class SchemaValidator:
    name = "canonical-schema-validator"
    version = "1.0.0"

    def evaluate(self, document: CanonicalDocument) -> EvaluatorResult:
        started = time.monotonic()
        issues: list[QualityIssue] = []
        ids = [block.id for block in document.blocks]
        known_ids = set(ids)
        if len(ids) != len(known_ids):
            issues.append(
                self._issue(
                    "DUPLICATE_BLOCK_IDS",
                    QualityIssueSeverity.CRITICAL,
                    "Canonical document contains duplicate Block IDs",
                    [QualityEvidence(metric="duplicate_id_count", value=len(ids) - len(known_ids))],
                )
            )
        root = next((block for block in document.blocks if block.id == document.root_node_id), None)
        if root is None or root.block_type is not BlockType.DOCUMENT:
            issues.append(
                self._issue(
                    "CANONICAL_ROOT_INVALID",
                    QualityIssueSeverity.CRITICAL,
                    "Canonical root node is missing or has the wrong type",
                    [QualityEvidence(metric="root_node_id", value=document.root_node_id)],
                )
            )
        orphan_ids = [
            block.id
            for block in document.blocks
            if block.id != document.root_node_id and block.parent_id not in known_ids
        ]
        if orphan_ids:
            issues.append(
                self._issue(
                    "ORPHAN_BLOCKS",
                    QualityIssueSeverity.HIGH,
                    "Canonical document contains blocks with unknown parents",
                    [
                        QualityEvidence(
                            metric="orphan_block_ratio",
                            value=len(orphan_ids) / max(1, len(document.blocks)),
                            block_ids=orphan_ids,
                        )
                    ],
                )
            )
        cycle_ids = self._cycle_ids(document)
        if cycle_ids:
            issues.append(
                self._issue(
                    "BLOCK_PARENT_CYCLE",
                    QualityIssueSeverity.CRITICAL,
                    "Canonical parent relationships contain a cycle",
                    [
                        QualityEvidence(
                            metric="cycle_block_count",
                            value=len(cycle_ids),
                            block_ids=cycle_ids,
                        )
                    ],
                )
            )
        score = 1.0 - min(1.0, len(orphan_ids) / max(1, len(document.blocks)))
        if any(issue.severity is QualityIssueSeverity.CRITICAL for issue in issues):
            score = 0.0
        evidence = [
            QualityEvidence(metric="block_count", value=len(document.blocks)),
            QualityEvidence(
                metric="orphan_block_count", value=len(orphan_ids), block_ids=orphan_ids
            ),
            QualityEvidence(metric="cycle_block_count", value=len(cycle_ids), block_ids=cycle_ids),
        ]
        execution = EvaluatorExecution(
            name=self.name,
            version=self.version,
            evaluator_type="schema",
            status=EvaluatorStatus.SUCCEEDED,
            duration_ms=_duration_ms(started),
            issue_count=len(issues),
        )
        return EvaluatorResult(
            scores=[
                DimensionScore(
                    dimension=QualityDimension.STRUCTURE_INTEGRITY,
                    score=score,
                    evidence=evidence,
                )
            ],
            issues=issues,
            execution=execution,
        )

    def _issue(
        self,
        code: str,
        severity: QualityIssueSeverity,
        message: str,
        evidence: list[QualityEvidence],
    ) -> QualityIssue:
        return QualityIssue(
            code=code,
            dimension=QualityDimension.STRUCTURE_INTEGRITY,
            severity=severity,
            source=QualityIssueSource.SCHEMA,
            evaluator=self.name,
            evaluator_version=self.version,
            message=message,
            evidence=evidence,
        )

    @staticmethod
    def _cycle_ids(document: CanonicalDocument) -> list[str]:
        parent_by_id = {block.id: block.parent_id for block in document.blocks}
        cycles: set[str] = set()
        for block_id in parent_by_id:
            path: set[str] = set()
            current: str | None = block_id
            while current is not None and current in parent_by_id:
                if current in path:
                    cycles.update(path)
                    break
                path.add(current)
                current = parent_by_id[current]
        return sorted(cycles)


class DeterministicRuleEvaluator:
    name = "deterministic-document-quality-rules"
    version = "1.0.0"

    def __init__(self, config: Any) -> None:
        self.config = config

    def evaluate(self, document: CanonicalDocument) -> EvaluatorResult:
        started = time.monotonic()
        content_blocks = [
            block for block in document.blocks if block.block_type not in CONTAINER_TYPES
        ]
        valid_blocks = [block for block in content_blocks if block.normalized_text.strip()]
        joined = "\n".join(block.normalized_text for block in valid_blocks)
        issues: list[QualityIssue] = []

        parse_score = len(valid_blocks) / max(1, len(content_blocks))
        if not joined.strip():
            issues.append(
                self._issue(
                    code="NO_VALID_TEXT",
                    dimension=QualityDimension.PARSE_COMPLETENESS,
                    severity=QualityIssueSeverity.CRITICAL,
                    message="Document contains no valid normalized text",
                    evidence=[QualityEvidence(metric="valid_text_char_count", value=0)],
                )
            )

        corrupt_count = len(re.findall(r"[\ufffd\u25a1\u25a0]", joined))
        nonspace_count = len(re.sub(r"\s", "", joined))
        corrupt_ratio = corrupt_count / max(1, nonspace_count)
        text_score = max(
            0.0,
            1.0 - corrupt_ratio / max(self.config.corruption_quarantine_ratio, 0.0001),
        )
        if corrupt_ratio >= self.config.corruption_reject_ratio:
            issues.append(
                self._issue(
                    code="UNRECOVERABLE_TEXT_CORRUPTION",
                    dimension=QualityDimension.TEXT_INTEGRITY,
                    severity=QualityIssueSeverity.CRITICAL,
                    message="Text corruption ratio exceeds the rejection threshold",
                    evidence=[
                        QualityEvidence(
                            metric="corruption_ratio",
                            value=corrupt_ratio,
                            threshold=self.config.corruption_reject_ratio,
                        )
                    ],
                )
            )
        elif corrupt_ratio >= self.config.corruption_quarantine_ratio:
            issues.append(
                self._issue(
                    code="HIGH_TEXT_CORRUPTION",
                    dimension=QualityDimension.TEXT_INTEGRITY,
                    severity=QualityIssueSeverity.HIGH,
                    message="Text corruption ratio exceeds the quarantine threshold",
                    evidence=[
                        QualityEvidence(
                            metric="corruption_ratio",
                            value=corrupt_ratio,
                            threshold=self.config.corruption_quarantine_ratio,
                        )
                    ],
                )
            )
        elif corrupt_count:
            issues.append(
                self._issue(
                    code="TEXT_CORRUPTION_DETECTED",
                    dimension=QualityDimension.TEXT_INTEGRITY,
                    severity=QualityIssueSeverity.WARNING,
                    message="A small amount of replacement or box characters remains",
                    evidence=[QualityEvidence(metric="corruption_ratio", value=corrupt_ratio)],
                )
            )

        duplicate_ids = [
            block.id for block in content_blocks if _has_cleaning_flag(block, "exact_duplicate")
        ]
        near_duplicate_ids = [
            block.id for block in content_blocks if _has_cleaning_flag(block, "near_duplicate")
        ]
        duplicate_ratio = len(set(duplicate_ids + near_duplicate_ids)) / max(1, len(content_blocks))
        duplication_score = max(0.0, 1.0 - duplicate_ratio)
        if duplicate_ratio >= self.config.duplicate_quarantine_ratio:
            issues.append(
                self._issue(
                    code="HIGH_DUPLICATION",
                    dimension=QualityDimension.DUPLICATION,
                    severity=QualityIssueSeverity.HIGH,
                    message="Duplicate Block ratio exceeds the quarantine threshold",
                    evidence=[
                        QualityEvidence(
                            metric="duplicate_block_ratio",
                            value=duplicate_ratio,
                            threshold=self.config.duplicate_quarantine_ratio,
                            block_ids=sorted(set(duplicate_ids + near_duplicate_ids)),
                        )
                    ],
                )
            )
        elif duplicate_ratio > 0:
            issues.append(
                self._issue(
                    code="DUPLICATION_DETECTED",
                    dimension=QualityDimension.DUPLICATION,
                    severity=QualityIssueSeverity.WARNING,
                    message="Duplicate or near-duplicate Blocks were detected",
                    evidence=[
                        QualityEvidence(
                            metric="duplicate_block_ratio",
                            value=duplicate_ratio,
                            block_ids=sorted(set(duplicate_ids + near_duplicate_ids)),
                        )
                    ],
                )
            )

        incomplete_locator_ids = [
            block.id for block in content_blocks if not _has_specific_locator(block)
        ]
        missing_locator_ratio = len(incomplete_locator_ids) / max(1, len(content_blocks))
        traceability_score = max(0.0, 1.0 - missing_locator_ratio)
        if missing_locator_ratio >= self.config.missing_locator_quarantine_ratio:
            issues.append(
                self._issue(
                    code="SOURCE_TRACEABILITY_LOW",
                    dimension=QualityDimension.SOURCE_TRACEABILITY,
                    severity=QualityIssueSeverity.HIGH,
                    message="Too many content Blocks lack usable source positions",
                    evidence=[
                        QualityEvidence(
                            metric="missing_locator_ratio",
                            value=missing_locator_ratio,
                            threshold=self.config.missing_locator_quarantine_ratio,
                            block_ids=incomplete_locator_ids,
                        )
                    ],
                )
            )
        elif incomplete_locator_ids:
            issues.append(
                self._issue(
                    code="SOURCE_LOCATOR_GAPS",
                    dimension=QualityDimension.SOURCE_TRACEABILITY,
                    severity=QualityIssueSeverity.WARNING,
                    message="Some content Blocks lack usable source positions",
                    evidence=[
                        QualityEvidence(
                            metric="missing_locator_ratio",
                            value=missing_locator_ratio,
                            block_ids=incomplete_locator_ids,
                        )
                    ],
                )
            )

        page_numbers = {
            locator.page_number
            for block in document.blocks
            for locator in block.source_locators
            if locator.page_number is not None
        }
        pages_with_text = {
            locator.page_number
            for block in valid_blocks
            for locator in block.source_locators
            if locator.page_number is not None
        }
        empty_page_ratio = (
            len(page_numbers - pages_with_text) / len(page_numbers) if page_numbers else 0.0
        )
        if empty_page_ratio >= self.config.empty_page_quarantine_ratio:
            issues.append(
                self._issue(
                    code="HIGH_EMPTY_PAGE_RATIO",
                    dimension=QualityDimension.PARSE_COMPLETENESS,
                    severity=QualityIssueSeverity.HIGH,
                    message="Too many source pages contain no valid text",
                    evidence=[
                        QualityEvidence(
                            metric="empty_page_ratio",
                            value=empty_page_ratio,
                            threshold=self.config.empty_page_quarantine_ratio,
                        )
                    ],
                )
            )
            parse_score = min(parse_score, 1.0 - empty_page_ratio)

        low_confidence_ids = [
            block.id
            for block in content_blocks
            if block.parser_confidence is not None
            and block.parser_confidence < self.config.parser_confidence_warning
        ]
        low_confidence_ratio = len(low_confidence_ids) / max(1, len(content_blocks))
        if low_confidence_ratio >= self.config.low_confidence_quarantine_ratio:
            issues.append(
                self._issue(
                    code="OCR_CONFIDENCE_LOW",
                    dimension=QualityDimension.TEXT_INTEGRITY,
                    severity=QualityIssueSeverity.HIGH,
                    message="Low-confidence parsed text exceeds the quarantine threshold",
                    evidence=[
                        QualityEvidence(
                            metric="low_confidence_block_ratio",
                            value=low_confidence_ratio,
                            threshold=self.config.low_confidence_quarantine_ratio,
                            block_ids=low_confidence_ids,
                        )
                    ],
                )
            )
            text_score = min(text_score, 1.0 - low_confidence_ratio)
        elif low_confidence_ids:
            issues.append(
                self._issue(
                    code="OCR_CONFIDENCE_WARNING",
                    dimension=QualityDimension.TEXT_INTEGRITY,
                    severity=QualityIssueSeverity.WARNING,
                    message="Some parsed text has low confidence",
                    evidence=[
                        QualityEvidence(
                            metric="low_confidence_block_ratio",
                            value=low_confidence_ratio,
                            block_ids=low_confidence_ids,
                        )
                    ],
                )
            )

        meaningful_count = len(re.findall(r"[\w\u3400-\u9fff]", joined, flags=re.UNICODE))
        density = meaningful_count / max(1, nonspace_count)
        density_score = min(1.0, density / max(self.config.information_density_warning, 0.0001))
        if joined and density < self.config.information_density_quarantine:
            issues.append(
                self._issue(
                    code="INFORMATION_DENSITY_LOW",
                    dimension=QualityDimension.INFORMATION_DENSITY,
                    severity=QualityIssueSeverity.HIGH,
                    message="Document information density is below the quarantine threshold",
                    evidence=[
                        QualityEvidence(
                            metric="information_density",
                            value=density,
                            threshold=self.config.information_density_quarantine,
                        )
                    ],
                )
            )
        elif joined and density < self.config.information_density_warning:
            issues.append(
                self._issue(
                    code="INFORMATION_DENSITY_WARNING",
                    dimension=QualityDimension.INFORMATION_DENSITY,
                    severity=QualityIssueSeverity.WARNING,
                    message="Document information density is below the warning threshold",
                    evidence=[QualityEvidence(metric="information_density", value=density)],
                )
            )

        heading_jump_ids = _heading_jump_ids(document.blocks)
        if heading_jump_ids:
            issues.append(
                self._issue(
                    code="HEADING_LEVEL_JUMP",
                    dimension=QualityDimension.STRUCTURE_INTEGRITY,
                    severity=QualityIssueSeverity.WARNING,
                    message="Heading hierarchy contains skipped levels",
                    evidence=[
                        QualityEvidence(
                            metric="heading_level_jump_count",
                            value=len(heading_jump_ids),
                            block_ids=heading_jump_ids,
                        )
                    ],
                )
            )

        context_score = _context_score(document, valid_blocks)
        structure_score = max(0.0, 1.0 - len(heading_jump_ids) / max(1, len(content_blocks)))
        scores = [
            _score(QualityDimension.PARSE_COMPLETENESS, parse_score, "valid_block_ratio"),
            _score(QualityDimension.TEXT_INTEGRITY, text_score, "text_integrity_score"),
            _score(QualityDimension.STRUCTURE_INTEGRITY, structure_score, "structure_score"),
            _score(QualityDimension.DUPLICATION, duplication_score, "unique_block_ratio"),
            _score(QualityDimension.INFORMATION_DENSITY, density_score, "density_score"),
            _score(QualityDimension.CONTEXT_COMPLETENESS, context_score, "context_score"),
            _score(QualityDimension.SOURCE_TRACEABILITY, traceability_score, "traceability_score"),
        ]
        execution = EvaluatorExecution(
            name=self.name,
            version=self.version,
            evaluator_type="deterministic_rule",
            status=EvaluatorStatus.SUCCEEDED,
            duration_ms=_duration_ms(started),
            issue_count=len(issues),
            raw_results=[
                {
                    "content_block_count": len(content_blocks),
                    "valid_block_count": len(valid_blocks),
                    "character_count": len(joined),
                    "empty_page_ratio": empty_page_ratio,
                    "corruption_ratio": corrupt_ratio,
                    "duplicate_ratio": duplicate_ratio,
                    "missing_locator_ratio": missing_locator_ratio,
                    "low_confidence_ratio": low_confidence_ratio,
                    "information_density": density,
                }
            ],
        )
        return EvaluatorResult(scores=scores, issues=issues, execution=execution)

    def _issue(
        self,
        *,
        code: str,
        dimension: QualityDimension,
        severity: QualityIssueSeverity,
        message: str,
        evidence: list[QualityEvidence],
    ) -> QualityIssue:
        return QualityIssue(
            code=code,
            dimension=dimension,
            severity=severity,
            source=QualityIssueSource.DETERMINISTIC,
            evaluator=self.name,
            evaluator_version=self.version,
            message=message,
            evidence=evidence,
        )


def _score(dimension: QualityDimension, score: float, metric: str) -> DimensionScore:
    bounded = min(1.0, max(0.0, score))
    return DimensionScore(
        dimension=dimension,
        score=bounded,
        evidence=[QualityEvidence(metric=metric, value=bounded)],
    )


def _has_cleaning_flag(block: CanonicalBlock, flag: str) -> bool:
    cleaning = block.attributes.get("cleaning", {})
    return isinstance(cleaning, dict) and flag in cleaning.get("flags", [])


def _has_specific_locator(block: CanonicalBlock) -> bool:
    for locator in block.source_locators:
        if locator.source_type is SourceType.PDF and locator.page_number is not None:
            return True
        if locator.source_type is SourceType.POWERPOINT and locator.slide_number is not None:
            return True
        if locator.source_type is SourceType.EXCEL and locator.sheet_name is not None:
            return True
        if locator.source_type is SourceType.HTML and locator.dom_path is not None:
            return True
        if locator.source_type in {SourceType.MARKDOWN, SourceType.TEXT} and locator.line_start:
            return True
        if locator.source_type is SourceType.WORD and (
            locator.paragraph_index is not None or locator.table_index is not None
        ):
            return True
    return False


def _heading_jump_ids(blocks: list[CanonicalBlock]) -> list[str]:
    previous_level = 0
    invalid: list[str] = []
    for block in blocks:
        if block.block_type is not BlockType.HEADING:
            continue
        level = int(block.attributes.get("level", 1))
        if previous_level and level > previous_level + 1:
            invalid.append(block.id)
        previous_level = level
    return invalid


def _context_score(document: CanonicalDocument, valid_blocks: list[CanonicalBlock]) -> float:
    if not valid_blocks:
        return 0.0
    title_score = 1.0 if document.title and document.title.strip() else 0.5
    contextualized = sum(
        1 for block in valid_blocks if block.heading_path or block.block_type is BlockType.HEADING
    )
    hierarchy_score = contextualized / len(valid_blocks)
    return (title_score + hierarchy_score) / 2


def _duration_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
