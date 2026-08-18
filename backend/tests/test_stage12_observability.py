from __future__ import annotations

from robust_rag.core.logging import redact_sensitive_log_fields
from robust_rag.core.observability import (
    ObservabilityService,
    sanitize_payload,
    trace_id_from_seed,
)
from robust_rag.core.settings import Settings


class FakeObservation:
    id = "observation-id"

    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.scores: list[dict[str, object]] = []

    def update(self, **kwargs: object) -> None:
        self.updates.append(kwargs)

    def score(self, **kwargs: object) -> None:
        self.scores.append(kwargs)

    def score_trace(self, **kwargs: object) -> None:
        self.scores.append(kwargs)


class FakeObservationManager:
    def __init__(self, observation: FakeObservation) -> None:
        self.observation = observation
        self.exited = False

    def __enter__(self) -> FakeObservation:
        return self.observation

    def __exit__(self, *_: object) -> None:
        self.exited = True


class FakeLangfuse:
    def __init__(self) -> None:
        self.observation = FakeObservation()
        self.manager = FakeObservationManager(self.observation)
        self.started: list[dict[str, object]] = []
        self.flushed = False

    def start_as_current_observation(self, **kwargs: object) -> FakeObservationManager:
        self.started.append(kwargs)
        return self.manager

    def auth_check(self) -> bool:
        return True

    def flush(self) -> None:
        self.flushed = True

    def shutdown(self) -> None:
        self.flushed = True


class FailingLangfuse(FakeLangfuse):
    def start_as_current_observation(self, **kwargs: object) -> FakeObservationManager:
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
        observation.update(
            output="private answer",
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
    assert client.observation.scores[0]["name"] == "faithfulness"
    assert client.manager.exited is True


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
