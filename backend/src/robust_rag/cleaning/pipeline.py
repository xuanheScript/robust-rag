"""Deterministic structure-aware cleaning pipeline."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from robust_rag.cleaning.operators import (
    CleaningOperator,
    ControlCharacterCleaner,
    EmptyBlockRemover,
    ExactDuplicateMarker,
    LanguageDetector,
    NearDuplicateMarker,
    ReadingOrderCorrector,
    RepeatedBoilerplateRemover,
    SourceLocatorCompletenessChecker,
    TableHeaderPreparation,
    UnicodeNewlineNormalizer,
    WhitespaceNormalizer,
)
from robust_rag.cleaning.schemas import CleaningIssue, OperatorExecution
from robust_rag.parsing.canonicalizer import Canonicalizer
from robust_rag.parsing.schemas import BlockType, CanonicalDocument


class CleaningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: str = "stage3-cleaning-v1"
    boilerplate_min_occurrences: int = Field(default=3, ge=2)
    boilerplate_min_page_ratio: float = Field(default=0.6, ge=0, le=1)
    near_duplicate_threshold: float = Field(default=0.92, gt=0, le=1)
    near_duplicate_min_chars: int = Field(default=80, ge=1)


@dataclass(frozen=True, slots=True)
class CleaningResult:
    document: CanonicalDocument
    operator_executions: list[OperatorExecution]
    issues: list[CleaningIssue]

    @property
    def changed_block_ids(self) -> set[str]:
        return {
            block_id
            for execution in self.operator_executions
            for block_id in execution.changed_block_ids
        }

    @property
    def removed_block_ids(self) -> set[str]:
        return {
            block_id
            for execution in self.operator_executions
            for block_id in execution.removed_block_ids
        }


class CleaningPipeline:
    name = "structure-aware-cleaning-pipeline"
    version = "1.0.0"

    def __init__(
        self,
        config: CleaningConfig | None = None,
        operators: list[CleaningOperator] | None = None,
    ) -> None:
        self.config = config or CleaningConfig()
        self.operators = operators or self._default_operators(self.config)

    @property
    def config_snapshot(self) -> dict[str, Any]:
        return {
            **self.config.model_dump(mode="json"),
            "operators": [
                {"name": operator.name, "version": operator.version, **operator.config_snapshot}
                for operator in self.operators
            ],
        }

    def clean(self, source: CanonicalDocument) -> CleaningResult:
        document = source.model_copy(deep=True)
        self._reset_from_original(document)
        executions: list[OperatorExecution] = []
        issues: list[CleaningIssue] = []

        for operator in self.operators:
            before = {block.id: block.model_dump(mode="json") for block in document.blocks}
            operator_issues = operator.apply(document)
            after = {block.id: block.model_dump(mode="json") for block in document.blocks}
            removed_ids = sorted(set(before) - set(after))
            changed_ids = sorted(
                block_id
                for block_id in set(before) & set(after)
                if before[block_id] != after[block_id]
            )
            executions.append(
                OperatorExecution(
                    name=operator.name,
                    version=operator.version,
                    config=operator.config_snapshot,
                    changed_block_ids=changed_ids,
                    removed_block_ids=removed_ids,
                    issue_count=len(operator_issues),
                )
            )
            issues.extend(operator_issues)

        self._repair_structure(document)
        document.metadata["cleaning"] = {
            "pipeline_name": self.name,
            "pipeline_version": self.version,
            "config_version": self.config.config_version,
            "operators": [
                {"name": operator.name, "version": operator.version} for operator in self.operators
            ],
        }
        return CleaningResult(document=document, operator_executions=executions, issues=issues)

    @staticmethod
    def _reset_from_original(document: CanonicalDocument) -> None:
        document.metadata.pop("cleaning", None)
        for block in document.blocks:
            block.normalized_text = block.original_text
            block.attributes.pop("cleaning", None)

    @staticmethod
    def _repair_structure(document: CanonicalDocument) -> None:
        known_ids = {block.id for block in document.blocks}
        by_parent: dict[str | None, list[Any]] = {}
        for order, block in enumerate(document.blocks):
            if block.parent_id not in known_ids and block.block_type is not BlockType.DOCUMENT:
                block.parent_id = document.root_node_id
            block.semantic_order = order
            block.token_count = Canonicalizer.estimate_tokens(block.normalized_text)
            by_parent.setdefault(block.parent_id, []).append(block)
        for siblings in by_parent.values():
            for index, block in enumerate(siblings):
                block.previous_id = siblings[index - 1].id if index > 0 else None
                block.next_id = siblings[index + 1].id if index + 1 < len(siblings) else None

        heading_stack: list[tuple[int, str]] = []
        for block in document.blocks:
            if block.block_type is BlockType.HEADING:
                level = int(block.attributes.get("level", 1))
                heading_stack = [item for item in heading_stack if item[0] < level]
                block.heading_path = [text for _, text in heading_stack]
                if block.normalized_text:
                    heading_stack.append((level, block.normalized_text))
            elif block.block_type is not BlockType.DOCUMENT:
                block.heading_path = [text for _, text in heading_stack]

    @staticmethod
    def _default_operators(config: CleaningConfig) -> list[CleaningOperator]:
        return [
            UnicodeNewlineNormalizer(),
            ControlCharacterCleaner(),
            WhitespaceNormalizer(),
            ReadingOrderCorrector(),
            RepeatedBoilerplateRemover(
                min_occurrences=config.boilerplate_min_occurrences,
                min_page_ratio=config.boilerplate_min_page_ratio,
            ),
            EmptyBlockRemover(),
            ExactDuplicateMarker(),
            NearDuplicateMarker(
                threshold=config.near_duplicate_threshold,
                min_chars=config.near_duplicate_min_chars,
            ),
            LanguageDetector(),
            TableHeaderPreparation(),
            SourceLocatorCompletenessChecker(),
        ]
