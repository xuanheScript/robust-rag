"""Bounded LangGraph Agentic RAG orchestration over controlled domain tools."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from robust_rag.core.observability import observe
from robust_rag.core.settings import Settings
from robust_rag.db.enums import RetrievalMode
from robust_rag.generation.prompts import agent_decision_request
from robust_rag.generation.provider import (
    LLMProviderError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMUsage,
)
from robust_rag.generation.schemas import ChatSource
from robust_rag.retrieval.query import QueryError, QueryRewriteResult, normalize_query
from robust_rag.retrieval.schemas import RetrievalSearchRequest, RetrievalSearchResponse
from robust_rag.retrieval.service import RetrievalError, RetrievalService

AgentAction = Literal["direct", "documents", "relationships", "insufficient"]
TrackedStream = Callable[
    [str, str, LLMRequest, dict[str, object]],
    tuple[uuid.UUID, Iterator[LLMStreamEvent]],
]
SourceLoader = Callable[[RetrievalSearchResponse], list[ChatSource]]
QueryPlanner = Callable[[str, list[tuple[str, str]]], tuple[QueryRewriteResult, str | None]]


class AgentState(TypedDict):
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    question: str
    history: list[tuple[str, str]]
    query: str
    query_plan: QueryRewriteResult | None
    action: AgentAction
    selected_tool: str | None
    tool_call_id: str | None
    direct_answer: str | None
    retrieval: RetrievalSearchResponse | None
    sources: list[ChatSource]
    tool_call_count: int
    warnings: list[str]
    invocation_ids: list[uuid.UUID]
    direct_invocation_id: uuid.UUID | None
    direct_usage: LLMUsage
    rewrite_warning: str | None


@dataclass(frozen=True)
class AgentRunResult:
    action: AgentAction
    selected_tool: str | None
    tool_call_id: str | None
    query: str
    direct_answer: str | None
    retrieval: RetrievalSearchResponse | None
    sources: list[ChatSource]
    tool_call_count: int
    warnings: tuple[str, ...]
    invocation_ids: tuple[uuid.UUID, ...]
    direct_invocation_id: uuid.UUID | None
    direct_usage: LLMUsage
    rewrite_warning: str | None


@dataclass(frozen=True)
class AgentStreamEvent:
    type: Literal["text_delta", "action", "completed"]
    delta: str = ""
    action: AgentAction | None = None
    result: AgentRunResult | None = None


class AgenticRAGGraph:
    """Bounded decide → optional retrieval Agentic RAG graph."""

    document_tool = "retrieve_enterprise_documents"
    relationship_tool = "retrieve_enterprise_relationships"

    def __init__(
        self,
        *,
        retrieval_service: RetrievalService,
        source_loader: SourceLoader,
        query_planner: QueryPlanner,
        stream_generate: TrackedStream,
        settings: Settings,
        mode: RetrievalMode,
        top_k: int | None,
        context_budget_tokens: int | None,
        debug: bool,
        conversation_id: uuid.UUID,
        user_message_id: uuid.UUID,
        assistant_message_id: uuid.UUID,
    ) -> None:
        self.retrieval_service = retrieval_service
        self.source_loader = source_loader
        self.query_planner = query_planner
        self.stream_generate = stream_generate
        self.settings = settings
        self.mode = mode
        self.top_k = top_k
        self.context_budget_tokens = context_budget_tokens
        self.debug = debug
        self.conversation_id = conversation_id
        self.user_message_id = user_message_id
        self.assistant_message_id = assistant_message_id
        workflow = StateGraph(AgentState)
        workflow.add_node("agent", self._agent)
        workflow.add_node("query_rewrite", self._query_rewrite)
        workflow.add_node("retrieve", self._retrieve)
        workflow.add_edge(START, "agent")
        workflow.add_conditional_edges(
            "agent",
            self._route_agent,
            {"retrieve": "query_rewrite", "end": END},
        )
        workflow.add_edge("query_rewrite", "retrieve")
        workflow.add_edge("retrieve", END)
        self.graph = workflow.compile()

    def run(self, question: str, history: list[tuple[str, str]]) -> AgentRunResult:
        result: AgentRunResult | None = None
        for event in self.stream(question, history):
            if event.type == "completed":
                result = event.result
        if result is None:
            raise RuntimeError("Agent graph ended without a result")
        return result

    def stream(self, question: str, history: list[tuple[str, str]]) -> Iterator[AgentStreamEvent]:
        final: AgentState | None = None
        chunks = cast(
            Iterator[tuple[str, object]],
            self.graph.stream(
                self._initial_state(question, history),
                config={"recursion_limit": self.settings.agent_recursion_limit},
                stream_mode=["custom", "values"],
            ),
        )
        for mode, payload in chunks:
            if mode == "values" and isinstance(payload, dict):
                final = cast(AgentState, payload)
                continue
            if mode != "custom" or not isinstance(payload, dict):
                continue
            event_type = payload.get("type")
            if event_type == "text_delta" and isinstance(payload.get("delta"), str):
                yield AgentStreamEvent(type="text_delta", delta=str(payload["delta"]))
            elif event_type == "action" and payload.get("action") in {
                "direct",
                "documents",
                "relationships",
            }:
                yield AgentStreamEvent(
                    type="action",
                    action=cast(AgentAction, payload["action"]),
                )
        if final is None:
            raise RuntimeError("Agent graph ended without state")
        yield AgentStreamEvent(type="completed", result=self._result(final))

    def _initial_state(self, question: str, history: list[tuple[str, str]]) -> AgentState:
        return {
            "conversation_id": self.conversation_id,
            "user_message_id": self.user_message_id,
            "assistant_message_id": self.assistant_message_id,
            "question": question,
            "history": history,
            "query": question,
            "query_plan": None,
            "action": "insufficient",
            "selected_tool": None,
            "tool_call_id": None,
            "direct_answer": None,
            "retrieval": None,
            "sources": [],
            "tool_call_count": 0,
            "warnings": [],
            "invocation_ids": [],
            "direct_invocation_id": None,
            "direct_usage": LLMUsage(),
            "rewrite_warning": None,
        }

    @staticmethod
    def _result(final: AgentState) -> AgentRunResult:
        result_query = final["query_plan"].query if final["query_plan"] else final["query"]
        return AgentRunResult(
            action=final["action"],
            selected_tool=final["selected_tool"],
            tool_call_id=final["tool_call_id"],
            query=result_query,
            direct_answer=final["direct_answer"],
            retrieval=final["retrieval"],
            sources=final["sources"],
            tool_call_count=final["tool_call_count"],
            warnings=tuple(final["warnings"]),
            invocation_ids=tuple(final["invocation_ids"]),
            direct_invocation_id=final["direct_invocation_id"],
            direct_usage=final["direct_usage"],
            rewrite_warning=final["rewrite_warning"],
        )

    def _agent(self, state: AgentState) -> dict[str, object]:
        writer = get_stream_writer()
        request = agent_decision_request(
            state["query"],
            state["history"],
            max_output_tokens=self.settings.agent_max_output_tokens,
            prompt_version=self.settings.agent_prompt_version,
            reasoning_effort=self.settings.agent_reasoning_effort,
        )
        with observe(
            "agent.decide",
            as_type="agent",
            input={"query": state["query"], "history_messages": len(state["history"])},
            metadata={
                "graph_version": self.settings.agent_graph_version,
                "conversation_id": str(state["conversation_id"]),
                "message_id": str(state["assistant_message_id"]),
                "tool_call_count": state["tool_call_count"],
                "reasoning_effort": self.settings.agent_reasoning_effort,
            },
            version=self.settings.agent_prompt_version,
        ) as span:
            invocation_id, events = self.stream_generate(
                "agent_decision",
                self.settings.agent_prompt_version,
                request,
                {
                    "conversation_id": str(state["conversation_id"]),
                    "message_id": str(state["assistant_message_id"]),
                    "history_message_count": len(state["history"]),
                    "tool_call_count": state["tool_call_count"],
                    "reasoning_effort": self.settings.agent_reasoning_effort,
                },
            )
            answer_parts: list[str] = []
            completion: LLMStreamEvent | None = None
            try:
                for event in events:
                    if event.type == "text_delta":
                        answer_parts.append(event.delta)
                    else:
                        completion = event
            except LLMProviderError as exc:
                if answer_parts:
                    writer({"type": "action", "action": "direct"})
                    for delta in answer_parts:
                        writer({"type": "text_delta", "delta": delta})
                    span.update(level="ERROR", status_message=exc.code)
                    raise
                span.update(
                    output={"action": "documents", "fallback": True},
                    level="WARNING",
                    status_message=exc.code,
                )
                writer({"type": "action", "action": "documents"})
                return {
                    "action": "documents",
                    "selected_tool": self.document_tool,
                    "tool_call_id": None,
                    "invocation_ids": [*state["invocation_ids"], invocation_id],
                    "warnings": [*state["warnings"], exc.code],
                }

            if completion is None:
                raise RuntimeError("Agent model stream ended without completion")
            response = LLMResponse(
                text="".join(answer_parts),
                response_id=completion.response_id,
                usage=completion.usage,
                finish_reason=completion.finish_reason,
                tool_calls=completion.tool_calls,
            )
            invocation_ids = [*state["invocation_ids"], invocation_id]
            mixed_output = bool(response.text.strip() and response.tool_calls)
            if len(response.tool_calls) != 1:
                if response.tool_calls:
                    span.update(
                        output={"action": "documents", "fallback": True},
                        level="WARNING",
                        status_message="AGENT_INVALID_TOOL_COUNT",
                    )
                    writer({"type": "action", "action": "documents"})
                    return {
                        "action": "documents",
                        "selected_tool": self.document_tool,
                        "tool_call_id": None,
                        "invocation_ids": invocation_ids,
                        "warnings": [*state["warnings"], "AGENT_INVALID_TOOL_COUNT"],
                    }
                answer = response.text.strip()
                if answer:
                    writer({"type": "action", "action": "direct"})
                    for delta in answer_parts:
                        writer({"type": "text_delta", "delta": delta})
                    span.update(output={"action": "direct", "answer": answer})
                    return {
                        "action": "direct",
                        "selected_tool": None,
                        "tool_call_id": None,
                        "direct_answer": answer,
                        "direct_invocation_id": invocation_id,
                        "direct_usage": response.usage,
                        "invocation_ids": invocation_ids,
                    }
                writer({"type": "action", "action": "documents"})
                return {
                    "action": "documents",
                    "selected_tool": self.document_tool,
                    "tool_call_id": None,
                    "invocation_ids": invocation_ids,
                    "warnings": [*state["warnings"], "AGENT_EMPTY_DECISION"],
                }

            tool_call = response.tool_calls[0]
            raw_query = tool_call.arguments.get("query")
            if not isinstance(raw_query, str):
                writer({"type": "action", "action": "documents"})
                return {
                    "action": "documents",
                    "selected_tool": self.document_tool,
                    "tool_call_id": tool_call.call_id,
                    "invocation_ids": invocation_ids,
                    "warnings": [*state["warnings"], "AGENT_INVALID_TOOL_ARGUMENTS"],
                }
            try:
                query = normalize_query(
                    raw_query, max_chars=self.settings.retrieval_query_max_chars
                )
            except QueryError:
                writer({"type": "action", "action": "documents"})
                return {
                    "action": "documents",
                    "selected_tool": self.document_tool,
                    "tool_call_id": tool_call.call_id,
                    "invocation_ids": invocation_ids,
                    "warnings": [*state["warnings"], "AGENT_INVALID_TOOL_QUERY"],
                }
            if tool_call.name == self.relationship_tool:
                action: AgentAction = "relationships"
            elif tool_call.name == self.document_tool:
                action = "documents"
            else:
                action = "documents"
                state_warnings = [*state["warnings"], "AGENT_UNKNOWN_TOOL"]
                span.update(
                    output={"action": action, "fallback": True},
                    level="WARNING",
                    status_message="AGENT_UNKNOWN_TOOL",
                )
                writer({"type": "action", "action": action})
                return {
                    "action": action,
                    "query": query,
                    "selected_tool": self.document_tool,
                    "tool_call_id": tool_call.call_id,
                    "invocation_ids": invocation_ids,
                    "warnings": state_warnings,
                }
            if mixed_output:
                span.update(
                    output={
                        "action": action,
                        "tool": tool_call.name,
                        "ignored_text_chars": len(response.text.strip()),
                    },
                    level="WARNING",
                    status_message="AGENT_MIXED_OUTPUT_IGNORED",
                )
            else:
                span.update(output={"action": action, "tool": tool_call.name})
            writer({"type": "action", "action": action})
            return {
                "action": action,
                "query": query,
                "selected_tool": tool_call.name,
                "tool_call_id": tool_call.call_id,
                "invocation_ids": invocation_ids,
            }

    @staticmethod
    def _route_agent(state: AgentState) -> Literal["retrieve", "end"]:
        return "retrieve" if state["action"] in {"documents", "relationships"} else "end"

    def _retrieve(self, state: AgentState) -> dict[str, object]:
        use_graph = state["action"] == "relationships"
        tool_name = self.relationship_tool if use_graph else self.document_tool
        with observe(
            f"agent.tool.{tool_name}",
            as_type="tool",
            input={"query": state["query"]},
            metadata={"graph_requested": use_graph},
            version=self.settings.agent_graph_version,
        ) as span:
            try:
                rewrite = state["query_plan"] or QueryRewriteResult(
                    query=state["query"],
                    strategy="agent-tool-call-fallback",
                    implementation="langgraph-agent",
                    version=self.settings.agent_graph_version,
                    changed=state["query"] != state["question"],
                    semantic_query=state["query"],
                    metadata={"agent_action": state["action"]},
                )
                retrieval = self.retrieval_service.search(
                    RetrievalSearchRequest(
                        query=state["query"],
                        mode=self.mode,
                        top_k=self.top_k,
                        context_budget_tokens=self.context_budget_tokens,
                        debug=self.debug,
                    ),
                    rewrite_override=rewrite,
                    use_graph=use_graph,
                )
                sources = self.source_loader(retrieval)
                span.update(
                    output={"source_count": len(sources)},
                    metadata={
                        "retrieval_trace_id": str(retrieval.trace_id),
                        "graph_query_trace_id": (
                            str(retrieval.graph_query_trace_id)
                            if retrieval.graph_query_trace_id
                            else None
                        ),
                    },
                )
                warnings = list(state["warnings"])
                if retrieval.graph_fallback_reason:
                    warnings.append(retrieval.graph_fallback_reason)
                if retrieval.rerank_fallback_reason:
                    warnings.append(retrieval.rerank_fallback_reason)
                return {
                    "retrieval": retrieval,
                    "sources": sources,
                    "tool_call_count": state["tool_call_count"] + 1,
                    "warnings": warnings,
                }
            except (RetrievalError, QueryError) as exc:
                code = exc.code
                span.update(level="WARNING", status_message=code)
                return {
                    "retrieval": None,
                    "sources": [],
                    "tool_call_count": state["tool_call_count"] + 1,
                    "warnings": [*state["warnings"], code],
                }

    def _query_rewrite(self, state: AgentState) -> dict[str, object]:
        """Build an additive retrieval plan only after the Agent selects retrieval."""

        rewrite, warning = self.query_planner(state["query"], state["history"])
        invocation_ids = list(state["invocation_ids"])
        raw_invocation_id = rewrite.metadata.get("invocation_id")
        if isinstance(raw_invocation_id, str):
            try:
                invocation_id = uuid.UUID(raw_invocation_id)
            except ValueError:
                invocation_id = None
            if invocation_id is not None and invocation_id not in invocation_ids:
                invocation_ids.append(invocation_id)
        return {
            "query_plan": rewrite,
            "invocation_ids": invocation_ids,
            "rewrite_warning": warning,
        }
