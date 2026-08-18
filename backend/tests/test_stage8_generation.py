import json
import pickle
import uuid
from typing import cast
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from robust_rag.core.settings import Settings
from robust_rag.db.enums import MessageStatus, ModelInvocationStatus
from robust_rag.db.models import Citation, Message, ModelInvocation, RetrievalTrace
from robust_rag.generation.prompts import GROUNDED_INSTRUCTIONS
from robust_rag.generation.provider import (
    FakeLLMProvider,
    LLMProviderError,
    LLMRequest,
    ResponsesAPIProvider,
)
from robust_rag.generation.schemas import ChatRequest
from robust_rag.generation.service import ChatService, build_llm_provider, get_chat_service
from robust_rag.indexing.opensearch import MemoryOpenSearchAdapter
from robust_rag.storage.local import LocalFileStorage
from tests.test_stage7_retrieval import (
    _ready_search_fixture,
    _retrieval_service,
    _retrieval_settings,
)


def _chat_settings() -> Settings:
    return _retrieval_settings().model_copy(
        update={
            "llm_max_retries": 0,
            "llm_retry_base_seconds": 0,
            "citation_excerpt_max_chars": 100,
        }
    )


def _chat_service(
    session_factory: sessionmaker[Session],
    search_adapter: MemoryOpenSearchAdapter,
    provider: FakeLLMProvider,
) -> ChatService:
    return ChatService(
        session_factory=session_factory,
        retrieval_service=_retrieval_service(session_factory, search_adapter),
        provider=provider,
        settings=_chat_settings(),
        sleeper=lambda _delay: None,
        jitter=lambda: 0,
    )


def _request(query: str, conversation_id: uuid.UUID | None = None) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "conversation_id": str(conversation_id) if conversation_id else None,
            "messages": [{"role": "user", "parts": [{"type": "text", "text": query}]}],
            "mode": "hybrid_rerank",
        }
    )


def _events(chunks: list[str]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: {"):
                events.append(json.loads(line[6:]))
    return events


def _log_records(logger: Mock) -> list[dict[str, object]]:
    return [
        {
            "event": call.args[0],
            "level": call[0],
            **call.kwargs,
        }
        for call in logger.mock_calls
    ]


def test_responses_api_provider_non_stream_and_stream_contract() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["stream"]:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=(
                    'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n'
                    'data: {"type":"response.output_text.delta","delta":"Hello "}\n\n'
                    'data: {"type":"response.output_text.delta","delta":"world"}\n\n'
                    "data: "
                    '{"type":"response.completed","response":{"id":"resp_1",'
                    '"status":"completed","usage":{"input_tokens":9,'
                    '"output_tokens":2,"total_tokens":11}}}\n\n'
                    "data: [DONE]\n\n"
                ),
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "Hello"}]}
                ],
                "usage": {"input_tokens": 9, "output_tokens": 1, "total_tokens": 10},
            },
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://llm.example.test/v1/"
    )
    provider = ResponsesAPIProvider(
        base_url="https://llm.example.test/v1",
        model="test-responses-model",
        reasoning_effort="medium",
        api_key="secret",
        client=client,
    )
    request = LLMRequest(
        instructions="Grounded",
        input=[{"role": "user", "content": "Question"}],
        max_output_tokens=200,
        metadata={"purpose": "test"},
    )

    response = provider.generate(request)
    streamed = list(provider.stream(request))

    assert response.text == "Hello"
    assert response.usage.total_tokens == 10
    assert [event.delta for event in streamed if event.type == "text_delta"] == [
        "Hello ",
        "world",
    ]
    assert streamed[-1].usage.total_tokens == 11
    assert requests[0] == {
        "model": "test-responses-model",
        "instructions": "Grounded",
        "input": [{"role": "user", "content": "Question"}],
        "max_output_tokens": 200,
        "stream": False,
        "reasoning": {"effort": "medium"},
        "metadata": {"purpose": "test"},
    }
    assert requests[1]["stream"] is True
    assert client.headers["authorization"] == "Bearer secret"
    assert provider.endpoint == "https://llm.example.test/v1/responses"


