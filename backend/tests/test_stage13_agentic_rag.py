"""Stage 13 LangGraph Agentic RAG routing, tools, and streaming tests."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.core.settings import Settings
from robust_rag.db.models import Message, ModelInvocation, RetrievalNode, RetrievalTrace
from robust_rag.evaluation.schemas import GoldenDataset
from robust_rag.generation.provider import (
    FakeLLMProvider,
    LLMProviderError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMToolCall,
    LLMUsage,
)
from robust_rag.generation.schemas import ChatRequest
from robust_rag.generation.service import ChatService, PreparedChat, get_chat_service
from robust_rag.indexing.opensearch import MemoryOpenSearchAdapter
from robust_rag.retrieval.query import IdentityQueryRewriter
from robust_rag.retrieval.service import RetrievalService
from robust_rag.storage.local import LocalFileStorage
from tests.test_stage7_retrieval import (
    FakeQueryEmbeddingAdapter,
    FakeRerankAdapter,
    _ready_search_fixture,
    _retrieval_service,
)
from tests.test_stage8_generation import _events
from tests.test_stage9_graph import StaticGraphRetriever, _insert_graph_trace


def _response(
    text: str = "",
    *,
    tool: str | None = None,
    query: str = "Policy",
) -> LLMResponse:
    calls = (LLMToolCall(call_id="call-1", name=tool, arguments={"query": query}),) if tool else ()
    return LLMResponse(
        text=text,
        response_id="resp-agent",
        usage=LLMUsage(input_tokens=8, output_tokens=3, total_tokens=11),
        finish_reason="completed",
        tool_calls=calls,
    )


def _request(query: str, conversation_id: UUID | None = None) -> ChatRequest:
    payload: dict[str, object] = {
        "messages": [{"role": "user", "parts": [{"type": "text", "text": query}]}]
    }
    if conversation_id is not None:
        payload["conversation_id"] = str(conversation_id)
    return ChatRequest.model_validate(payload)


def _settings() -> Settings:
    return Settings(_env_file=None).model_copy(
        update={
            "agentic_rag_enabled": True,
            "llm_max_retries": 0,
            "llm_retry_base_seconds": 0,
            "voyage_embedding_max_retries": 0,
            "voyage_rerank_max_retries": 0,
            "opensearch_max_retries": 0,
            "retrieval_context_max_tokens": 1000,
            "retrieval_context_parent_max_tokens": 500,
            "retrieval_bm25_top_k": 20,
            "retrieval_dense_top_k": 20,
            "retrieval_rrf_top_k": 20,
            "retrieval_rerank_candidate_top_k": 10,
            "retrieval_final_child_top_k": 5,
            "citation_excerpt_max_chars": 100,
        }
    )


def _service(
    session_factory: sessionmaker[Session],
    adapter: MemoryOpenSearchAdapter,
    provider: FakeLLMProvider,
) -> ChatService:
    retrieval = _retrieval_service(session_factory, adapter)
    settings = _settings()
    retrieval.settings = settings
    return ChatService(
        session_factory=session_factory,
        retrieval_service=retrieval,
        provider=provider,
        settings=settings,
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    )


def _chunks(service: ChatService, request: ChatRequest) -> tuple[PreparedChat, list[str]]:
    prepared = service.prepare(request)
    return prepared, list(service.stream(prepared))


def _answer(chunks: list[str]) -> str:
    return "".join(
        str(event.get("delta", ""))
        for event in _events(chunks)
        if event.get("type") == "text-delta"
    )


def test_agent_direct_response_skips_all_retrieval(
    session_factory: sessionmaker[Session],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_observations: list[dict[str, object]] = []
    http_observations: list[dict[str, object]] = []

    class Span:
        def __init__(self, target: dict[str, object]) -> None:
            self.target = target

        def update(self, **kwargs: object) -> None:
            cast(list[dict[str, object]], self.target["updates"]).append(kwargs)

    @contextmanager
    def record_model(name: str, **kwargs: object) -> Generator[Span, None, None]:
        target: dict[str, object] = {"name": name, "start": kwargs, "updates": []}
        model_observations.append(target)
        yield Span(target)

    @contextmanager
    def record_http(name: str, **kwargs: object) -> Generator[Span, None, None]:
        target: dict[str, object] = {"name": name, "start": kwargs, "updates": []}
        http_observations.append(target)
        yield Span(target)

    monkeypatch.setattr("robust_rag.generation.service.observe", record_model)
    monkeypatch.setattr("robust_rag.core.middleware.observe", record_http)
    provider = FakeLLMProvider(generate_responses=[_response("你好! 有什么可以帮你?")])
    service = _service(session_factory, MemoryOpenSearchAdapter(), provider)

    prepared, chunks = _chunks(service, _request("你好"))
    events = _events(chunks)

    assert prepared.retrieval is None
    assert prepared.pending_agent is not None
    assert "你好" in _answer(chunks)
    assert any(
        event["type"] == "data-agent-status"
        and cast(dict[str, object], event["data"])["action"] == "direct"
        for event in events
    )
    assert not any(event["type"] == "data-retrieval-status" for event in events)
    assert provider.generate_requests == []
    assert len(provider.stream_requests) == 1
    assert provider.stream_requests[0].metadata["purpose"] == "agent_decision"
    assert provider.stream_requests[0].reasoning_effort == "none"
    assert model_observations[0]["name"] == "llm.agent_decision"
    model_updates = cast(list[dict[str, object]], model_observations[0]["updates"])
    output_update = next(update for update in model_updates if "output" in update)
    assert cast(dict[str, object], output_update["output"])["text"] == ("你好! 有什么可以帮你?")
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(RetrievalTrace)) == 0
        assistant = db.get(Message, prepared.assistant_message_id)
        assert assistant is not None
        assert assistant.metadata_json["agent_action"] == "direct"
        assert assistant.model_invocation_id is not None
        assert db.get(ModelInvocation, assistant.model_invocation_id) is not None

    provider.generate_responses.append(_response("Hello!"))
    cast(FastAPI, client.app).dependency_overrides[get_chat_service] = lambda: service
    response = client.post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    message_id = next(
        str(event["messageId"]) for event in _events([response.text]) if event["type"] == "start"
    )
    trace = client.get(f"/api/v1/messages/{message_id}/trace")
    assert trace.status_code == 200
    assert trace.json()["agent"]["action"] == "direct"
    assert trace.json()["retrieval"] is None
    chat_http = next(
        value
        for value in http_observations
        if cast(dict[str, object], value["start"])["input"]
        == {"method": "POST", "path": "/api/v1/chat"}
    )
    http_updates = cast(list[dict[str, object]], chat_http["updates"])
    assert cast(dict[str, object], http_updates[-1]["output"])["chat_output"] == "Hello!"


def test_agent_direct_response_starts_http_stream_before_model_and_relays_deltas(
    session_factory: sessionmaker[Session],
) -> None:
    provider = FakeLLMProvider(
        generate_responses=[_response("你好。我是企业知识助手。")],
        stream_deltas=["你好。", "我是企业知识助手。"],
    )
    service = _service(session_factory, MemoryOpenSearchAdapter(), provider)
    prepared = service.prepare(_request("你好"))

    chunks = service.stream(prepared)
    assert _events([next(chunks)])[0]["type"] == "start"
    assert _events([next(chunks)])[0]["type"] == "data-conversation"
    assert provider.stream_requests == []

    remaining = list(chunks)
    events = _events(remaining)
    deltas = [str(event["delta"]) for event in events if event["type"] == "text-delta"]
    assert deltas == ["你好。", "我是企业知识助手。"]
    assert provider.generate_requests == []
    assert len(provider.stream_requests) == 1


def test_agent_history_is_stably_ordered_user_then_assistant(
    session_factory: sessionmaker[Session],
) -> None:
    provider = FakeLLMProvider(
        generate_responses=[_response("第一答"), _response("第二答")],
    )
    service = _service(session_factory, MemoryOpenSearchAdapter(), provider)
    first = service.prepare(_request("第一问"))
    list(service.stream(first))

    second = service.prepare(_request("第二问", first.conversation_id))
    assert second.pending_agent is not None
    assert second.pending_agent.history == [("user", "第一问"), ("assistant", "第一答")]

    list(service.stream(second))
    assert [message["role"] for message in provider.stream_requests[1].input] == [
        "user",
        "assistant",
        "user",
    ]


def test_agent_partial_direct_stream_failure_persists_partial_state_without_retrieval(
    session_factory: sessionmaker[Session],
) -> None:
    class PartialFailureProvider(FakeLLMProvider):
        def stream(self, request: LLMRequest) -> Generator[LLMStreamEvent, None, None]:
            self.stream_requests.append(request)
            yield LLMStreamEvent(type="text_delta", delta="部分回答")
            raise LLMProviderError(
                "LLM_STREAM_FAILED",
                "upstream disconnected",
                retryable=True,
            )

    provider = PartialFailureProvider()
    service = _service(session_factory, MemoryOpenSearchAdapter(), provider)
    prepared, chunks = _chunks(service, _request("你好"))
    events = _events(chunks)

    assert _answer(chunks) == "部分回答"
    assert any(event["type"] == "error" for event in events)
    with session_factory() as db:
        assistant = db.get(Message, prepared.assistant_message_id)
        assert assistant is not None
        assert assistant.status.value == "failed"
        assert assistant.content == "部分回答"
        assert assistant.error is not None
        assert assistant.error["code"] == "LLM_STREAM_FAILED"
        assert assistant.model_invocation_id is not None
        invocation = db.get(ModelInvocation, assistant.model_invocation_id)
        assert invocation is not None
        assert invocation.status.value == "failed"
        assert db.scalar(select(func.count()).select_from(RetrievalTrace)) == 0


def test_agent_document_tool_skips_graph_and_generates_grounded_answer(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
) -> None:
    _, _, adapter = _ready_search_fixture(session_factory, storage)
    provider = FakeLLMProvider(
        response_text="Grounded answer [S1]",
        generate_responses=[_response(tool="retrieve_enterprise_documents")],
    )
    service = _service(session_factory, adapter, provider)

    prepared, chunks = _chunks(service, _request("Policy"))
    events = _events(chunks)

    assert _answer(chunks) == "Grounded answer [S1]"
    assert any(event["type"] == "data-tool-status" for event in events)
    assert provider.stream_requests[0].tools
    assert provider.stream_requests[0].text_format is None
    assert provider.generate_requests == []
    assert len(provider.stream_requests) == 2
    with session_factory() as db:
        assistant = db.get(Message, prepared.assistant_message_id)
        assert assistant is not None
        assert assistant.retrieval_trace_id is not None
        trace = db.get(RetrievalTrace, assistant.retrieval_trace_id)
        assert trace is not None
        assert trace.config_snapshot["graph_requested"] is False
        assert trace.graph_query_trace_id is None
        assert assistant.metadata_json["selected_tool"] == ("retrieve_enterprise_documents")


def test_agent_mixed_output_prefers_tool_without_leaking_decision_text(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
) -> None:
    _, _, adapter = _ready_search_fixture(session_factory, storage)
    provider = FakeLLMProvider(
        response_text="Grounded answer [S1]",
        generate_responses=[
            _response(
                "这段决策文本不应展示给用户。",
                tool="retrieve_enterprise_documents",
            )
        ],
    )
    service = _service(session_factory, adapter, provider)

    prepared, chunks = _chunks(service, _request("Policy"))
    events = _events(chunks)

    assert _answer(chunks) == "Grounded answer [S1]"
    assert not any(event["type"] == "error" for event in events)
    actions = [
        cast(dict[str, object], event["data"])["action"]
        for event in events
        if event["type"] == "data-agent-status"
    ]
    assert actions == ["documents"]
    with session_factory() as db:
        assistant = db.get(Message, prepared.assistant_message_id)
        assert assistant is not None
        assert assistant.status.value == "completed"
        assert assistant.content == "Grounded answer [S1]"
        assert assistant.metadata_json["action"] == "documents"


def test_agent_refuses_when_retrieval_has_no_sources(
    session_factory: sessionmaker[Session],
) -> None:
    provider = FakeLLMProvider(
        generate_responses=[_response(tool="retrieve_enterprise_documents", query="missing")],
    )
    service = _service(session_factory, MemoryOpenSearchAdapter(), provider)

    prepared, chunks = _chunks(service, _request("不存在的企业信息"))

    assert prepared.sources == []
    assert prepared.generation_request is None
    assert _answer(chunks) == "在当前企业知识库中没有找到足够的信息来回答这个问题。"
    assert len(provider.stream_requests) == 1
    with session_factory() as db:
        purposes = list(db.scalars(select(ModelInvocation.purpose)))
        assistant = db.get(Message, prepared.assistant_message_id)
        assert assistant is not None
        assert assistant.query_rewritten == "missing"
    assert purposes.count("agent_decision") == 1


def test_agent_relationship_tool_enables_controlled_graph_retrieval(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
) -> None:
    _, version_id, adapter = _ready_search_fixture(session_factory, storage)
    with session_factory() as db:
        parent = db.scalar(
            select(RetrievalNode).where(
                RetrievalNode.document_version_id == version_id,
                RetrievalNode.node_level == "parent",
            )
        )
        assert parent is not None
    graph_trace_id = _insert_graph_trace(session_factory)
    settings = _settings().model_copy(update={"graph_query_enabled": True})
    retrieval = RetrievalService(
        session_factory=session_factory,
        search_adapter=adapter,
        embedding_adapter=FakeQueryEmbeddingAdapter(),
        rerank_adapter=FakeRerankAdapter(),
        query_rewriter=IdentityQueryRewriter(),
        settings=settings,
        graph_retriever=StaticGraphRetriever(parent.id, graph_trace_id),
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    )
    provider = FakeLLMProvider(
        response_text="Relationship answer [S1]",
        generate_responses=[
            _response(
                tool="retrieve_enterprise_relationships",
                query="Who owns Policy?",
            ),
        ],
    )
    service = ChatService(
        session_factory=session_factory,
        retrieval_service=retrieval,
        provider=provider,
        settings=settings,
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    )

    prepared, chunks = _chunks(service, _request("Who owns Policy?"))

    assert _answer(chunks) == "Relationship answer [S1]"
    with session_factory() as db:
        assistant = db.get(Message, prepared.assistant_message_id)
        assert assistant is not None
        assert assistant.retrieval_trace_id is not None
        trace = db.get(RetrievalTrace, assistant.retrieval_trace_id)
        assert trace is not None
        assert trace.config_snapshot["graph_requested"] is True
        assert trace.graph_query_trace_id == graph_trace_id


def test_agent_routing_golden_dataset_is_versioned() -> None:
    dataset = GoldenDataset.load(Path(__file__).parents[2] / "evals/datasets/agent-routing-v1.json")

    assert len(dataset.samples) == 8
    assert {sample.expected_action for sample in dataset.samples} == {
        "direct",
        "documents",
        "relationships",
    }
    assert any(sample.conversation_history for sample in dataset.samples)
