"""API and internal schemas for stage 8 chat generation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from robust_rag.db.enums import (
    ConversationStatus,
    MessageRole,
    MessageStatus,
    RetrievalMode,
)


class UIMessagePart(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = None


class UIMessageInput(BaseModel):
    id: str | None = None
    role: Literal["user", "assistant"]
    parts: list[UIMessagePart] = Field(default_factory=list)
    content: str | None = None

    def text_content(self) -> str:
        parts = [part.text for part in self.parts if part.type == "text" and part.text]
        return "".join(parts) or (self.content or "")


class ChatRequest(BaseModel):
    id: str | None = None
    conversation_id: uuid.UUID | None = None
    messages: list[UIMessageInput] = Field(min_length=1, max_length=100)
    mode: RetrievalMode = RetrievalMode.HYBRID_RERANK
    top_k: int | None = Field(default=None, ge=1, le=100)
    context_budget_tokens: int | None = Field(default=None, ge=1, le=100000)
    debug: bool = False

    @model_validator(mode="after")
    def validate_latest_message(self) -> ChatRequest:
        latest = self.messages[-1]
        if latest.role != "user" or not latest.text_content().strip():
            raise ValueError("The latest chat message must contain non-empty user text")
        return self

    def requested_conversation_id(self) -> uuid.UUID | None:
        if self.conversation_id is not None:
            return self.conversation_id
        if self.id:
            try:
                return uuid.UUID(self.id)
            except ValueError:
                return None
        return None


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=500)


class CitationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    citation_index: int
    source_label: str
    node_id: uuid.UUID
    document_id: uuid.UUID | None
    document_version_id: uuid.UUID | None
    document_name: str
    heading_path: list[str]
    source_locators_json: list[dict[str, object]]
    excerpt: str


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: MessageRole
    status: MessageStatus
    content: str
    query_original: str | None
    query_rewritten: str | None
    retrieval_trace_id: uuid.UUID | None
    model_invocation_id: uuid.UUID | None
    metadata_json: dict[str, object]
    error: dict[str, object] | None
    created_at: datetime
    finished_at: datetime | None
    citations: list[CitationRead]


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    status: ConversationStatus
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationRead):
    messages: list[MessageRead]


@dataclass(frozen=True)
class ChatSource:
    label: str
    node_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    document_name: str
    title: str | None
    heading_path: list[str]
    content: str
    content_types: list[str]
    source_locators: list[dict[str, object]]

    def snapshot(self, *, excerpt_max_chars: int) -> dict[str, object]:
        return {
            "label": self.label,
            "node_id": str(self.node_id),
            "document_id": str(self.document_id),
            "document_version_id": str(self.document_version_id),
            "document_name": self.document_name,
            "title": self.title,
            "heading_path": self.heading_path,
            "content_types": self.content_types,
            "source_locators": self.source_locators,
            "location": source_location_text(self.source_locators),
            "excerpt": self.content[:excerpt_max_chars],
        }


def source_location_text(locators: list[dict[str, object]]) -> str:
    values: list[str] = []
    for locator in locators:
        if isinstance(locator.get("page_number"), int):
            values.append(f"page {locator['page_number']}")
        if isinstance(locator.get("slide_number"), int):
            values.append(f"slide {locator['slide_number']}")
        sheet = locator.get("sheet_name")
        if isinstance(sheet, str) and sheet:
            cell_range = locator.get("cell_range")
            values.append(f"sheet {sheet} {cell_range}".strip())
        if isinstance(locator.get("line_start"), int):
            line_end = locator.get("line_end") or locator["line_start"]
            values.append(f"lines {locator['line_start']}-{line_end}")
    return ", ".join(dict.fromkeys(values))
