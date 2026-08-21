"""Structure-aware, independently versioned cleaning operators."""

import re
import unicodedata
from abc import ABC, abstractmethod
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any

from robust_rag.cleaning.schemas import CleaningIssue, CleaningIssueSeverity
from robust_rag.parsing.schemas import BlockType, CanonicalBlock, CanonicalDocument, SourceType
from robust_rag.parsing.tables import ensure_table_model

CONTAINER_TYPES = {
    BlockType.DOCUMENT,
    BlockType.SECTION,
    BlockType.PAGE,
    BlockType.SLIDE,
    BlockType.SHEET,
    BlockType.LOGICAL_TABLE,
}
TABLE_TYPES = {BlockType.TABLE, BlockType.TABLE_ROW, BlockType.LOGICAL_TABLE}


class CleaningOperator(ABC):
    """Mutate only the working copy owned by a CleaningPipeline."""

    name: str
    version: str

    @property
    def config_snapshot(self) -> dict[str, Any]:
        return {}

    @abstractmethod
    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        """Apply the operator and return its auditable findings."""

    def issue(
        self,
        *,
        code: str,
        message: str,
        block_ids: list[str],
        severity: CleaningIssueSeverity = CleaningIssueSeverity.INFO,
        details: dict[str, Any] | None = None,
    ) -> CleaningIssue:
        return CleaningIssue(
            code=code,
            severity=severity,
            operator_name=self.name,
            operator_version=self.version,
            message=message,
            block_ids=block_ids,
            details=details or {},
        )


class UnicodeNewlineNormalizer(CleaningOperator):
    name = "unicode-newline-normalizer"
    version = "1.0.0"

    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        issues: list[CleaningIssue] = []
        for block in document.blocks:
            before = block.normalized_text
            after = unicodedata.normalize("NFKC", before)
            after = after.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
            if after != before:
                block.normalized_text = after
                issues.append(
                    self.issue(
                        code="TEXT_UNICODE_OR_NEWLINE_NORMALIZED",
                        message="Unicode characters or newline sequences were normalized",
                        block_ids=[block.id],
                    )
                )
        return issues


class ControlCharacterCleaner(CleaningOperator):
    name = "control-character-cleaner"
    version = "1.0.0"
    _invalid = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        issues: list[CleaningIssue] = []
        for block in document.blocks:
            matches = self._invalid.findall(block.normalized_text)
            if not matches:
                continue
            block.normalized_text = self._invalid.sub("", block.normalized_text)
            issues.append(
                self.issue(
                    code="ABNORMAL_CONTROL_CHARACTERS_REMOVED",
                    message="Abnormal control characters were removed from normalized text",
                    block_ids=[block.id],
                    severity=CleaningIssueSeverity.WARNING,
                    details={"removed_count": len(matches)},
                )
            )
        return issues


class WhitespaceNormalizer(CleaningOperator):
    name = "whitespace-normalizer"
    version = "1.0.0"

    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        issues: list[CleaningIssue] = []
        for block in document.blocks:
            if block.block_type is BlockType.CODE:
                after = "\n".join(
                    line.rstrip() for line in block.normalized_text.splitlines()
                ).strip("\n")
            elif block.block_type in TABLE_TYPES:
                after = "\n".join(
                    "\t".join(re.sub(r"[ ]+", " ", cell).strip() for cell in line.split("\t"))
                    for line in block.normalized_text.splitlines()
                    if line.strip()
                )
            else:
                lines = [
                    re.sub(r"[\t ]+", " ", line).strip()
                    for line in block.normalized_text.splitlines()
                ]
                after = "\n".join(line for line in lines if line)
            if after != block.normalized_text:
                block.normalized_text = after
                issues.append(
                    self.issue(
                        code="WHITESPACE_NORMALIZED",
                        message="Whitespace was normalized without changing original text",
                        block_ids=[block.id],
                    )
                )
        return issues


