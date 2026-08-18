"""Grounded chat orchestration with durable messages, citations, and model traces."""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.core.observability import current_trace_id, observe
from robust_rag.core.settings import Settings, get_settings
from robust_rag.db.enums import (
    ConversationStatus,
    MessageRole,
    MessageStatus,
    ModelInvocationStatus,
)
from robust_rag.db.models import (
    Citation,
    Conversation,
    Document,
    Message,
    ModelInvocation,
    RetrievalNode,
)
from robust_rag.db.session import SessionLocal
from robust_rag.generation.prompts import grounded_request, rewrite_request
from robust_rag.generation.provider import (
    LLMProvider,
    LLMProviderError,
    LLMRequest,
    LLMUsage,
    ResponsesAPIProvider,
)
from robust_rag.generation.schemas import ChatRequest, ChatSource
from robust_rag.retrieval.query import QueryError, QueryRewriteResult, normalize_query
from robust_rag.retrieval.schemas import RetrievalSearchRequest, RetrievalSearchResponse
from robust_rag.retrieval.service import RetrievalError, RetrievalService, get_retrieval_service

_logger = structlog.get_logger(__name__)


class ChatError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class PreparedChat:
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    invocation_id: uuid.UUID | None
    question: str
    rewritten_question: str
    retrieval: RetrievalSearchResponse
    sources: list[ChatSource]
    generation_request: LLMRequest | None
    rewrite_warning: str | None