def test_responses_api_provider_exposes_retryable_http_and_stream_errors() -> None:
    unavailable = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                503, json={"error": {"code": "route_unavailable", "message": "offline"}}
            )
        ),
        base_url="https://llm.example.test/v1/",
    )
    provider = ResponsesAPIProvider(
        base_url="https://llm.example.test/v1",
        model="test-responses-model",
        reasoning_effort="none",
        api_key="secret",
        client=unavailable,
    )
    request = LLMRequest(instructions="x", input=[], max_output_tokens=1)
    with pytest.raises(LLMProviderError) as captured:
        provider.generate(request)
    assert captured.value.code == "LLM_ROUTE_UNAVAILABLE"
    assert captured.value.retryable is True
    assert captured.value.status_code == 503

    failed_stream = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text='data: {"type":"error","error":{"code":"bad","message":"failed"}}\n\n',
            )
        ),
        base_url="http://cc/v1/",
    )
    provider.client = failed_stream
    with pytest.raises(LLMProviderError, match="failed"):
        list(provider.stream(request))


def test_responses_api_provider_requires_api_key() -> None:
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        ResponsesAPIProvider(
            base_url="https://llm.example.test/v1",
            model="test-responses-model",
            reasoning_effort="none",
            api_key=" ",
        )

    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        build_llm_provider(Settings(_env_file=None))


def test_llm_provider_error_is_pickle_safe_for_celery_workers() -> None:
    original = LLMProviderError(
        "LLM_FORBIDDEN",
        "Access denied",
        retryable=False,
        status_code=403,
    )

    restored = pickle.loads(pickle.dumps(original))

    assert restored.code == "LLM_FORBIDDEN"
    assert restored.message == "Access denied"
    assert restored.retryable is False
    assert restored.status_code == 403


def test_grounded_chat_stream_persists_citations_usage_and_trace(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, search_adapter = _ready_search_fixture(session_factory, storage)
    provider = FakeLLMProvider(
        response_text="The policy is documented here [S1].",
        stream_deltas=["The policy is ", "documented here [S1]."],
    )
    service = _chat_service(session_factory, search_adapter, provider)
    prepared = service.prepare(_request("Policy"))
    logger = Mock()
    monkeypatch.setattr("robust_rag.generation.service._logger", logger)
    chunks = list(service.stream(prepared))
    logs = _log_records(logger)
    events = _events(chunks)

    assert chunks[-1] == "data: [DONE]\n\n"
    assert [event["type"] for event in events][:4] == [
        "start",
        "data-conversation",
        "data-retrieval-status",
        "data-source",
    ]
    assert any(event["type"] == "data-usage" for event in events)
    assert provider.stream_requests[0].instructions == GROUNDED_INSTRUCTIONS
    assert "<S1 " in str(provider.stream_requests[0].input[0]["content"])
    started_log = next(log for log in logs if log["event"] == "llm_request_started")
    succeeded_log = next(log for log in logs if log["event"] == "llm_request_succeeded")
    assert started_log["purpose"] == "rag_generation"
    assert started_log["invocation_id"] == str(prepared.invocation_id)
    assert succeeded_log["input_tokens"] == 10
    assert succeeded_log["output_tokens"] == 5
    assert "question" not in started_log
    assert "input" not in started_log

    with session_factory() as db:
        assistant = db.get(Message, prepared.assistant_message_id)
        assert assistant is not None
        assert assistant.status is MessageStatus.COMPLETED
        assert assistant.content.endswith("[S1].")
        assert db.scalar(select(Citation).where(Citation.message_id == assistant.id)) is not None
        invocation = db.get(ModelInvocation, prepared.invocation_id)
        assert invocation is not None
        assert invocation.status is ModelInvocationStatus.SUCCEEDED
        assert invocation.input_tokens == 10
        assert invocation.output_tokens == 5

    cast(FastAPI, client.app).dependency_overrides[get_chat_service] = lambda: service
    response = client.post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "parts": [{"type": "text", "text": "Policy"}]}]},
    )
    assert response.status_code == 200
    assert response.headers["x-vercel-ai-ui-message-stream"] == "v1"
    assert '"type":"text-delta"' in response.text
    assert response.text.endswith("data: [DONE]\n\n")

    conversation = client.get(f"/api/v1/conversations/{prepared.conversation_id}")
    assert conversation.status_code == 200
    assert len(conversation.json()["messages"]) == 2
    trace = client.get(f"/api/v1/messages/{prepared.assistant_message_id}/trace")
    assert trace.status_code == 200
    assert trace.json()["retrieval"]["context_node_ids"]
    assert trace.json()["generation"]["model"] == "fake-grounded-model"
    assert trace.json()["citations"][0]["source_label"] == "S1"
    assert client.get("/api/v1/conversations").json()


