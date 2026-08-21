"""Stage 8 grounded chat, conversation history, and UI Message Stream APIs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from robust_rag.core.errors import AppError
from robust_rag.db.enums import ConversationStatus
from robust_rag.db.models import Conversation, Message, ModelInvocation, RetrievalTrace
from robust_rag.db.session import get_db
from robust_rag.generation.schemas import (
    ChatRequest,
    ConversationCreate,
    ConversationDetail,
    ConversationRead,
)
from robust_rag.generation.service import ChatError, ChatService, get_chat_service
from robust_rag.retrieval.query import QueryError

router = APIRouter(tags=["chat"])
DatabaseSession = Annotated[Session, Depends(get_db)]
ChatDependency = Annotated[ChatService, Depends(get_chat_service)]


@router.post("/chat")
def chat(request: ChatRequest, service: ChatDependency) -> StreamingResponse:
    try:
        prepared = service.prepare(request)
    except QueryError as exc:
        raise AppError(code=exc.code, message=exc.message, status_code=422) from exc
    except ChatError as exc:
        raise AppError(
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            details={"retryable": exc.retryable},
        ) from exc
    return StreamingResponse(
        service.stream(prepared),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-vercel-ai-ui-message-stream": "v1",
        },
    )


@router.post("/conversations", response_model=ConversationRead, status_code=201)
def create_conversation(request: ConversationCreate, db: DatabaseSession) -> Conversation:
    conversation = Conversation(title=request.title, status=ConversationStatus.ACTIVE)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/conversations", response_model=list[ConversationRead])
def list_conversations(
    db: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[Conversation]:
    return list(
        db.scalars(
            select(Conversation)
            .where(Conversation.status == ConversationStatus.ACTIVE)
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: uuid.UUID, db: DatabaseSession) -> Conversation:
    conversation = db.scalar(
        select(Conversation)
        .options(selectinload(Conversation.messages).selectinload(Message.citations))
        .where(Conversation.id == conversation_id)
    )
    if conversation is None or conversation.status is ConversationStatus.DELETED:
        raise AppError(
            code="CONVERSATION_NOT_FOUND",
            message="Conversation was not found",
            status_code=404,
        )
    return conversation


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: uuid.UUID, db: DatabaseSession) -> None:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.status is ConversationStatus.DELETED:
        raise AppError(
            code="CONVERSATION_NOT_FOUND",
            message="Conversation was not found",
            status_code=404,
        )
    conversation.status = ConversationStatus.DELETED
    conversation.deleted_at = datetime.now(UTC)
    db.commit()


@router.get("/messages/{message_id}/trace")
def get_message_trace(message_id: uuid.UUID, db: DatabaseSession) -> dict[str, object]:
    message = db.scalar(
        select(Message).options(selectinload(Message.citations)).where(Message.id == message_id)
    )
    if message is None:
        raise AppError(code="MESSAGE_NOT_FOUND", message="Message was not found", status_code=404)
    retrieval = (
        db.get(RetrievalTrace, message.retrieval_trace_id) if message.retrieval_trace_id else None
    )
    invocation = (
        db.get(ModelInvocation, message.model_invocation_id)
        if message.model_invocation_id
        else None
    )
    return {
        "message_id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "status": message.status.value,
        "query_original": message.query_original,
        "query_rewritten": message.query_rewritten,
        "agent": _agent_debug(message.metadata_json),
        "retrieval": _retrieval_debug(retrieval),
        "generation": _invocation_debug(invocation),
        "citations": [
            {
                "source_label": citation.source_label,
                "node_id": str(citation.node_id),
                "document_name": citation.document_name,
                "heading_path": citation.heading_path,
                "source_locators": citation.source_locators_json,
                "excerpt": citation.excerpt,
            }
            for citation in message.citations
        ],
        "error": message.error,
    }


def _retrieval_debug(trace: RetrievalTrace | None) -> dict[str, object] | None:
    if trace is None:
        return None
    return {
        "trace_id": str(trace.id),
        "status": trace.status.value,
        "mode": trace.mode.value,
        "rewrite_snapshot": trace.rewrite_snapshot,
        "context_node_ids": [value.get("node_id") for value in trace.context_nodes_json],
        "context_budget_tokens": trace.context_budget_tokens,
        "context_used_tokens": trace.context_used_tokens,
        "usage": trace.usage_json,
        "latency_ms": trace.latency_json,
        "rerank_fallback_reason": trace.rerank_fallback_reason,
        "error": trace.error,
    }


def _agent_debug(metadata: dict[str, object]) -> dict[str, object] | None:
    if metadata.get("agentic") is not True:
        return None
    keys = {
        "graph_version",
        "action",
        "selected_tool",
        "tool_call_count",
        "agent_invocation_ids",
        "warnings",
        "rewrite_warning",
    }
    return {key: value for key, value in metadata.items() if key in keys}


def _invocation_debug(invocation: ModelInvocation | None) -> dict[str, object] | None:
    if invocation is None:
        return None
    return {
        "invocation_id": str(invocation.id),
        "purpose": invocation.purpose,
        "provider": invocation.provider,
        "model": invocation.model,
        "prompt_version": invocation.prompt_version,
        "status": invocation.status.value,
        "input_tokens": invocation.input_tokens,
        "output_tokens": invocation.output_tokens,
        "latency_ms": invocation.latency_ms,
        "estimated_cost_usd": invocation.estimated_cost_usd,
        "retry_count": invocation.retry_count,
        "trace_id": invocation.trace_id,
        "request_snapshot": invocation.request_snapshot,
        "response_snapshot": invocation.response_snapshot,
        "error": invocation.error,
    }
