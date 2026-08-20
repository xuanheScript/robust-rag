"""Replaceable LLM providers with a direct Responses API implementation."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]


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

    def __reduce__(
        self,
    ) -> tuple[Callable[..., LLMProviderError], tuple[str, str, bool, int | None]]:
        """Keep the error safe to move across Celery process boundaries."""

        return (
            _restore_llm_provider_error,
            (self.code, self.message, self.retryable, self.status_code),
        )


def _restore_llm_provider_error(
    code: str,
    message: str,
    retryable: bool,
    status_code: int | None,
) -> LLMProviderError:
    return LLMProviderError(
        code,
        message,
        retryable=retryable,
        status_code=status_code,
    )


@dataclass(frozen=True)
class LLMRequest:
    instructions: str
    input: list[dict[str, object]]
    max_output_tokens: int
    reasoning_effort: ReasoningEffort | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    text_format: dict[str, object] | None = None
    tools: list[dict[str, object]] = field(default_factory=list)
    tool_choice: str | dict[str, object] | None = None


@dataclass(frozen=True)
class LLMToolCall:
    call_id: str
    name: str
    arguments: dict[str, object]


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
    tool_calls: tuple[LLMToolCall, ...] = ()


@dataclass(frozen=True)
class LLMStreamEvent:
    type: Literal["text_delta", "completed"]
    delta: str = ""
    response_id: str | None = None
    usage: LLMUsage = field(default_factory=LLMUsage)
    finish_reason: str | None = None
    tool_calls: tuple[LLMToolCall, ...] = ()


class LLMProvider(Protocol):
    provider: str
    model: str
    endpoint: str

    def generate(self, request: LLMRequest) -> LLMResponse: ...

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]: ...


class ResponsesAPIProvider:
    """Call a configured OpenAI Responses-compatible API directly."""

    provider = "responses-api"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        reasoning_effort: str,
        api_key: str,
        timeout_seconds: float = 120,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("LLM_API_KEY is required for direct Responses API access")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self.endpoint = f"{base_url.rstrip('/')}/responses"
        headers = {
            "Accept": "text/event-stream, application/json",
            "Authorization": f"Bearer {api_key.strip()}",
        }
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
        graph_extraction = request.metadata.get("purpose") == "graph-extraction"
        started = time.perf_counter()
        if graph_extraction:
            logger.info(
                "graph_llm_provider_request_started",
                model=self.model,
                endpoint=self.endpoint,
                timeout_seconds=self.timeout_seconds,
                **_request_diagnostics(request),
            )
        try:
            response = self.client.post("responses", json=self._payload(request, stream=False))
        except httpx.HTTPError as exc:
            error = _transport_error(exc)
            if graph_extraction:
                logger.exception(
                    "graph_llm_provider_transport_failed",
                    model=self.model,
                    endpoint=self.endpoint,
                    latency_ms=_elapsed_ms(started),
                    error_code=error.code,
                    error_type=type(exc).__name__,
                    retryable=error.retryable,
                )
            raise error from exc
        response_metadata = _http_response_diagnostics(response, started)
        if graph_extraction:
            logger.info(
                "graph_llm_provider_response_received",
                model=self.model,
                endpoint=self.endpoint,
                **response_metadata,
            )
        if response.status_code >= 400:
            error = _http_error(response)
            if graph_extraction:
                logger.error(
                    "graph_llm_provider_http_failed",
                    model=self.model,
                    endpoint=self.endpoint,
                    error_code=error.code,
                    error_message=error.message,
                    retryable=error.retryable,
                    **response_metadata,
                )
            raise error
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            if graph_extraction:
                logger.exception(
                    "graph_llm_provider_invalid_json",
                    model=self.model,
                    endpoint=self.endpoint,
                    **response_metadata,
                )
            raise LLMProviderError(
                "LLM_INVALID_RESPONSE",
                "Generation service returned invalid JSON",
                retryable=False,
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            if graph_extraction:
                logger.error(
                    "graph_llm_provider_invalid_object",
                    model=self.model,
                    endpoint=self.endpoint,
                    response_json_type=type(payload).__name__,
                    **response_metadata,
                )
            raise LLMProviderError(
                "LLM_INVALID_RESPONSE",
                "Generation service returned an invalid response object",
                retryable=False,
                status_code=response.status_code,
            )
        if payload.get("status") == "incomplete":
            incomplete = payload.get("incomplete_details")
            reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
            if graph_extraction:
                logger.error(
                    "graph_llm_provider_incomplete_response",
                    model=self.model,
                    endpoint=self.endpoint,
                    **response_metadata,
                    **_response_structure(payload),
                )
            raise LLMProviderError(
                "LLM_INCOMPLETE_RESPONSE",
                f"Generation service returned an incomplete response ({reason or 'unknown'})",
                retryable=False,
                status_code=response.status_code,
            )
        text = _extract_output_text(payload)
        tool_calls = _extract_tool_calls(payload)
        if not text and not tool_calls:
            if graph_extraction:
                logger.error(
                    "graph_llm_provider_empty_response",
                    model=self.model,
                    endpoint=self.endpoint,
                    **response_metadata,
                    **_response_structure(payload),
                )
            raise LLMProviderError(
                "LLM_EMPTY_RESPONSE",
                "Generation service returned neither text nor tool calls",
                retryable=False,
                status_code=response.status_code,
            )
        result = LLMResponse(
            text=text,
            response_id=_optional_string(payload.get("id")),
            usage=_usage(payload.get("usage")),
            finish_reason=_finish_reason(payload),
            tool_calls=tool_calls,
        )
        if graph_extraction:
            logger.info(
                "graph_llm_provider_response_parsed",
                model=self.model,
                endpoint=self.endpoint,
                finish_reason=result.finish_reason,
                output_characters=len(result.text),
                output_sha256=_sha256_text(result.text),
                tool_call_count=len(result.tool_calls),
                **response_metadata,
                **_response_structure(payload),
            )
        return result

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        try:
            with self.client.stream(
                "POST", "responses", json=self._payload(request, stream=True)
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise _http_error(response)
                completed = False
                emitted_text = False
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
                            emitted_text = True
                            yield LLMStreamEvent(type="text_delta", delta=delta)
                    elif event_type == "response.completed":
                        completed = True
                        result = event.get("response")
                        result_payload = result if isinstance(result, dict) else event
                        final_text = _extract_output_text(result_payload)
                        if final_text and not emitted_text:
                            emitted_text = True
                            yield LLMStreamEvent(type="text_delta", delta=final_text)
                        yield LLMStreamEvent(
                            type="completed",
                            response_id=_optional_string(result_payload.get("id")),
                            usage=_usage(result_payload.get("usage")),
                            finish_reason=_finish_reason(result_payload),
                            tool_calls=_extract_tool_calls(result_payload),
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
        reasoning_effort = request.reasoning_effort or self.reasoning_effort
        if reasoning_effort != "none":
            payload["reasoning"] = {"effort": reasoning_effort}
        if request.metadata:
            payload["metadata"] = request.metadata
        if request.text_format is not None:
            payload["text"] = {"format": request.text_format}
        if request.tools:
            payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
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
        generate_responses: list[LLMResponse] | None = None,
    ) -> None:
        self.response_text = response_text
        self.generate_text = generate_text or response_text
        self.stream_deltas = stream_deltas
        self.failure = failure
        self.generate_responses = list(generate_responses or [])
        self.generate_requests: list[LLMRequest] = []
        self.stream_requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.generate_requests.append(request)
        if self.failure is not None:
            raise self.failure
        if self.generate_responses:
            return self.generate_responses.pop(0)
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
        if request.metadata.get("purpose") == "agent_decision" and self.generate_responses:
            response = self.generate_responses.pop(0)
            if response.text:
                deltas = self.stream_deltas
                if deltas is None:
                    midpoint = max(1, len(response.text) // 2)
                    deltas = [response.text[:midpoint], response.text[midpoint:]]
                yield from (
                    LLMStreamEvent(type="text_delta", delta=delta) for delta in deltas if delta
                )
            yield LLMStreamEvent(
                type="completed",
                response_id=response.response_id,
                usage=response.usage,
                finish_reason=response.finish_reason,
                tool_calls=response.tool_calls,
            )
            return
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


def _request_diagnostics(request: LLMRequest) -> dict[str, object]:
    try:
        serialized_input = json.dumps(
            request.input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        serialized_input = ""
    text_format = request.text_format or {}
    return {
        "purpose": request.metadata.get("purpose"),
        "input_message_count": len(request.input),
        "input_characters": len(serialized_input),
        "input_sha256": _sha256_text(serialized_input) if serialized_input else None,
        "max_output_tokens": request.max_output_tokens,
        "text_format_type": text_format.get("type"),
        "text_format_name": text_format.get("name"),
        "text_format_strict": text_format.get("strict"),
        "tool_count": len(request.tools),
    }


def _http_response_diagnostics(response: httpx.Response, started: float) -> dict[str, object]:
    upstream_request_id = next(
        (
            response.headers.get(name)
            for name in ("x-request-id", "request-id", "x-correlation-id", "cf-ray")
            if response.headers.get(name)
        ),
        None,
    )
    return {
        "http_status": response.status_code,
        "latency_ms": _elapsed_ms(started),
        "content_type": response.headers.get("content-type"),
        "upstream_request_id": upstream_request_id,
        "response_bytes": len(response.content),
        "response_sha256": hashlib.sha256(response.content).hexdigest(),
    }


def _response_structure(payload: dict[str, object]) -> dict[str, object]:
    output = payload.get("output")
    output_items = output if isinstance(output, list) else []
    output_types: list[str] = []
    output_item_keys: list[list[str]] = []
    content_types: list[str] = []
    content_keys: list[list[str]] = []
    for item in output_items:
        if not isinstance(item, dict):
            output_types.append(type(item).__name__)
            continue
        output_types.append(str(item.get("type") or "unknown"))
        output_item_keys.append(sorted(str(key) for key in item))
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                content_types.append(type(part).__name__)
                continue
            content_types.append(str(part.get("type") or "unknown"))
            content_keys.append(sorted(str(key) for key in part))
    incomplete = payload.get("incomplete_details")
    incomplete_reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
    choices = payload.get("choices")
    choice_items = choices if isinstance(choices, list) else []
    choice_keys: list[list[str]] = []
    choice_message_keys: list[list[str]] = []
    choice_content_types: list[str] = []
    for choice in choice_items:
        if not isinstance(choice, dict):
            choice_keys.append([type(choice).__name__])
            continue
        choice_keys.append(sorted(str(key) for key in choice))
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        choice_message_keys.append(sorted(str(key) for key in message))
        choice_content_types.append(type(message.get("content")).__name__)
    usage = _usage(payload.get("usage"))
    return {
        "response_id": _optional_string(payload.get("id")),
        "response_status": _optional_string(payload.get("status")),
        "incomplete_reason": _optional_string(incomplete_reason),
        "response_top_level_keys": sorted(str(key) for key in payload),
        "direct_output_text_type": type(payload.get("output_text")).__name__,
        "output_container_type": type(output).__name__,
        "output_count": len(output_items),
        "output_types": output_types,
        "output_item_keys": output_item_keys,
        "content_types": content_types,
        "content_keys": content_keys,
        "choice_count": len(choice_items),
        "choice_keys": choice_keys,
        "choice_message_keys": choice_message_keys,
        "choice_content_types": choice_content_types,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def _extract_tool_calls(payload: dict[str, object]) -> tuple[LLMToolCall, ...]:
    output = payload.get("output")
    if not isinstance(output, list):
        return ()
    calls: list[LLMToolCall] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = item.get("call_id") or item.get("id")
        name = item.get("name")
        raw_arguments = item.get("arguments")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise LLMProviderError(
                "LLM_INVALID_TOOL_CALL",
                "Generation service returned an invalid tool call",
                retryable=False,
            )
        if isinstance(raw_arguments, str):
            try:
                parsed_arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise LLMProviderError(
                    "LLM_INVALID_TOOL_CALL",
                    "Generation service returned invalid tool arguments",
                    retryable=False,
                ) from exc
        else:
            parsed_arguments = raw_arguments
        if not isinstance(parsed_arguments, dict):
            raise LLMProviderError(
                "LLM_INVALID_TOOL_CALL",
                "Generation service returned non-object tool arguments",
                retryable=False,
            )
        calls.append(LLMToolCall(call_id=call_id, name=name, arguments=dict(parsed_arguments)))
    return tuple(calls)


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