class ReadingOrderCorrector(CleaningOperator):
    name = "reading-order-corrector"
    version = "1.0.0"

    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        positions_by_parent: dict[str | None, list[int]] = defaultdict(list)
        for position, block in enumerate(document.blocks):
            positions_by_parent[block.parent_id].append(position)

        issues: list[CleaningIssue] = []
        for positions in positions_by_parent.values():
            if len(positions) < 2:
                continue
            siblings = [document.blocks[position] for position in positions]
            keys = [self._physical_key(block) for block in siblings]
            if any(key is None for key in keys):
                continue
            ordered = [
                block
                for _, block in sorted(
                    zip(keys, siblings, strict=True),
                    key=lambda item: item[0] or (0, 0.0, 0.0),
                )
            ]
            if [block.id for block in ordered] == [block.id for block in siblings]:
                continue
            for position, block in zip(positions, ordered, strict=True):
                document.blocks[position] = block
            issues.append(
                self.issue(
                    code="READING_ORDER_CORRECTED",
                    message="Sibling blocks were reordered using physical source coordinates",
                    block_ids=[block.id for block in ordered],
                    severity=CleaningIssueSeverity.WARNING,
                )
            )
        return issues

    @staticmethod
    def _physical_key(block: CanonicalBlock) -> tuple[int, float, float] | None:
        for locator in block.source_locators:
            number = locator.page_number or locator.slide_number
            if number is not None and locator.bbox is not None:
                return number, locator.bbox[1], locator.bbox[0]
        return None


