"""Deterministic structure-aware parent/child chunking."""

import hashlib
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from robust_rag.chunking.schemas import RetrievalNodeData
from robust_rag.db.enums import RetrievalNodeLevel
from robust_rag.parsing.canonicalizer import Canonicalizer
from robust_rag.parsing.schemas import BlockType, CanonicalBlock, CanonicalDocument, SourceLocator
from robust_rag.quality.schemas import QualityDecision

NODE_ID_NAMESPACE = uuid.UUID("e68aa9f6-e141-4dde-97eb-6efb3001dcbe")
TOKEN_PATTERN = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\w\s]", re.UNICODE)
IGNORED_CONTAINER_TYPES = {
    BlockType.DOCUMENT,
    BlockType.SECTION,
    BlockType.PAGE,
    BlockType.SLIDE,
    BlockType.SHEET,
}
TABLE_TYPES = {BlockType.TABLE, BlockType.LOGICAL_TABLE}


class ChunkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: str = "stage5-parent-child-v1"
    parent_target_tokens: int = Field(default=1800, ge=1)
    parent_max_tokens: int = Field(default=2500, ge=1)
    child_target_tokens: int = Field(default=500, ge=1)
    child_max_tokens: int = Field(default=600, ge=1)
    child_overlap_tokens: int = Field(default=64, ge=0)

    @model_validator(mode="after")
    def validate_token_windows(self) -> "ChunkingConfig":
        if self.parent_target_tokens > self.parent_max_tokens:
            raise ValueError("parent_target_tokens cannot exceed parent_max_tokens")
        if self.child_target_tokens > self.child_max_tokens:
            raise ValueError("child_target_tokens cannot exceed child_max_tokens")
        if self.child_overlap_tokens >= self.child_target_tokens:
            raise ValueError("child_overlap_tokens must be smaller than child_target_tokens")
        return self


@dataclass(frozen=True, slots=True)
class ChunkingResult:
    nodes: list[RetrievalNodeData]

    @property
    def parents(self) -> list[RetrievalNodeData]:
        return [node for node in self.nodes if node.node_level is RetrievalNodeLevel.PARENT]

    @property
    def children(self) -> list[RetrievalNodeData]:
        return [node for node in self.nodes if node.node_level is RetrievalNodeLevel.CHILD]


@dataclass(slots=True)
class _Fragment:
    text: str
    source_block_ids: list[str]
    source_locators: list[SourceLocator]
    content_types: list[str]
    heading_path: list[str]
    language: str | None
    table_header: list[str] = field(default_factory=list)

    @property
    def token_count(self) -> int:
        return Canonicalizer.estimate_tokens(self.text)

    @property
    def is_table(self) -> bool:
        return bool(self.table_header) or any(value in TABLE_TYPES for value in self.content_types)


@dataclass(slots=True)
class _StructuralGroup:
    kind: str
    blocks: list[CanonicalBlock] = field(default_factory=list)


@dataclass(slots=True)
class _ParentDraft:
    content: str
    source_block_ids: list[str]
    source_locators: list[SourceLocator]
    content_types: list[str]
    heading_path: list[str]
    language: str | None
    group_kind: str
    table_header: list[str] = field(default_factory=list)