class ChatService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        retrieval_service: RetrievalService,
        provider: LLMProvider,
        settings: Settings,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.session_factory = session_factory
        self.retrieval_service = retrieval_service
        self.provider = provider
        self.settings = settings
        self.sleeper = sleeper
        self.jitter = jitter

    def prepare(self, request: ChatRequest) -> PreparedChat:
        question = normalize_query(
            request.messages[-1].text_content(),
            max_chars=self.settings.retrieval_query_max_chars,
        )
        conversation_id, history, user_message_id, assistant_message_id = self._create_message_pair(
            request.requested_conversation_id(), question
        )
        rewrite, rewrite_warning = self._rewrite(question, history)
        try:
            retrieval = self.retrieval_service.search(
                RetrievalSearchRequest(
                    query=question,
                    mode=request.mode,
                    top_k=request.top_k,
                    context_budget_tokens=request.context_budget_tokens,
                    debug=request.debug,
                ),
                rewrite_override=rewrite,
            )
        except (RetrievalError, QueryError) as exc:
            code = exc.code
            message = exc.message
            retryable = isinstance(exc, RetrievalError) and exc.retryable
            self._fail_message(
                assistant_message_id,
                {"code": code, "message": message, "retryable": retryable},
            )
            raise ChatError(
                code,
                message,
                retryable=retryable,
                status_code=503 if retryable else 422,
            ) from exc

        sources = self._load_sources(retrieval)
        generation_request = None
        invocation_id = None
        if sources:
            generation_request = grounded_request(
                question,
                sources,
                max_output_tokens=self.settings.llm_max_output_tokens,
                prompt_version=self.settings.generation_prompt_version,
            )
            invocation_id = self._create_invocation(
                purpose="rag_generation",
                prompt_version=self.settings.generation_prompt_version,
                request_snapshot={
                    "conversation_id": str(conversation_id),
                    "message_id": str(assistant_message_id),
                    "retrieval_trace_id": str(retrieval.trace_id),
                    "context_node_ids": [str(source.node_id) for source in sources],
                    "context_used_tokens": retrieval.context_used_tokens,
                },
            )
        self._attach_retrieval(
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            rewritten_question=rewrite.query,
            retrieval_trace_id=retrieval.trace_id,
            invocation_id=invocation_id,
        )
        return PreparedChat(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            invocation_id=invocation_id,
            question=question,
            rewritten_question=rewrite.query,
            retrieval=retrieval,
            sources=sources,
            generation_request=generation_request,
            rewrite_warning=rewrite_warning,
        )

    def stream(self, prepared: PreparedChat) -> Iterator[str]:
        text_id = f"text-{prepared.assistant_message_id}"
        yield _sse({"type": "start", "messageId": str(prepared.assistant_message_id)})
        yield _sse(
            {
                "type": "data-conversation",
                "data": {
                    "conversation_id": str(prepared.conversation_id),
                    "message_id": str(prepared.assistant_message_id),
                },
            }
        )
        yield _sse(
            {
                "type": "data-retrieval-status",
                "data": {
                    "status": prepared.retrieval.status.value,
                    "trace_id": str(prepared.retrieval.trace_id),
                    "query_rewritten": prepared.rewritten_question,
                    "source_count": len(prepared.sources),
                },
            }
        )
        if prepared.rewrite_warning:
            yield _sse(
                {
                    "type": "data-warning",
                    "data": {
                        "code": prepared.rewrite_warning,
                        "message": (
                            "Conversation rewrite was unavailable; the latest question was used."
                        ),
                    },
                }
            )
        if prepared.retrieval.rerank_fallback_reason:
            yield _sse(
                {
                    "type": "data-warning",
                    "data": {
                        "code": prepared.retrieval.rerank_fallback_reason,
                        "message": "Reranking was unavailable; fused retrieval order was used.",
                    },
                }
            )
        for source in prepared.sources:
            yield _sse(
                {
                    "type": "data-source",
                    "data": source.snapshot(
                        excerpt_max_chars=self.settings.citation_excerpt_max_chars
                    ),
                }
            )
        yield _sse({"type": "text-start", "id": text_id})

        if prepared.generation_request is None:
            refusal = _refusal_text(prepared.question)
            yield _sse({"type": "text-delta", "id": text_id, "delta": refusal})
            yield _sse({"type": "text-end", "id": text_id})
            self._complete_refusal(prepared.assistant_message_id, refusal)
            yield _sse(
                {
                    "type": "data-usage",
                    "data": {"refused": True, "input_tokens": 0, "output_tokens": 0},
                }
            )
            yield _sse({"type": "finish"})
            yield "data: [DONE]\n\n"
            return

        answer_parts: list[str] = []
        usage = LLMUsage()
        response_id: str | None = None
        finish_reason: str | None = None
        retry_count = 0
        started = time.perf_counter()
        first_token_ms: float | None = None
        emitted_text = False
        log_context = self._llm_log_context(
            purpose="rag_generation",
            invocation_id=prepared.invocation_id,
        )
        _logger.info(
            "llm_request_started",
            **log_context,
            max_attempts=self.settings.llm_max_retries + 1,
            max_output_tokens=prepared.generation_request.max_output_tokens,
        )
        with observe(
            "llm.rag_generation",
            as_type="generation",
            input={"question": prepared.question},
            metadata={
                "conversation_id": str(prepared.conversation_id),
                "message_id": str(prepared.assistant_message_id),
                "retrieval_trace_id": str(prepared.retrieval.trace_id),
                "model_invocation_id": str(prepared.invocation_id),
                "context_node_count": len(prepared.sources),
                "prompt_version": self.settings.generation_prompt_version,
            },
            version=self.settings.generation_prompt_version,
            model=self.provider.model,
            model_parameters={"max_output_tokens": self.settings.llm_max_output_tokens},
        ) as generation:
            try:
                for retry_count in range(self.settings.llm_max_retries + 1):
                    try:
                        for event in self.provider.stream(prepared.generation_request):
                            if event.type == "text_delta":
                                if first_token_ms is None:
                                    first_token_ms = round(
                                        (time.perf_counter() - started) * 1000, 3
                                    )
                                emitted_text = True
                                answer_parts.append(event.delta)
                                yield _sse(
                                    {"type": "text-delta", "id": text_id, "delta": event.delta}
                                )
                            else:
                                usage = event.usage
                                response_id = event.response_id
                                finish_reason = event.finish_reason
                        break
                    except LLMProviderError as exc:
                        if (
                            emitted_text
                            or not exc.retryable
                            or retry_count >= self.settings.llm_max_retries
                        ):
                            raise
                        _logger.warning(
                            "llm_request_retry",
                            **log_context,
                            attempt=retry_count + 1,
                            next_attempt=retry_count + 2,
                            max_attempts=self.settings.llm_max_retries + 1,
                            error_code=exc.code,
                            error_message=exc.message,
                            status_code=exc.status_code,
                            retryable=exc.retryable,
                            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                        )
                        self._sleep(retry_count)
                answer = "".join(answer_parts).strip()
                if not answer:
                    raise LLMProviderError(
                        "LLM_EMPTY_RESPONSE",
                        "Generation service returned no text",
                        retryable=False,
                    )
                latency_ms = round((time.perf_counter() - started) * 1000, 3)
                yield _sse({"type": "text-end", "id": text_id})
                citation_count = self._complete_answer(
                    prepared,
                    answer=answer,
                    usage=usage,
                    response_id=response_id,
                    finish_reason=finish_reason,
                    retry_count=retry_count,
                    latency_ms=latency_ms,
                    first_token_ms=first_token_ms,
                )
                generation.update(
                    output=answer,
                    metadata={
                        "latency_ms": latency_ms,
                        "first_token_ms": first_token_ms,
                        "retry_count": retry_count,
                        "citation_count": citation_count,
                        "finish_reason": finish_reason,
                    },
                    usage_details=_usage_details(usage),
                    cost_details=_cost_details(self._estimated_cost(usage)),
                )
                _logger.info(
                    "llm_request_succeeded",
                    **log_context,
                    attempts=retry_count + 1,
                    latency_ms=latency_ms,
                    first_token_ms=first_token_ms,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    finish_reason=finish_reason,
                    citation_count=citation_count,
                )
                yield _sse(
                    {
                        "type": "data-usage",
                        "data": {
                            **usage.snapshot(),
                            "model": self.provider.model,
                            "retry_count": retry_count,
                            "first_token_ms": first_token_ms,
                            "latency_ms": latency_ms,
                            "citation_count": citation_count,
                        },
                    }
                )
                yield _sse({"type": "finish"})
                yield "data: [DONE]\n\n"
            except LLMProviderError as exc:
                latency_ms = round((time.perf_counter() - started) * 1000, 3)
                error = {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "status_code": exc.status_code,
                }
                generation.update(
                    level="ERROR",
                    status_message=exc.code,
                    metadata={"latency_ms": latency_ms, "retry_count": retry_count},
                )
                self._fail_generation(
                    prepared,
                    partial_text="".join(answer_parts),
                    error=error,
                    retry_count=retry_count,
                    latency_ms=latency_ms,
                )
                _logger.error(
                    "llm_request_failed",
                    **log_context,
                    attempts=retry_count + 1,
                    latency_ms=latency_ms,
                    error_code=exc.code,
                    error_message=exc.message,
                    status_code=exc.status_code,
                    retryable=exc.retryable,
                    partial_response=emitted_text,
                )
                yield _sse({"type": "text-end", "id": text_id})
                safe_message = "Generation service is temporarily unavailable. Please try again."
                yield _sse(
                    {
                        "type": "data-warning",
                        "data": {
                            "code": exc.code,
                            "message": safe_message,
                            "retryable": exc.retryable,
                        },
                    }
                )
                yield _sse({"type": "error", "errorText": safe_message})
                yield _sse({"type": "finish"})
                yield "data: [DONE]\n\n"

    def _create_message_pair(
        self, conversation_id: uuid.UUID | None, question: str
    ) -> tuple[uuid.UUID, list[tuple[str, str]], uuid.UUID, uuid.UUID]:
        with self.session_factory.begin() as db:
            conversation = db.get(Conversation, conversation_id) if conversation_id else None
            if conversation_id and conversation is None:
                raise ChatError(
                    "CONVERSATION_NOT_FOUND",
                    "Conversation was not found",
                    retryable=False,
                    status_code=404,
                )
            if conversation is None:
                conversation = Conversation(title=question[:200], status=ConversationStatus.ACTIVE)
                db.add(conversation)
                db.flush()
            if conversation.status is not ConversationStatus.ACTIVE:
                raise ChatError(
                    "CONVERSATION_DELETED",
                    "Conversation has been deleted",
                    retryable=False,
                    status_code=409,
                )
            history_rows = list(
                db.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation.id,
                        Message.status.in_([MessageStatus.COMPLETED, MessageStatus.REFUSED]),
                    )
                    .order_by(Message.created_at.desc())
                    .limit(self.settings.query_rewrite_history_messages)
                )
            )
            history = [(row.role.value, row.content) for row in reversed(history_rows)]
            user_message = Message(
                conversation_id=conversation.id,
                role=MessageRole.USER,
                status=MessageStatus.COMPLETED,
                content=question,
                query_original=question,
                finished_at=datetime.now(UTC),
            )
            assistant_message = Message(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                status=MessageStatus.STREAMING,
                content="",
                query_original=question,
            )
            db.add_all([user_message, assistant_message])
            conversation.updated_at = datetime.now(UTC)
            db.flush()
            return conversation.id, history, user_message.id, assistant_message.id

    def _rewrite(
        self, question: str, history: list[tuple[str, str]]
    ) -> tuple[QueryRewriteResult, str | None]:
        if not history:
            return (
                QueryRewriteResult(
                    query=question,
                    strategy="conversation-aware",
                    implementation="identity-without-history",
                    version="1.0.0",
                    changed=False,
                    metadata={"history_message_count": 0},
                ),
                None,
            )
        request = rewrite_request(
            question,
            history,
            max_output_tokens=self.settings.query_rewrite_max_output_tokens,
            prompt_version=self.settings.query_rewrite_prompt_version,
        )
        invocation_id = self._create_invocation(
            purpose="query_rewrite",
            prompt_version=self.settings.query_rewrite_prompt_version,
            request_snapshot={"history_message_count": len(history)},
        )
        started = time.perf_counter()
        retry_count = 0
        log_context = self._llm_log_context(
            purpose="query_rewrite",
            invocation_id=invocation_id,
        )
        _logger.info(
            "llm_request_started",
            **log_context,
            max_attempts=self.settings.llm_max_retries + 1,
            max_output_tokens=request.max_output_tokens,
        )
        with observe(
            "llm.query_rewrite",
            as_type="generation",
            input={"question": question, "history_message_count": len(history)},
            metadata={"model_invocation_id": str(invocation_id)},
            version=self.settings.query_rewrite_prompt_version,
            model=self.provider.model,
            model_parameters={"max_output_tokens": self.settings.query_rewrite_max_output_tokens},
        ) as generation:
            try:
                for retry_count in range(self.settings.llm_max_retries + 1):
                    try:
                        response = self.provider.generate(request)
                        break
                    except LLMProviderError as exc:
                        if not exc.retryable or retry_count >= self.settings.llm_max_retries:
                            raise
                        _logger.warning(
                            "llm_request_retry",
                            **log_context,
                            attempt=retry_count + 1,
                            next_attempt=retry_count + 2,
                            max_attempts=self.settings.llm_max_retries + 1,
                            error_code=exc.code,
                            error_message=exc.message,
                            status_code=exc.status_code,
                            retryable=exc.retryable,
                            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                        )
                        self._sleep(retry_count)
                rewritten = normalize_query(
                    response.text.strip().strip("\"'"),
                    max_chars=self.settings.retrieval_query_max_chars,
                )
                latency_ms = round((time.perf_counter() - started) * 1000, 3)
                self._complete_invocation(
                    invocation_id,
                    usage=response.usage,
                    response_id=response.response_id,
                    finish_reason=response.finish_reason,
                    retry_count=retry_count,
                    latency_ms=latency_ms,
                )
                generation.update(
                    output=rewritten,
                    metadata={"latency_ms": latency_ms, "retry_count": retry_count},
                    usage_details=_usage_details(response.usage),
                    cost_details=_cost_details(self._estimated_cost(response.usage)),
                )
                _logger.info(
                    "llm_request_succeeded",
                    **log_context,
                    attempts=retry_count + 1,
                    latency_ms=latency_ms,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    total_tokens=response.usage.total_tokens,
                    finish_reason=response.finish_reason,
                )
                return (
                    QueryRewriteResult(
                        query=rewritten,
                        strategy="conversation-aware",
                        implementation="llm-query-rewriter",
                        version="1.0.0",
                        changed=rewritten != question,
                        metadata={
                            "history_message_count": len(history),
                            "prompt_version": self.settings.query_rewrite_prompt_version,
                            "invocation_id": str(invocation_id),
                        },
                    ),
                    None,
                )
            except (LLMProviderError, QueryError) as exc:
                latency_ms = round((time.perf_counter() - started) * 1000, 3)
                code = exc.code
                message = exc.message
                generation.update(
                    level="WARNING",
                    status_message=code,
                    metadata={"latency_ms": latency_ms, "retry_count": retry_count},
                )
                self._fail_invocation(
                    invocation_id,
                    {"code": code, "message": message},
                    retry_count=retry_count,
                    latency_ms=latency_ms,
                )
                _logger.warning(
                    "llm_request_failed",
                    **log_context,
                    attempts=retry_count + 1,
                    latency_ms=latency_ms,
                    error_code=code,
                    error_message=message,
                    status_code=(exc.status_code if isinstance(exc, LLMProviderError) else None),
                    retryable=(exc.retryable if isinstance(exc, LLMProviderError) else False),
                    fallback=True,
                )
                return (
                    QueryRewriteResult(
                        query=question,
                        strategy="conversation-aware-fallback",
                        implementation="llm-query-rewriter",
                        version="1.0.0",
                        changed=False,
                        metadata={
                            "history_message_count": len(history),
                            "prompt_version": self.settings.query_rewrite_prompt_version,
                            "fallback_reason": code,
                        },
                    ),
                    code,
                )

    def _load_sources(self, retrieval: RetrievalSearchResponse) -> list[ChatSource]:
        node_ids = [value.node_id for value in retrieval.context_nodes]
        if not node_ids:
            return []
        with self.session_factory() as db:
            rows = db.execute(
                select(RetrievalNode, Document)
                .join(Document, RetrievalNode.document_id == Document.id)
                .where(RetrievalNode.id.in_(node_ids))
            ).all()
        by_id = {node.id: (node, document) for node, document in rows}
        sources: list[ChatSource] = []
        for index, context in enumerate(retrieval.context_nodes, start=1):
            row = by_id.get(context.node_id)
            if row is None:
                continue
            node, document = row
            sources.append(
                ChatSource(
                    label=f"S{index}",
                    node_id=node.id,
                    document_id=node.document_id,
                    document_version_id=node.document_version_id,
                    document_name=document.display_name,
                    title=node.title,
                    heading_path=node.heading_path,
                    content=context.content,
                    content_types=context.content_types,
                    source_locators=context.source_locators,
                )
            )
        return sources

    def _create_invocation(
        self,
        *,
        purpose: str,
        prompt_version: str,
        request_snapshot: dict[str, object],
    ) -> uuid.UUID:
        with self.session_factory.begin() as db:
            invocation = ModelInvocation(
                purpose=purpose,
                provider=self.provider.provider,
                model=self.provider.model,
                endpoint=self.provider.endpoint,
                prompt_version=prompt_version,
                status=ModelInvocationStatus.RUNNING,
                trace_id=current_trace_id(),
                request_snapshot=request_snapshot,
            )
            db.add(invocation)
            db.flush()
            return invocation.id

    def _attach_retrieval(
        self,
        *,
        user_message_id: uuid.UUID,
        assistant_message_id: uuid.UUID,
        rewritten_question: str,
        retrieval_trace_id: uuid.UUID,
        invocation_id: uuid.UUID | None,
    ) -> None:
        with self.session_factory.begin() as db:
            for message_id in (user_message_id, assistant_message_id):
                message = db.get(Message, message_id)
                if message is None:
                    raise RuntimeError("Chat message disappeared")
                message.query_rewritten = rewritten_question
                message.retrieval_trace_id = retrieval_trace_id
            assistant = db.get(Message, assistant_message_id)
            if assistant is not None:
                assistant.model_invocation_id = invocation_id

    def _complete_refusal(self, message_id: uuid.UUID, refusal: str) -> None:
        with self.session_factory.begin() as db:
            message = db.get(Message, message_id)
            if message is not None:
                message.status = MessageStatus.REFUSED
                message.content = refusal
                message.metadata_json = {"refusal_reason": "no_retrieval_context"}
                message.finished_at = datetime.now(UTC)

    def _complete_answer(
        self,
        prepared: PreparedChat,
        *,
        answer: str,
        usage: LLMUsage,
        response_id: str | None,
        finish_reason: str | None,
        retry_count: int,
        latency_ms: float,
        first_token_ms: float | None,
    ) -> int:
        referenced = _referenced_labels(answer, prepared.sources)
        estimated_cost = self._estimated_cost(usage)
        with self.session_factory.begin() as db:
            message = db.get(Message, prepared.assistant_message_id)
            if message is None:
                raise RuntimeError("Assistant message disappeared")
            message.status = MessageStatus.COMPLETED
            message.content = answer
            message.metadata_json = {
                "prompt_version": self.settings.generation_prompt_version,
                "provider": self.provider.provider,
                "model": self.provider.model,
                "response_id": response_id,
                "finish_reason": finish_reason,
            }
            message.finished_at = datetime.now(UTC)
            for index, source in enumerate(referenced, start=1):
                snapshot = source.snapshot(
                    excerpt_max_chars=self.settings.citation_excerpt_max_chars
                )
                db.add(
                    Citation(
                        message_id=message.id,
                        citation_index=index,
                        source_label=source.label,
                        node_id=source.node_id,
                        document_id=source.document_id,
                        document_version_id=source.document_version_id,
                        document_name=source.document_name,
                        heading_path=source.heading_path,
                        source_locators_json=source.source_locators,
                        excerpt=str(snapshot["excerpt"]),
                        snapshot_json=snapshot,
                    )
                )
            if prepared.invocation_id is not None:
                invocation = db.get(ModelInvocation, prepared.invocation_id)
                if invocation is not None:
                    invocation.status = ModelInvocationStatus.SUCCEEDED
                    invocation.input_tokens = usage.input_tokens
                    invocation.output_tokens = usage.output_tokens
                    invocation.latency_ms = latency_ms
                    invocation.estimated_cost_usd = estimated_cost
                    invocation.retry_count = retry_count
                    invocation.response_snapshot = {
                        "response_id": response_id,
                        "finish_reason": finish_reason,
                        "citation_count": len(referenced),
                        "first_token_ms": first_token_ms,
                    }
                    invocation.finished_at = datetime.now(UTC)
        return len(referenced)

    def _complete_invocation(
        self,
        invocation_id: uuid.UUID,
        *,
        usage: LLMUsage,
        response_id: str | None,
        finish_reason: str | None,
        retry_count: int,
        latency_ms: float,
    ) -> None:
        with self.session_factory.begin() as db:
            invocation = db.get(ModelInvocation, invocation_id)
            if invocation is not None:
                invocation.status = ModelInvocationStatus.SUCCEEDED
                invocation.input_tokens = usage.input_tokens
                invocation.output_tokens = usage.output_tokens
                invocation.latency_ms = latency_ms
                invocation.estimated_cost_usd = self._estimated_cost(usage)
                invocation.retry_count = retry_count
                invocation.response_snapshot = {
                    "response_id": response_id,
                    "finish_reason": finish_reason,
                }
                invocation.finished_at = datetime.now(UTC)

    def _fail_generation(
        self,
        prepared: PreparedChat,
        *,
        partial_text: str,
        error: dict[str, object],
        retry_count: int,
        latency_ms: float,
    ) -> None:
        self._fail_message(prepared.assistant_message_id, error, partial_text=partial_text)
        if prepared.invocation_id is not None:
            self._fail_invocation(
                prepared.invocation_id,
                error,
                retry_count=retry_count,
                latency_ms=latency_ms,
            )

    def _fail_message(
        self,
        message_id: uuid.UUID,
        error: dict[str, object],
        *,
        partial_text: str = "",
    ) -> None:
        with self.session_factory.begin() as db:
            message = db.get(Message, message_id)
            if message is not None:
                message.status = MessageStatus.FAILED
                message.content = partial_text
                message.error = error
                message.finished_at = datetime.now(UTC)

    def _fail_invocation(
        self,
        invocation_id: uuid.UUID,
        error: dict[str, object],
        *,
        retry_count: int,
        latency_ms: float,
    ) -> None:
        with self.session_factory.begin() as db:
            invocation = db.get(ModelInvocation, invocation_id)
            if invocation is not None:
                invocation.status = ModelInvocationStatus.FAILED
                invocation.error = error
                invocation.retry_count = retry_count
                invocation.latency_ms = latency_ms
                invocation.finished_at = datetime.now(UTC)

    def _estimated_cost(self, usage: LLMUsage) -> float | None:
        input_price = self.settings.llm_price_per_million_input_tokens
        output_price = self.settings.llm_price_per_million_output_tokens
        if input_price is None and output_price is None:
            return None
        return (
            (usage.input_tokens or 0) * (input_price or 0)
            + (usage.output_tokens or 0) * (output_price or 0)
        ) / 1_000_000

    def _sleep(self, retry_count: int) -> None:
        delay = self.settings.llm_retry_base_seconds * (2**retry_count)
        delay *= 0.5 + self.jitter()
        self.sleeper(delay)

    def _llm_log_context(
        self,
        *,
        purpose: str,
        invocation_id: uuid.UUID | None,
    ) -> dict[str, object]:
        return {
            "purpose": purpose,
            "provider": self.provider.provider,
            "model": self.provider.model,
            "endpoint": self.provider.endpoint,
            "invocation_id": str(invocation_id) if invocation_id else None,
            "trace_id": current_trace_id(),
        }


