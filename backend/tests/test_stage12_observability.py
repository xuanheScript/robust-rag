from __future__ import annotations

from contextvars import copy_context
from datetime import UTC, datetime

from robust_rag.core.logging import redact_sensitive_log_fields
from robust_rag.core.observability import (
    ObservabilityService,
    sanitize_payload,
    trace_id_from_seed,
)
from robust_rag.core.settings import Settings


class FakeObservation:
    def __init__(self, observation_id: str = "observation-id") -> None:
        self.id = observation_id
        self.updates: list[dict[str, object]] = []
        self.scores: list[dict[str, object]] = []
        self.ended = False

    def update(self, **kwargs: object) -> None:
        self.updates.append(kwargs)

    def score(self, **kwargs: object) -> None:
        self.scores.append(kwargs)

    def score_trace(self, **kwargs: object) -> None:
        self.scores.append(kwargs)

    def end(self) -> None:
        self.ended = True


class FakeLangfuse:
    def __init__(self) -> None:
        self.observation = FakeObservation()
        self.observations: list[FakeObservation] = []
        self.started: list[dict[str, object]] = []
        self.flushed = False

    def start_observation(self, **kwargs: object) -> FakeObservation:
        self.started.append(kwargs)
        self.observation = FakeObservation(f"{len(self.started):016x}")
        self.observations.append(self.observation)
        return self.observation

    def auth_check(self) -> bool:
        return True

    def flush(self) -> None:
        self.flushed = True

    def shutdown(self) -> None:
        self.flushed = True


class FailingLangfuse(FakeLangfuse):
    def start_observation(self, **kwargs: object) -> FakeObservation:
        raise TimeoutError


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        LANGFUSE_ENABLED=True,
        LANGFUSE_PUBLIC_KEY="pk-test",
        LANGFUSE_SECRET_KEY="sk-test",
        LANGFUSE_CAPTURE_CONTENT=False,
    )


def test_sanitize_payload_redacts_credentials_and_bounds_content() -> None:
    payload = sanitize_payload(
        {
            "api_key": "secret",
            "input_tokens": 12,
            "header": "Bearer abc.def",
            "database_url": "postgresql://user:password@localhost/db",
        }
    )

    assert payload == {
        "api_key": "[REDACTED]",
        "input_tokens": 12,
        "header": "Bearer [REDACTED]",
        "database_url": "[REDACTED]",
    }


def test_structured_log_processor_redacts_secret_fields() -> None:
    event = redact_sensitive_log_fields(
        None,
        "info",
        {"event": "provider_failed", "api_key": "secret", "input_tokens": 12},
    )

    assert event == {
        "event": "provider_failed",
        "api_key": "[REDACTED]",
        "input_tokens": 12,
    }


def test_langfuse_observation_uses_stable_trace_and_redacted_content() -> None:
    client = FakeLangfuse()
    service = ObservabilityService(_settings(), client=client)  # type: ignore[arg-type]
    trace_id = trace_id_from_seed("request-1")

    with service.observe(
        "llm.generate",
        as_type="generation",
        trace_id=trace_id,
        input={"prompt": "private enterprise text"},
        metadata={"authorization": "Bearer secret", "input_tokens": 4},
        model="test-model",
    ) as observation:
        completion_start_time = datetime.now(UTC)
        observation.update(
            output="private answer",
            completion_start_time=completion_start_time,
            usage_details={"input": 4, "output": 2},
        )
        observation.score_trace("faithfulness", 1.0)

    started = client.started[0]
    assert started["trace_context"] == {"trace_id": trace_id}
    assert started["input"] == {
        "content_redacted": True,
        "type": "object",
        "keys": ["prompt"],
    }
    assert started["metadata"] == {
        "authorization": "[REDACTED]",
        "input_tokens": 4,
    }
    assert client.observation.updates[0]["output"] == {
        "content_redacted": True,
        "type": "string",
        "characters": 14,
    }
    assert client.observation.updates[0]["completion_start_time"] == completion_start_time
    assert client.observation.scores[0]["name"] == "faithfulness"
    assert client.observation.ended is True