class StructureAwareChunker:
    name = "structure-aware-parent-child-chunker"
    version = "1.0.0"

    def __init__(self, config: ChunkingConfig | None = None) -> None:
        self.config = config or ChunkingConfig()

    @property
    def config_snapshot(self) -> dict[str, Any]:
        return self.config.model_dump(mode="json")

    def chunk(
        self,
        document: CanonicalDocument,
        *,
        canonical_document_id: uuid.UUID,
        quality_status: QualityDecision,
        quality_summary: dict[str, Any],
    ) -> ChunkingResult:
        document_id = uuid.UUID(document.document_id)
        version_id = uuid.UUID(document.document_version_id)
        parent_drafts: list[_ParentDraft] = []
        for group in self._structural_groups(document):
            fragments = [
                fragment
                for block in group.blocks
                for fragment in self._block_fragments(block)
                if fragment.text.strip()
            ]
            parent_drafts.extend(self._pack_parent_fragments(group.kind, fragments))

        parents = [
            self._build_node(
                draft=draft,
                ordinal=ordinal,
                document=document,
                document_id=document_id,
                version_id=version_id,
                canonical_document_id=canonical_document_id,
                quality_status=quality_status,
                quality_summary=quality_summary,
            )
            for ordinal, draft in enumerate(parent_drafts)
        ]
        parents = self._link_siblings(parents)

        children: list[RetrievalNodeData] = []
        for parent in parents:
            contents = self._table_child_contents(parent) if parent.attributes.get("table") else []
            if not contents:
                contents = self._text_child_contents(parent.content)
            parent_children = [
                self._build_child(
                    parent=parent,
                    content=content,
                    ordinal=ordinal,
                    document=document,
                )
                for ordinal, content in enumerate(contents)
                if content.strip()
            ]
            children.extend(self._link_siblings(parent_children))
        return ChunkingResult(nodes=[*parents, *children])

    def _structural_groups(self, document: CanonicalDocument) -> list[_StructuralGroup]:
        blocks_by_id = {block.id: block for block in document.blocks}
        groups: OrderedDict[str, _StructuralGroup] = OrderedDict()
        for block in sorted(document.blocks, key=lambda value: value.semantic_order):
            if block.block_type in IGNORED_CONTAINER_TYPES or not block.normalized_text.strip():
                continue
            table_ancestor = _ancestor_of_type(block, blocks_by_id, TABLE_TYPES)
            if (
                block.block_type is BlockType.TABLE_ROW
                and table_ancestor is not None
                and table_ancestor.normalized_text.strip()
            ):
                continue
            slide = _ancestor_of_type(block, blocks_by_id, {BlockType.SLIDE})
            page = _ancestor_of_type(block, blocks_by_id, {BlockType.PAGE})
            if block.block_type is BlockType.LOGICAL_TABLE:
                key = f"logical-table:{block.id}"
                kind = "logical_table"
            elif table_ancestor is not None and slide is None:
                key = f"table:{table_ancestor.id}"
                kind = "table"
            elif slide is not None:
                key = f"slide:{slide.id}"
                kind = "slide"
            else:
                heading_path = _effective_heading_path(block)
                if heading_path:
                    key = "heading:" + "\x1f".join(heading_path)
                    kind = "heading_section"
                elif page is not None:
                    key = f"page:{page.id}"
                    kind = "page"
                else:
                    key = "document-root"
                    kind = "document"
            group = groups.setdefault(key, _StructuralGroup(kind=kind))
            if block not in group.blocks:
                group.blocks.append(block)
        return list(groups.values())

    def _block_fragments(self, block: CanonicalBlock) -> list[_Fragment]:
        if block.block_type in TABLE_TYPES:
            return self._table_fragments(block)
        pieces = _split_text(block.normalized_text, self.config.parent_max_tokens)
        return [self._fragment(block, piece) for piece in pieces]

    def _table_fragments(self, block: CanonicalBlock) -> list[_Fragment]:
        header = _table_header(block)
        lines = [line.strip() for line in block.normalized_text.splitlines() if line.strip()]
        header_line = "\t".join(header)
        if header and lines and _normalized_row(lines[0]) == _normalized_row(header_line):
            data_lines = lines[1:]
        else:
            data_lines = lines
        if not header:
            header = _row_values(lines[0]) if lines else ["table"]
            header_line = "\t".join(header)
            data_lines = lines[1:] if lines else []

        fragments: list[_Fragment] = []
        current_rows: list[str] = []
        for row in data_lines:
            candidate = "\n".join([header_line, *current_rows, row])
            if (
                current_rows
                and Canonicalizer.estimate_tokens(candidate) > self.config.parent_max_tokens
            ):
                fragments.append(
                    self._fragment(block, "\n".join([header_line, *current_rows]), header)
                )
                current_rows = [row]
            else:
                current_rows.append(row)
        table_text = "\n".join([header_line, *current_rows])
        if current_rows or header_line:
            fragments.append(self._fragment(block, table_text, header))
        return fragments

    @staticmethod
    def _fragment(
        block: CanonicalBlock, text: str, table_header: list[str] | None = None
    ) -> _Fragment:
        return _Fragment(
            text=text,
            source_block_ids=[block.id],
            source_locators=list(block.source_locators),
            content_types=[block.block_type.value],
            heading_path=_effective_heading_path(block),
            language=block.language,
            table_header=table_header or [],
        )

    def _pack_parent_fragments(
        self, group_kind: str, fragments: list[_Fragment]
    ) -> list[_ParentDraft]:
        output: list[_ParentDraft] = []
        current: list[_Fragment] = []
        current_tokens = 0

        def flush() -> None:
            nonlocal current, current_tokens
            if current:
                output.append(_combine_fragments(group_kind, current))
            current = []
            current_tokens = 0

        for fragment in fragments:
            if fragment.is_table:
                flush()
                output.append(_combine_fragments(group_kind, [fragment]))
                continue
            if current and (
                current_tokens >= self.config.parent_target_tokens
                or current_tokens + fragment.token_count > self.config.parent_max_tokens
            ):
                flush()
            current.append(fragment)
            current_tokens += fragment.token_count
        flush()
        return output

    def _build_node(
        self,
        *,
        draft: _ParentDraft,
        ordinal: int,
        document: CanonicalDocument,
        document_id: uuid.UUID,
        version_id: uuid.UUID,
        canonical_document_id: uuid.UUID,
        quality_status: QualityDecision,
        quality_summary: dict[str, Any],
    ) -> RetrievalNodeData:
        content_hash = _sha256(draft.content)
        node_id = uuid.uuid5(
            NODE_ID_NAMESPACE,
            ":".join(
                [
                    str(version_id),
                    self.version,
                    self.config.config_version,
                    "parent",
                    str(ordinal),
                    content_hash,
                ]
            ),
        )
        retrieval_text = _retrieval_text(
            document.title,
            draft.heading_path,
            draft.content_types,
            draft.source_locators,
            draft.content,
        )
        return RetrievalNodeData(
            node_id=node_id,
            document_id=document_id,
            document_version_id=version_id,
            canonical_document_id=canonical_document_id,
            node_level=RetrievalNodeLevel.PARENT,
            title=document.title,
            heading_path=draft.heading_path,
            content=draft.content,
            retrieval_text=retrieval_text,
            source_locators=draft.source_locators,
            source_block_ids=draft.source_block_ids,
            content_types=draft.content_types,
            language=draft.language or document.language,
            token_count=Canonicalizer.estimate_tokens(draft.content),
            quality_status=quality_status,
            quality_summary=quality_summary,
            chunker_name=self.name,
            chunker_version=self.version,
            chunking_config_version=self.config.config_version,
            retrieval_text_hash=_sha256(retrieval_text),
            attributes={
                "group_kind": draft.group_kind,
                "table": bool(draft.table_header),
                "table_header": draft.table_header,
            },
        )

    def _build_child(
        self,
        *,
        parent: RetrievalNodeData,
        content: str,
        ordinal: int,
        document: CanonicalDocument,
    ) -> RetrievalNodeData:
        content_hash = _sha256(content)
        node_id = uuid.uuid5(
            NODE_ID_NAMESPACE,
            f"{parent.node_id}:{self.config.config_version}:child:{ordinal}:{content_hash}",
        )
        retrieval_text = _retrieval_text(
            document.title,
            parent.heading_path,
            parent.content_types,
            parent.source_locators,
            content,
        )
        return RetrievalNodeData(
            node_id=node_id,
            document_id=parent.document_id,
            document_version_id=parent.document_version_id,
            canonical_document_id=parent.canonical_document_id,
            node_level=RetrievalNodeLevel.CHILD,
            parent_node_id=parent.node_id,
            title=parent.title,
            heading_path=parent.heading_path,
            content=content,
            retrieval_text=retrieval_text,
            source_locators=parent.source_locators,
            source_block_ids=parent.source_block_ids,
            content_types=parent.content_types,
            language=parent.language,
            token_count=Canonicalizer.estimate_tokens(content),
            quality_status=parent.quality_status,
            quality_summary=parent.quality_summary,
            chunker_name=self.name,
            chunker_version=self.version,
            chunking_config_version=self.config.config_version,
            retrieval_text_hash=_sha256(retrieval_text),
            attributes={
                "child_ordinal": ordinal,
                "table": parent.attributes.get("table", False),
                "table_header": parent.attributes.get("table_header", []),
            },
        )

    def _text_child_contents(self, content: str) -> list[str]:
        return _split_text(
            content,
            self.config.child_max_tokens,
            overlap_tokens=self.config.child_overlap_tokens,
            target_tokens=self.config.child_target_tokens,
        )

    def _table_child_contents(self, parent: RetrievalNodeData) -> list[str]:
        header = [str(value) for value in parent.attributes.get("table_header", [])]
        header_line = "\t".join(header)
        lines = [line.strip() for line in parent.content.splitlines() if line.strip()]
        if lines and _normalized_row(lines[0]) == _normalized_row(header_line):
            rows = lines[1:]
        else:
            rows = lines
        if not rows:
            return [header_line] if header_line else [parent.content]
        output: list[str] = []
        current: list[str] = []
        for row in rows:
            candidate = "\n".join([header_line, *current, row])
            if (
                current
                and Canonicalizer.estimate_tokens(candidate) > self.config.child_target_tokens
            ):
                output.append("\n".join([header_line, *current]))
                current = [row]
            else:
                current.append(row)
        if current:
            output.append("\n".join([header_line, *current]))
        return output

    @staticmethod
    def _link_siblings(nodes: list[RetrievalNodeData]) -> list[RetrievalNodeData]:
        return [
            node.model_copy(
                update={
                    "previous_node_id": nodes[index - 1].node_id if index > 0 else None,
                    "next_node_id": nodes[index + 1].node_id if index + 1 < len(nodes) else None,
                }
            )
            for index, node in enumerate(nodes)
        ]