def test_no_context_refuses_without_calling_model_and_conversation_can_be_deleted(
    session_factory: sessionmaker[Session],
    client: TestClient,
) -> None:
    provider = FakeLLMProvider()
    service = _chat_service(session_factory, MemoryOpenSearchAdapter(), provider)
    prepared = service.prepare(_request("未知公司的内部政策是什么\uff1f"))
    chunks = list(service.stream(prepared))

    assert "没有找到足够的信息" in "".join(chunks)
    assert provider.stream_requests == []
    assert prepared.invocation_id is None
    with session_factory() as db:
        assistant = db.get(Message, prepared.assistant_message_id)
        assert assistant is not None
        assert assistant.status is MessageStatus.REFUSED
        assert assistant.metadata_json["refusal_reason"] == "no_retrieval_context"

    cast(FastAPI, client.app).dependency_overrides[get_chat_service] = lambda: service
    created = client.post("/api/v1/conversations", json={"title": "Temporary"})
    conversation_id = created.json()["id"]
    assert created.status_code == 201
    assert client.delete(f"/api/v1/conversations/{conversation_id}").status_code == 204
    assert client.get(f"/api/v1/conversations/{conversation_id}").status_code == 404
    failed = client.post(
        "/api/v1/chat",
        json={
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": "question"}],
        },
    )
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "CONVERSATION_DELETED"


def test_multiturn_rewrite_is_saved_and_generation_failure_is_explainable(
    session_factory: sessionmaker[Session],
    storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, search_adapter = _ready_search_fixture(session_factory, storage)
    provider = FakeLLMProvider(
        response_text="Initial answer [S1]", generate_text="Policy effective date"
    )
    service = _chat_service(session_factory, search_adapter, provider)
    first = service.prepare(_request("Policy"))
    list(service.stream(first))

    second = service.prepare(_request("它什么时候生效\uff1f", first.conversation_id))
    assert second.rewritten_question == "Policy effective date"
    assert len(provider.generate_requests) == 1
    with session_factory() as db:
        trace = db.get(RetrievalTrace, second.retrieval.trace_id)
        assert trace is not None
        assert trace.query_original == "它什么时候生效?"
        assert trace.query_rewritten == "Policy effective date"
        assert trace.rewrite_snapshot["history_message_count"] == 2
        assert trace.rewrite_snapshot["prompt_version"] == "stage8-conversation-rewrite-v1"

    provider.failure = LLMProviderError(
        "LLM_UNAVAILABLE",
        "LLM API offline",
        retryable=True,
        status_code=503,
    )
    service.settings = service.settings.model_copy(update={"llm_max_retries": 1})
    failed = service.prepare(_request("Policy"))
    logger = Mock()
    monkeypatch.setattr("robust_rag.generation.service._logger", logger)
    chunks = list(service.stream(failed))
    logs = _log_records(logger)
    events = _events(chunks)
    assert any(event["type"] == "error" for event in events)
    warning = next(event for event in events if event["type"] == "data-warning")
    assert cast(dict[str, object], warning["data"])["code"] == "LLM_UNAVAILABLE"
    with session_factory() as db:
        message = db.get(Message, failed.assistant_message_id)
        invocation = db.get(ModelInvocation, failed.invocation_id)
        assert message is not None and message.status is MessageStatus.FAILED
        assert invocation is not None
        assert invocation.status is ModelInvocationStatus.FAILED
        assert invocation.error is not None
        assert invocation.error["code"] == "LLM_UNAVAILABLE"

    log_events = [log["event"] for log in logs]
    assert log_events == [
        "llm_request_started",
        "llm_request_retry",
        "llm_request_failed",
    ]
    retry_log = logs[1]
    failure_log = logs[2]
    assert retry_log["attempt"] == 1
    assert retry_log["next_attempt"] == 2
    assert failure_log["attempts"] == 2
    assert failure_log["status_code"] == 503
    assert failure_log["error_code"] == "LLM_UNAVAILABLE"
    assert failure_log["partial_response"] is False
    assert "question" not in failure_log
    assert "input" not in failure_log