def build_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_api_style != "responses":
        raise RuntimeError("Stage 8 supports only the configured Responses API style")
    if settings.llm_api_key is None:
        raise RuntimeError("LLM_API_KEY is required for direct Responses API access")
    return ResponsesAPIProvider(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        reasoning_effort=settings.llm_reasoning_effort,
        api_key=settings.llm_api_key.get_secret_value(),
        timeout_seconds=settings.llm_timeout_seconds,
    )


@lru_cache(maxsize=1)
def get_chat_service() -> ChatService:
    settings = get_settings()
    return ChatService(
        session_factory=SessionLocal,
        retrieval_service=get_retrieval_service(),
        provider=build_llm_provider(settings),
        settings=settings,
    )


def _referenced_labels(answer: str, sources: list[ChatSource]) -> list[ChatSource]:
    labels = {match.group(1) for match in re.finditer(r"\[(S\d+)\]", answer)}
    return [source for source in sources if source.label in labels]


def _refusal_text(query: str) -> str:
    if re.search(r"[\u3400-\u9fff]", query):
        return "在当前企业知识库中没有找到足够的信息来回答这个问题。"
    return (
        "The current enterprise knowledge base does not contain enough information "
        "to answer this question."
    )


def _sse(event: dict[str, object]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _usage_details(usage: LLMUsage) -> dict[str, int]:
    return {
        key: value
        for key, value in {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "total": usage.total_tokens,
        }.items()
        if value is not None
    }


def _cost_details(estimated_cost_usd: float | None) -> dict[str, float] | None:
    return {"total": estimated_cost_usd} if estimated_cost_usd is not None else None