def _ancestor_of_type(
    block: CanonicalBlock,
    blocks_by_id: dict[str, CanonicalBlock],
    types: set[BlockType],
) -> CanonicalBlock | None:
    current_id = block.parent_id
    visited: set[str] = set()
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        current = blocks_by_id.get(current_id)
        if current is None:
            return None
        if current.block_type in types:
            return current
        current_id = current.parent_id
    return None


def _effective_heading_path(block: CanonicalBlock) -> list[str]:
    path = [value.strip() for value in block.heading_path if value.strip()]
    text = block.normalized_text.strip()
    if block.block_type is BlockType.HEADING and text and (not path or path[-1] != text):
        path.append(text)
    return path


def _combine_fragments(group_kind: str, fragments: list[_Fragment]) -> _ParentDraft:
    heading_path = max((fragment.heading_path for fragment in fragments), key=len, default=[])
    languages = [fragment.language for fragment in fragments if fragment.language]
    table_header = next(
        (fragment.table_header for fragment in fragments if fragment.table_header), []
    )
    return _ParentDraft(
        content="\n\n".join(fragment.text for fragment in fragments),
        source_block_ids=_unique(
            block_id for fragment in fragments for block_id in fragment.source_block_ids
        ),
        source_locators=_merge_locators(
            locator for fragment in fragments for locator in fragment.source_locators
        ),
        content_types=_unique(value for fragment in fragments for value in fragment.content_types),
        heading_path=list(heading_path),
        language=(
            languages[0]
            if languages and all(value == languages[0] for value in languages)
            else None
        ),
        group_kind=group_kind,
        table_header=list(table_header),
    )