class RepeatedBoilerplateRemover(CleaningOperator):
    name = "repeated-boilerplate-remover"
    version = "1.0.0"

    def __init__(self, *, min_occurrences: int, min_page_ratio: float) -> None:
        self.min_occurrences = min_occurrences
        self.min_page_ratio = min_page_ratio

    @property
    def config_snapshot(self) -> dict[str, Any]:
        return {
            "min_occurrences": self.min_occurrences,
            "min_page_ratio": self.min_page_ratio,
        }

    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        page_blocks: dict[int, list[CanonicalBlock]] = defaultdict(list)
        for block in document.blocks:
            if block.block_type in CONTAINER_TYPES or not block.normalized_text:
                continue
            page_number = next(
                (locator.page_number for locator in block.source_locators if locator.page_number),
                None,
            )
            if page_number is not None:
                page_blocks[page_number].append(block)
        page_count = len(page_blocks)
        if page_count < self.min_occurrences:
            return []

        pages_by_value: dict[str, set[int]] = defaultdict(set)
        candidates_by_value: dict[str, dict[str, CanonicalBlock]] = defaultdict(dict)
        for page_number, blocks in page_blocks.items():
            edge_by_id = {block.id: block for block in [*blocks[:2], *blocks[-2:]]}
            for block in edge_by_id.values():
                key = self._key(block.normalized_text)
                pages_by_value[key].add(page_number)
                candidates_by_value[key][block.id] = block
        repeated = {
            key
            for key, pages in pages_by_value.items()
            if key
            and len(pages) >= self.min_occurrences
            and len(pages) / page_count >= self.min_page_ratio
            and len(key) <= 200
        }
        removed = [block for key in repeated for block in candidates_by_value[key].values()]
        removed_ids = {block.id for block in removed}
        if not removed_ids:
            return []
        document.blocks = [block for block in document.blocks if block.id not in removed_ids]
        return [
            self.issue(
                code="REPEATED_HEADER_FOOTER_REMOVED",
                message="Repeated page-edge boilerplate was removed from the cleaned projection",
                block_ids=sorted(removed_ids),
                details={"page_count": page_count, "normalized_values": sorted(repeated)},
            )
        ]

    @staticmethod
    def _key(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()


class EmptyBlockRemover(CleaningOperator):
    name = "empty-block-remover"
    version = "1.0.0"

    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        parent_ids = {block.parent_id for block in document.blocks if block.parent_id is not None}
        removable = {
            block.id
            for block in document.blocks
            if block.block_type not in CONTAINER_TYPES
            and block.id not in parent_ids
            and not block.normalized_text.strip()
        }
        if not removable:
            return []
        document.blocks = [block for block in document.blocks if block.id not in removable]
        return [
            self.issue(
                code="EMPTY_BLOCK_REMOVED",
                message="Empty leaf blocks were removed from the cleaned projection",
                block_ids=sorted(removable),
            )
        ]


class ExactDuplicateMarker(CleaningOperator):
    name = "exact-duplicate-marker"
    version = "1.0.0"

    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        by_text: dict[str, list[CanonicalBlock]] = defaultdict(list)
        for block in document.blocks:
            if block.block_type not in CONTAINER_TYPES and block.normalized_text:
                by_text[self._key(block.normalized_text)].append(block)
        issues: list[CleaningIssue] = []
        for blocks in by_text.values():
            if len(blocks) < 2:
                continue
            original_id = blocks[0].id
            duplicate_ids: list[str] = []
            for duplicate in blocks[1:]:
                _mark(duplicate, "exact_duplicate", duplicate_of=original_id)
                duplicate_ids.append(duplicate.id)
            issues.append(
                self.issue(
                    code="EXACT_DUPLICATE_BLOCKS_FOUND",
                    message="Exact duplicate blocks were marked but retained",
                    block_ids=[original_id, *duplicate_ids],
                    severity=CleaningIssueSeverity.WARNING,
                    details={"duplicate_of": original_id},
                )
            )
        return issues

    @staticmethod
    def _key(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()


class NearDuplicateMarker(CleaningOperator):
    name = "near-duplicate-marker"
    version = "1.0.0"

    def __init__(self, *, threshold: float, min_chars: int) -> None:
        self.threshold = threshold
        self.min_chars = min_chars

    @property
    def config_snapshot(self) -> dict[str, Any]:
        return {"threshold": self.threshold, "min_chars": self.min_chars}

    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        candidates = [
            block
            for block in document.blocks
            if block.block_type not in CONTAINER_TYPES
            and len(self._key(block.normalized_text)) >= self.min_chars
            and not _has_mark(block, "exact_duplicate")
        ]
        issues: list[CleaningIssue] = []
        matched: set[tuple[str, str]] = set()
        for index, left in enumerate(candidates):
            left_value = self._key(left.normalized_text)
            for right in candidates[index + 1 :]:
                right_value = self._key(right.normalized_text)
                if left_value == right_value:
                    continue
                length_ratio = min(len(left_value), len(right_value)) / max(
                    len(left_value), len(right_value)
                )
                if length_ratio < self.threshold:
                    continue
                score = SequenceMatcher(None, left_value, right_value, autojunk=False).ratio()
                if score < self.threshold:
                    continue
                pair = (left.id, right.id)
                if pair in matched:
                    continue
                matched.add(pair)
                _mark(right, "near_duplicate", similar_to=left.id, similarity=round(score, 4))
                issues.append(
                    self.issue(
                        code="NEAR_DUPLICATE_BLOCKS_FOUND",
                        message="Near-duplicate blocks were marked but retained",
                        block_ids=[left.id, right.id],
                        severity=CleaningIssueSeverity.WARNING,
                        details={"similarity": round(score, 4)},
                    )
                )
        return issues

    @staticmethod
    def _key(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()


class LanguageDetector(CleaningOperator):
    name = "language-detector"
    version = "1.0.0"

    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        issues: list[CleaningIssue] = []
        for block in document.blocks:
            detected = _detect_language(block.normalized_text)
            if detected is not None and detected != block.language:
                previous = block.language
                block.language = detected
                issues.append(
                    self.issue(
                        code="BLOCK_LANGUAGE_DETECTED",
                        message="Block language metadata was updated from normalized text",
                        block_ids=[block.id],
                        details={"previous": previous, "detected": detected},
                    )
                )
        document_language = _detect_language(
            "\n".join(block.normalized_text for block in document.blocks)
        )
        if document_language is not None:
            document.language = document_language
        return issues


class TableHeaderPreparation(CleaningOperator):
    name = "table-header-preparation"
    version = "2.0.0"

    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        issues: list[CleaningIssue] = []
        for block in document.blocks:
            if block.block_type not in TABLE_TYPES:
                continue
            before_profile = block.attributes.get("table_profile")
            model = ensure_table_model(block.attributes, block.normalized_text)
            profile = model.get("profile", {})
            block.attributes["table_model"] = model
            block.attributes["table_profile"] = profile
            cleaning = _cleaning_metadata(block)
            grid = model.get("grid", [])
            kind = str(profile.get("kind", "complex")) if isinstance(profile, dict) else "complex"
            header = (
                [str(value).strip() for value in grid[0]]
                if kind in {"record_table", "matrix"}
                and isinstance(grid, list)
                and grid
                and isinstance(grid[0], list)
                else []
            )
            if header:
                cleaning["table_header"] = header
                cleaning["header_source"] = "shape_analyzer"
            else:
                cleaning.pop("table_header", None)
                cleaning["header_source"] = "not_applicable"
            cleaning["table_kind"] = kind
            cleaning["profile_confidence"] = profile.get("confidence", 0)
            cleaning["data_row_count"] = max(0, len(grid) - len(profile.get("header_rows", [])))
            if before_profile == profile and cleaning.get("table_header") == header:
                continue
            issues.append(
                self.issue(
                    code=(
                        "TABLE_HEADER_CANDIDATE_PREPARED"
                        if header
                        else "TABLE_SHAPE_PROFILE_PREPARED"
                    ),
                    message=(
                        "Table shape and applicable header semantics were prepared for chunking"
                    ),
                    block_ids=[block.id],
                    details={
                        "kind": kind,
                        "confidence": profile.get("confidence", 0),
                        "column_count": profile.get("column_count", len(header)),
                    },
                )
            )
        return issues


class SourceLocatorCompletenessChecker(CleaningOperator):
    name = "source-locator-completeness-checker"
    version = "1.0.0"

    def apply(self, document: CanonicalDocument) -> list[CleaningIssue]:
        issues: list[CleaningIssue] = []
        for block in document.blocks:
            if block.block_type is BlockType.DOCUMENT:
                continue
            if not block.source_locators:
                issues.append(self._missing(block, "Block has no source locator"))
                continue
            incomplete = [
                locator for locator in block.source_locators if not self._specific(locator)
            ]
            if len(incomplete) == len(block.source_locators):
                issues.append(
                    self._missing(block, "Block source locator lacks a physical position")
                )
        return issues

    def _missing(self, block: CanonicalBlock, message: str) -> CleaningIssue:
        _mark(block, "source_locator_incomplete")
        return self.issue(
            code="SOURCE_LOCATOR_INCOMPLETE",
            message=message,
            block_ids=[block.id],
            severity=CleaningIssueSeverity.WARNING,
        )

    @staticmethod
    def _specific(locator: Any) -> bool:
        if locator.source_type is SourceType.PDF:
            return locator.page_number is not None
        if locator.source_type is SourceType.POWERPOINT:
            return locator.slide_number is not None
        if locator.source_type is SourceType.EXCEL:
            return locator.sheet_name is not None
        if locator.source_type is SourceType.HTML:
            return locator.dom_path is not None
        if locator.source_type in {SourceType.MARKDOWN, SourceType.TEXT}:
            return locator.line_start is not None
        if locator.source_type is SourceType.WORD:
            return locator.paragraph_index is not None or locator.table_index is not None
        return False


def _detect_language(value: str) -> str | None:
    chinese = len(re.findall(r"[\u3400-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if chinese == latin == 0:
        return None
    if chinese and latin:
        return "zh-en-mixed"
    return "zh" if chinese else "en"


def _cleaning_metadata(block: CanonicalBlock) -> dict[str, Any]:
    value = block.attributes.setdefault("cleaning", {})
    if not isinstance(value, dict):
        value = {}
        block.attributes["cleaning"] = value
    return value


def _mark(block: CanonicalBlock, flag: str, **details: Any) -> None:
    cleaning = _cleaning_metadata(block)
    flags = cleaning.setdefault("flags", [])
    if not isinstance(flags, list):
        flags = []
        cleaning["flags"] = flags
    if flag not in flags:
        flags.append(flag)
    if details:
        findings = cleaning.setdefault("findings", {})
        if not isinstance(findings, dict):
            findings = {}
            cleaning["findings"] = findings
        findings[flag] = details


def _has_mark(block: CanonicalBlock, flag: str) -> bool:
    cleaning = block.attributes.get("cleaning", {})
    return isinstance(cleaning, dict) and flag in cleaning.get("flags", [])