def test_observation_can_export_unbounded_content_for_a_single_generation() -> None:
    client = FakeLangfuse()
    service = ObservabilityService(_settings(), client=client)  # type: ignore[arg-type]
    long_context = "企业上下文" * 1000

    with service.observe(
        "llm.rag_generation",
        as_type="generation",
        input={
            "request": {
                "instructions": "完整指令",
                "input": [{"role": "user", "content": long_context}],
                "api_key": "must-not-leak",
            }
        },
        capture_content=True,
        unbounded_content=True,
    ) as observation:
        observation.update(output={"text": long_context, "response_id": "resp-1"})

    started_input = client.started[0]["input"]
    assert isinstance(started_input, dict)
    request = started_input["request"]
    assert isinstance(request, dict)
    assert request["api_key"] == "[REDACTED]"
    messages = request["input"]
    assert isinstance(messages, list)
    assert messages[0]["content"] == long_context
    output = client.observation.updates[0]["output"]
    assert isinstance(output, dict)
    assert output["text"] == long_context


def test_nested_observations_preserve_parent_child_relationship() -> None:
    client = FakeLangfuse()
    service = ObservabilityService(_settings(), client=client)  # type: ignore[arg-type]
    trace_id = trace_id_from_seed("request-with-children")

    with (
        service.observe("http.request", trace_id=trace_id),
        service.observe("retrieval.graph"),
        service.observe("graph.neo4j.query"),
    ):
        pass

    assert client.started[0]["trace_context"] == {"trace_id": trace_id}
    assert client.started[1]["trace_context"] == {
        "trace_id": trace_id,
        "parent_span_id": "0000000000000001",
    }
    assert client.started[2]["trace_context"] == {
        "trace_id": trace_id,
        "parent_span_id": "0000000000000002",
    }


def test_observation_can_end_in_a_different_streaming_context() -> None:
    client = FakeLangfuse()
    service = ObservabilityService(_settings(), client=client)  # type: ignore[arg-type]
    manager = service.observe(
        "llm.rag_generation",
        trace_id=trace_id_from_seed("streaming-request"),
    )

    entered_context = copy_context()
    exited_context = copy_context()
    entered_context.run(manager.__enter__)
    exited_context.run(manager.__exit__, None, None, None)

    assert client.observation.ended is True


def test_observation_preserves_specific_error_status() -> None:
    client = FakeLangfuse()
    service = ObservabilityService(_settings(), client=client)  # type: ignore[arg-type]

    try:
        with service.observe("llm.text_to_cypher") as observation:
            observation.update(level="ERROR", status_message="LLM_TIMEOUT")
            raise TimeoutError
    except TimeoutError:
        pass

    status_messages = [
        update.get("status_message")
        for update in client.observation.updates
        if "status_message" in update
    ]
    assert status_messages == ["LLM_TIMEOUT"]
    assert client.observation.updates[-1]["level"] == "ERROR"


def test_langfuse_failure_degrades_without_breaking_business_flow() -> None:
    service = ObservabilityService(_settings(), client=FailingLangfuse())  # type: ignore[arg-type]

    with service.observe("retrieval.search", trace_id="request-2") as observation:
        observation.update(output={"hits": 1})

    health = service.health()
    assert health.status == "degraded"
    assert health.dropped_spans == 1
    assert health.last_error == "span_start:TimeoutError"


def test_default_langfuse_configuration_is_enabled_but_degraded_without_keys() -> None:
    service = ObservabilityService(Settings(_env_file=None))

    health = service.health()

    assert health.enabled is True
    assert health.configured is False
    assert health.status == "degraded"
    assert health.capture_content is False


def test_remote_health_and_bounded_flush() -> None:
    client = FakeLangfuse()
    service = ObservabilityService(_settings(), client=client)  # type: ignore[arg-type]

    assert service.health(remote=True).status == "ok"
    assert service.flush(timeout_seconds=1) is True
    assert client.flushed is True