def _table_header(block: CanonicalBlock) -> list[str]:
    cleaning = block.attributes.get("cleaning", {})
    if isinstance(cleaning, dict):
        value = cleaning.get("table_header", [])
        if isinstance(value, list) and value:
            return [str(item) for item in value]
    rows = block.attributes.get("display_values") or block.attributes.get("rows")
    if isinstance(rows, list) and rows and isinstance(rows[0], list):
        return ["" if value is None else str(value) for value in rows[0]]
    lines = [line for line in block.normalized_text.splitlines() if line.strip()]
    return _row_values(lines[0]) if lines else []


def _row_values(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\t|\s*\|\s*", value) if part.strip()]


def _normalized_row(value: str) -> str:
    return "\x1f".join(_row_values(value))


def _split_text(
    text: str,
    max_tokens: int,
    *,
    overlap_tokens: int = 0,
    target_tokens: int | None = None,
) -> list[str]:
    matches = list(TOKEN_PATTERN.finditer(text))
    if not matches:
        return []
    if len(matches) <= max_tokens:
        return [text.strip()]
    output: list[str] = []
    start = 0
    while start < len(matches):
        hard_end = min(len(matches), start + max_tokens)
        preferred_end = min(hard_end, start + (target_tokens or max_tokens))
        end = next(
            (
                index + 1
                for index in range(preferred_end, hard_end)
                if matches[index].group(0)
                in {".", "!", "?", "\u3002", "\uff01", "\uff1f", ";", "\uff1b"}
            ),
            preferred_end,
        )
        value = text[matches[start].start() : matches[end - 1].end()].strip()
        if value:
            output.append(value)
        if end == len(matches):
            break
        start = end - overlap_tokens
    return output


def _retrieval_text(
    title: str | None,
    heading_path: list[str],
    content_types: list[str],
    source_locators: list[SourceLocator],
    content: str,
) -> str:
    context: list[str] = []
    if title:
        context.append(title.strip())
    if heading_path:
        context.append(" > ".join(heading_path))
    if content_types:
        context.append("content_types: " + ", ".join(content_types))
    source_labels = _source_labels(source_locators)
    if source_labels:
        context.append("source: " + "; ".join(source_labels))
    context.append(content.strip())
    return "\n".join(value for value in context if value)


def _source_labels(locators: list[SourceLocator]) -> list[str]:
    output: list[str] = []
    for locator in locators:
        if locator.page_number is not None:
            output.append(f"page {locator.page_number}")
        if locator.slide_number is not None:
            output.append(f"slide {locator.slide_number}")
        if locator.sheet_name:
            label = f"sheet {locator.sheet_name}"
            if locator.cell_range:
                label += f" {locator.cell_range}"
            output.append(label)
        if locator.line_start is not None:
            end = locator.line_end or locator.line_start
            output.append(f"lines {locator.line_start}-{end}")
    return _unique(output)


def _merge_locators(values: Any) -> list[SourceLocator]:
    output: list[SourceLocator] = []
    seen: set[str] = set()
    for locator in values:
        key = locator.model_dump_json()
        if key not in seen:
            output.append(locator)
            seen.add(key)
    return output


def _unique(values: Any) -> list[Any]:
    output: list[Any] = []
    for value in values:
        if value not in output:
            output.append(value)
    return output


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
