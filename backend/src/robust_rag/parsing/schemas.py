"""Versioned parser-neutral and canonical document schemas."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

PARSE_ARTIFACT_SCHEMA_VERSION = "parse-artifact/1.0"
CANONICAL_SCHEMA_VERSION = "canonical-document/1.0"


class SourceType(StrEnum):
    PDF = "pdf"
    WORD = "word"
    POWERPOINT = "powerpoint"
    EXCEL = "excel"
    HTML = "html"
    MARKDOWN = "markdown"
    TEXT = "text"


class BlockType(StrEnum):
    DOCUMENT = "document"
    SECTION = "section"
    PAGE = "page"
    SLIDE = "slide"
    SHEET = "sheet"
    LOGICAL_TABLE = "logical_table"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_ROW = "table_row"
    CODE = "code"
    QUOTE = "quote"
    FORMULA = "formula"
    FOOTNOTE = "footnote"
    CAPTION = "caption"
    NOTE = "note"
    ASSET_REFERENCE = "asset_reference"


class QualityStatus(StrEnum):
    UNASSESSED = "unassessed"
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


class SourceLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    page_number: int | None = Field(default=None, ge=1)
    slide_number: int | None = Field(default=None, ge=1)
    paragraph_index: int | None = Field(default=None, ge=1)
    table_index: int | None = Field(default=None, ge=1)
    shape_index: int | None = Field(default=None, ge=1)
    sheet_name: str | None = None
    cell_range: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    dom_path: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)


class ParsedBlock(BaseModel):
    """One ordered parser-level block before stable IDs are assigned."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    block_type: BlockType
    parent_ref: str | None = None
    original_text: str = ""
    source_locators: list[SourceLocator] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    language: str | None = None
    parser_confidence: float | None = Field(default=None, ge=0, le=1)


class ParseArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = PARSE_ARTIFACT_SCHEMA_VERSION
    parser_name: str
    parser_version: str
    parser_mode: str
    title: str | None = None
    language: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    blocks: list[ParsedBlock]
    assets: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "ParseArtifact":
        refs = [block.ref for block in self.blocks]
        if len(refs) != len(set(refs)):
            raise ValueError("ParseArtifact block refs must be unique")
        known = set(refs)
        for block in self.blocks:
            if block.parent_ref is not None and block.parent_ref not in known:
                raise ValueError(f"Unknown parent_ref: {block.parent_ref}")
            if block.parent_ref == block.ref:
                raise ValueError("A block cannot be its own parent")
        return self


class CanonicalBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    block_type: BlockType
    parent_id: str | None
    previous_id: str | None = None
    next_id: str | None = None
    semantic_order: int = Field(ge=0)
    heading_path: list[str] = Field(default_factory=list)
    original_text: str
    normalized_text: str
    source_locators: list[SourceLocator] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    language: str | None = None
    token_count: int = Field(ge=0)
    parser_confidence: float | None = Field(default=None, ge=0, le=1)
    quality_status: QualityStatus = QualityStatus.UNASSESSED
    quality_flags: list[str] = Field(default_factory=list)


class CanonicalDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CANONICAL_SCHEMA_VERSION
    document_id: str
    document_version_id: str
    title: str | None = None
    language: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    root_node_id: str
    blocks: list[CanonicalBlock]
    assets: list[dict[str, Any]] = Field(default_factory=list)
