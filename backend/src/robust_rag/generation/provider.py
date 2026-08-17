"""Replaceable LLM providers with a cc switch Responses implementation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx


class LLMProviderError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class LLMRequest:
    instructions: str
    input: list[dict[str, object]]
    max_output_tokens: int
    metadata: dict[str, str] = field(default_factory=dict)
    text_format: dict[str, object] | None = None


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def snapshot(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class LLMResponse:
    text: str
    response_id: str | None
    usage: LLMUsage
    finish_reason: str | None = None


@dataclass(frozen=True)
class LLMStreamEvent:
    type: Literal["text_delta", "completed"]
    delta: str = ""
    response_id: str | None = None
    usage: LLMUsage = field(default_factory=LLMUsage)
    finish_reason: str | None = None


class LLMProvider(Protocol):
    provider: str
    model: str
    endpoint: str

    def generate(self, request: LLMRequest) -> LLMResponse: ...

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]: ...


class CCSwitchResponsesProvider:
    """OpenAI Responses-compatible provider routed through local cc switch."""

    provider = "cc-switch"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        reasoning_effort: str,
        api_key: str | None = None,
        timeout_seconds: float = 120,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.endpoint = f"{base_url.rstrip('/')}/responses"
        headers = {"Accept": "text/event-stream, application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if client is not None:
            client.headers.update(headers)
            self.client = client
        else:
            self.client = httpx.Client(
                base_url=f"{base_url.rstrip('/')}/",
                headers=headers,
                timeout=timeout_seconds,
            )

    def generate(self, request: LLMRequest) -> LLMResponse:
        try:
            response = self.client.post("responses", json=self._payload(request, stream=False))
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc
        if response.status_code >= 400:
            raise _http_error(response)
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMProviderError(
                "LLM_INVALID_RESPONSE",
                "Generation service returned invalid JSON",
                retryable=False,
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise LLMProviderError(
                "LLM_INVALID_RESPONSE",
                "Generation service returned an invalid response object",
                retryable=False,
                status_code=response.status_code,
            )
        text = _extract_output_text(payload)
        if not text:
            raise LLMProviderError(
                "LLM_EMPTY_RESPONSE",
                "Generation service returned no text",
                retryable=False,
                status_code=response.status_code,
            )
        return LLMResponse(
            text=text,
            response_id=_optional_string(payload.get("id")),
            usage=_usage(payload.get("usage")),
            finish_reason=_finish_reason(payload),
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        try:
            with self.client.stream(
                "POST", "responses", json=self._payload(request, stream=True)
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise _http_error(response)
                completed = False
                for raw_data in _iter_sse_data(response.iter_lines()):
                    if raw_data == "[DONE]":
                        break
                    try:
                        event = json.loads(raw_data)
                    except json.JSONDecodeError as exc:
                        raise LLMProviderError(
                            "LLM_INVALID_STREAM",
                            "Generation service returned malformed stream data",
                            retryable=False,
                            status_code=response.status_code,
                        ) from exc
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("type")
                    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            yield LLMStreamEvent(type="text_delta", delta=delta)
                    elif event_type == "response.completed":
                        completed = True
                        result = event.get("response")
                        result_payload = result if isinstance(result, dict) else event
                        yield LLMStreamEvent(
                            type="completed",
                            response_id=_optional_string(result_payload.get("id")),
                            usage=_usage(result_payload.get("usage")),
                            finish_reason=_finish_reason(result_payload),
                        )
                    elif event_type in {"error", "response.failed", "response.incomplete"}:
                        raise _stream_error(event)
                if not completed:
                    raise LLMProviderError(
                        "LLM_STREAM_INCOMPLETE",
                        "Generation stream ended before completion",
                        retryable=True,
                        status_code=response.status_code,
                    )
        except LLMProviderError:
            raise
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc

    def _payload(self, request: LLMRequest, *, stream: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model,
            "instructions": request.instructions,
            "input": request.input,
            "max_output_tokens": request.max_output_tokens,
            "stream": stream,
        }
        if self.reasoning_effort != "none":
            payload["reasoning"] = {"effort": self.reasoning_effort}
        if request.metadata:
            payload["metadata"] = request.metadata
        if request.text_format is not None:
            payload["text"] = {"format": request.text_format}
        return payload


class FakeLLMProvider:
    """Deterministic, zero-cost provider used by tests and local contract checks."""

    provider = "fake"
    model = "fake-grounded-model"
    endpoint = "memory://responses"

    def __init__(
        self,
        *,
        response_text: str = "Answer [S1]",
        generate_text: str | None = None,
        stream_deltas: list[str] | None = None,
        failure: LLMProviderError | None = None,
    ) -> None:
        self.response_text = response_text
        self.generate_text = generate_text or response_text
        self.stream_deltas = stream_deltas
        self.failure = failure
        self.generate_requests: list[LLMRequest] = []
        self.stream_requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.generate_requests.append(request)
        if self.failure is not None:
            raise self.failure
        return LLMResponse(
            text=self.generate_text,
            response_id="resp_fake",
            usage=LLMUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            finish_reason="completed",
        )

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        self.stream_requests.append(request)
        if self.failure is not None:
            raise self.failure
        deltas = self.stream_deltas
        if deltas is None:
            midpoint = max(1, len(self.response_text) // 2)
            deltas = [self.response_text[:midpoint], self.response_text[midpoint:]]
        yield from (LLMStreamEvent(type="text_delta", delta=delta) for delta in deltas if delta)
        yield LLMStreamEvent(
            type="completed",
            response_id="resp_fake",
            usage=LLMUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            finish_reason="completed",
        )


def _iter_sse_data(lines: Iterator[str]) -> Iterator[str]:
    values: list[str] = []
    for line in lines:
        if not line:
            if values:
                yield "\n".join(values)
                values = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            values.append(line[5:].lstrip())
    if values:
        yield "\n".join(values)


def _extract_output_text(payload: dict[str, object]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str):
        return direct
    texts: list[str] = []
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text") or part.get("refusal")
            if isinstance(text, str):
                texts.append(text)
    return "".join(texts)


def _usage(value: object) -> LLMUsage:
    if not isinstance(value, dict):
        return LLMUsage()
    return LLMUsage(
        input_tokens=_optional_int(value.get("input_tokens")),
        output_tokens=_optional_int(value.get("output_tokens")),
        total_tokens=_optional_int(value.get("total_tokens")),
    )


def _finish_reason(payload: dict[str, object]) -> str | None:
    incomplete = payload.get("incomplete_details")
    if isinstance(incomplete, dict):
        return _optional_string(incomplete.get("reason"))
    status = payload.get("status")
    return status if isinstance(status, str) else None


def _http_error(response: httpx.Response) -> LLMProviderError:
    message = "Generation service request failed"
    code = "LLM_UPSTREAM_ERROR"
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            raw_code = error.get("code") or error.get("type")
            raw_message = error.get("message")
            if isinstance(raw_code, str):
                code = f"LLM_{raw_code.upper()}"
            if isinstance(raw_message, str):
                message = raw_message
    except (json.JSONDecodeError, ValueError):
        pass
    return LLMProviderError(
        code,
        message,
        retryable=response.status_code in {408, 409, 429} or response.status_code >= 500,
        status_code=response.status_code,
    )


def _stream_error(event: dict[str, object]) -> LLMProviderError:
    error = event.get("error")
    details = error if isinstance(error, dict) else event.get("response")
    detail_payload = details if isinstance(details, dict) else event
    raw_code = detail_payload.get("code") or detail_payload.get("type")
    raw_message = detail_payload.get("message") or detail_payload.get("error")
    code = f"LLM_{str(raw_code).upper()}" if raw_code else "LLM_STREAM_FAILED"
    message = str(raw_message) if isinstance(raw_message, str) else "Generation stream failed"
    return LLMProviderError(code, message, retryable=True)


def _transport_error(error: httpx.HTTPError) -> LLMProviderError:
    if isinstance(error, httpx.TimeoutException):
        return LLMProviderError("LLM_TIMEOUT", "Generation service timed out", retryable=True)
    return LLMProviderError(
        "LLM_UNAVAILABLE",
        "Generation service is unavailable",
        retryable=True,
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
