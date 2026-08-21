"""Replaceable LLM providers with a direct Responses API implementation."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
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

    def provider_payload(
        self,
        *,
        model: str,
        stream: bool,
        default_reasoning_effort: str | None = None,
    ) -> dict[str, object]:
        """Return the exact credential-free Responses API JSON body."""

        payload: dict[str, object] = {
            "model": model,
            "instructions": self.instructions,
            "input": self.input,
            "max_output_tokens": self.max_output_tokens,
            "stream": stream,
        }
        reasoning_effort = self.reasoning_effort or default_reasoning_effort
        if reasoning_effort and reasoning_effort != "none":
            payload["reasoning"] = {"effort": reasoning_effort}
        if self.metadata:
            payload["metadata"] = self.metadata
        if self.text_format is not None:
            payload["text"] = {"format": self.text_format}
        if self.tools:
            payload["tools"] = self.tools
        if self.tool_choice is not None:
            payload["tool_choice"] = self.tool_choice
        return payload


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


@dataclass
class _StreamTimingDiagnostics:
    started: float
    response_headers_ms: float | None = None
    first_sse_event_ms: float | None = None
    first_sse_event_type: str | None = None
    first_reasoning_event_ms: float | None = None
    first_tool_event_ms: float | None = None
    first_text_delta_ms: float | None = None
    response_completed_ms: float | None = None
    done_ms: float | None = None
    downstream_yield_wait_ms: float = 0
    json_parse_ms: float = 0
    event_counts: Counter[str] = field(default_factory=Counter)
    http_events_ms: dict[str, float] = field(default_factory=dict)

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 3)

    def http_trace(self, event_name: str, _info: dict[str, object]) -> None:
        self.http_events_ms.setdefault(event_name, self.elapsed_ms())

    def observe_sse(self, event_type: str) -> None:
        elapsed_ms = self.elapsed_ms()
        self.event_counts[event_type] += 1
        if self.first_sse_event_ms is None:
            self.first_sse_event_ms = elapsed_ms
            self.first_sse_event_type = event_type
        if event_type.startswith("response.reasoning") and self.first_reasoning_event_ms is None:
            self.first_reasoning_event_ms = elapsed_ms
        if "function_call" in event_type and self.first_tool_event_ms is None:
            self.first_tool_event_ms = elapsed_ms
        if event_type == "response.completed":
            self.response_completed_ms = elapsed_ms

    def observe_text_delta(self) -> bool:
        if self.first_text_delta_ms is not None:
            return False
        self.first_text_delta_ms = self.elapsed_ms()
        return True

    def add_downstream_wait(self, started: float) -> None:
        self.downstream_yield_wait_ms += (time.perf_counter() - started) * 1000

    def snapshot(self) -> dict[str, object]:
        total_ms = self.elapsed_ms()
        return {
            "total_latency_ms": total_ms,
            "response_headers_ms": self.response_headers_ms,
            "first_sse_event_ms": self.first_sse_event_ms,
            "first_sse_event_type": self.first_sse_event_type,
            "first_reasoning_event_ms": self.first_reasoning_event_ms,
            "first_tool_event_ms": self.first_tool_event_ms,
            "first_text_delta_ms": self.first_text_delta_ms,
            "response_completed_ms": self.response_completed_ms,
            "done_ms": self.done_ms,
            "downstream_yield_wait_ms": round(self.downstream_yield_wait_ms, 3),
            "provider_active_ms": round(max(0, total_ms - self.downstream_yield_wait_ms), 3),
            "json_parse_ms": round(self.json_parse_ms, 3),
            "sse_event_count": sum(self.event_counts.values()),
            "reasoning_event_count": sum(
                count
                for event_type, count in self.event_counts.items()
                if event_type.startswith("response.reasoning")
            ),
            "tool_event_count": sum(
                count
                for event_type, count in self.event_counts.items()
                if "function_call" in event_type
            ),
            "text_event_count": sum(
                count
                for event_type, count in self.event_counts.items()
                if event_type in {"response.output_text.delta", "response.refusal.delta"}
            ),
            "sse_event_types": sorted(self.event_counts),
            **self._http_phase_snapshot(),
        }

    def _http_phase_snapshot(self) -> dict[str, object]:
        return {
            "connection_reused": (
                not self._has_http_event("connect_tcp.started") if self.http_events_ms else None
            ),
            "request_send_started_ms": self._http_event("send_request_headers.started"),
            "request_headers_sent_ms": self._http_event("send_request_headers.complete"),
            "request_body_sent_ms": self._http_event("send_request_body.complete"),
            "connect_tcp_ms": self._http_duration("connect_tcp"),
            "tls_ms": self._http_duration("start_tls"),
            "upstream_headers_wait_ms": self._http_duration("receive_response_headers"),
        }

    def _has_http_event(self, suffix: str) -> bool:
        return any(name.endswith(suffix) for name in self.http_events_ms)

    def _http_event(self, suffix: str) -> float | None:
        return next(
            (elapsed for name, elapsed in self.http_events_ms.items() if name.endswith(suffix)),
            None,
        )

    def _http_duration(self, phase: str) -> float | None:
        started = self._http_event(f"{phase}.started")
        completed = self._http_event(f"{phase}.complete")
        if started is None or completed is None:
            return None
        return round(max(0, completed - started), 3)


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
        diagnostics = _StreamTimingDiagnostics(started=time.perf_counter())
        effective_reasoning = request.reasoning_effort or self.reasoning_effort
        log_context = {
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "timeout_seconds": self.timeout_seconds,
            "requested_reasoning_effort": request.reasoning_effort,
            "effective_reasoning_effort": effective_reasoning,
            "reasoning_parameter_sent": effective_reasoning != "none",
            **_request_diagnostics(request),
        }
        logger.info("llm_provider_stream_started", **log_context)
        response_status: int | None = None
        upstream_request_id: str | None = None
        response_id: str | None = None
        usage_diagnostics: dict[str, object] = {}
        try:
            with self.client.stream(
                "POST",
                "responses",
                json=self._payload(request, stream=True),
                extensions={"trace": diagnostics.http_trace},
            ) as response:
                diagnostics.response_headers_ms = diagnostics.elapsed_ms()
                response_status = response.status_code
                upstream_request_id = _upstream_request_id(response)
                logger.info(
                    "llm_provider_response_headers_received",
                    **log_context,
                    **diagnostics.snapshot(),
                    http_status=response.status_code,
                    content_type=response.headers.get("content-type"),
                    upstream_request_id=upstream_request_id,
                )
                if response.status_code >= 400:
                    response.read()
                    raise _http_error(response)
                completed = False
                emitted_text = False
                for raw_data in _iter_sse_data(response.iter_lines()):
                    if raw_data == "[DONE]":
                        diagnostics.done_ms = diagnostics.elapsed_ms()
                        break
                    parse_started = time.perf_counter()
                    try:
                        event = json.loads(raw_data)
                    except json.JSONDecodeError as exc:
                        diagnostics.json_parse_ms += (time.perf_counter() - parse_started) * 1000
                        raise LLMProviderError(
                            "LLM_INVALID_STREAM",
                            "Generation service returned malformed stream data",
                            retryable=False,
                            status_code=response.status_code,
                        ) from exc
                    diagnostics.json_parse_ms += (time.perf_counter() - parse_started) * 1000
                    if not isinstance(event, dict):
                        diagnostics.observe_sse("non_object")
                        continue
                    raw_event_type = event.get("type")
                    event_type = raw_event_type if isinstance(raw_event_type, str) else "unknown"
                    first_sse = diagnostics.first_sse_event_ms is None
                    first_reasoning = (
                        event_type.startswith("response.reasoning")
                        and diagnostics.first_reasoning_event_ms is None
                    )
                    first_tool = (
                        "function_call" in event_type and diagnostics.first_tool_event_ms is None
                    )
                    diagnostics.observe_sse(event_type)
                    if first_sse:
                        logger.info(
                            "llm_provider_first_sse_event",
                            **log_context,
                            **diagnostics.snapshot(),
                            http_status=response.status_code,
                            upstream_request_id=upstream_request_id,
                            sse_event_type=event_type,
                        )
                    if first_reasoning:
                        logger.info(
                            "llm_provider_first_reasoning_event",
                            **log_context,
                            first_reasoning_event_ms=diagnostics.first_reasoning_event_ms,
                            upstream_request_id=upstream_request_id,
                        )
                    if first_tool:
                        logger.info(
                            "llm_provider_first_tool_event",
                            **log_context,
                            first_tool_event_ms=diagnostics.first_tool_event_ms,
                            upstream_request_id=upstream_request_id,
                            sse_event_type=event_type,
                        )
                    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            emitted_text = True
                            if diagnostics.observe_text_delta():
                                logger.info(
                                    "llm_provider_first_text_delta",
                                    **log_context,
                                    first_text_delta_ms=diagnostics.first_text_delta_ms,
                                    upstream_request_id=upstream_request_id,
                                )
                            downstream_started = time.perf_counter()
                            yield LLMStreamEvent(type="text_delta", delta=delta)
                            diagnostics.add_downstream_wait(downstream_started)
                    elif event_type == "response.completed":
                        completed = True
                        result = event.get("response")
                        result_payload = result if isinstance(result, dict) else event
                        response_id = _optional_string(result_payload.get("id"))
                        usage_diagnostics = _stream_usage_diagnostics(result_payload.get("usage"))
                        final_text = _extract_output_text(result_payload)
                        if final_text and not emitted_text:
                            emitted_text = True
                            if diagnostics.observe_text_delta():
                                logger.info(
                                    "llm_provider_first_text_delta",
                                    **log_context,
                                    first_text_delta_ms=diagnostics.first_text_delta_ms,
                                    upstream_request_id=upstream_request_id,
                                    emitted_from_completed_event=True,
                                )
                            downstream_started = time.perf_counter()
                            yield LLMStreamEvent(type="text_delta", delta=final_text)
                            diagnostics.add_downstream_wait(downstream_started)
                        downstream_started = time.perf_counter()
                        yield LLMStreamEvent(
                            type="completed",
                            response_id=response_id,
                            usage=_usage(result_payload.get("usage")),
                            finish_reason=_finish_reason(result_payload),
                            tool_calls=_extract_tool_calls(result_payload),
                        )
                        diagnostics.add_downstream_wait(downstream_started)
                    elif event_type in {"error", "response.failed", "response.incomplete"}:
                        raise _stream_error(event)
                if not completed:
                    raise LLMProviderError(
                        "LLM_STREAM_INCOMPLETE",
                        "Generation stream ended before completion",
                        retryable=True,
                        status_code=response.status_code,
                    )
                logger.info(
                    "llm_provider_stream_completed",
                    **log_context,
                    **diagnostics.snapshot(),
                    **usage_diagnostics,
                    http_status=response.status_code,
                    upstream_request_id=upstream_request_id,
                    response_id=response_id,
                )
        except LLMProviderError as exc:
            logger.error(
                "llm_provider_stream_failed",
                **log_context,
                **diagnostics.snapshot(),
                http_status=response_status,
                upstream_request_id=upstream_request_id,
                error_code=exc.code,
                status_code=exc.status_code,
                retryable=exc.retryable,
            )
            raise
        except httpx.HTTPError as exc:
            error = _transport_error(exc)
            logger.error(
                "llm_provider_stream_failed",
                **log_context,
                **diagnostics.snapshot(),
                http_status=response_status,
                upstream_request_id=upstream_request_id,
                error_code=error.code,
                status_code=error.status_code,
                retryable=error.retryable,
                transport_error_type=type(exc).__name__,
            )
            raise error from exc

    def _payload(self, request: LLMRequest, *, stream: bool) -> dict[str, object]:
        return request.provider_payload(
            model=self.model,
            stream=stream,
            default_reasoning_effort=self.reasoning_effort,
        )


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
        self._generate_text_explicit = generate_text is not None
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
        if request.metadata.get("purpose") == "query_rewrite" and not self._generate_text_explicit:
            latest = request.input[-1].get("content", "") if request.input else ""
            query = str(latest)
            text = json.dumps(
                {
                    "standalone_query": query,
                    "semantic_query": query,
                    "lexical_queries": [],
                    "entities": [],
                    "answer_facets": [],
                    "filters": {},
                },
                ensure_ascii=False,
            )
        else:
            text = self.generate_text
        return LLMResponse(
            text=text,
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
    return {
        "http_status": response.status_code,
        "latency_ms": _elapsed_ms(started),
        "content_type": response.headers.get("content-type"),
        "upstream_request_id": _upstream_request_id(response),
        "response_bytes": len(response.content),
        "response_sha256": hashlib.sha256(response.content).hexdigest(),
    }


def _upstream_request_id(response: httpx.Response) -> str | None:
    return next(
        (
            response.headers.get(name)
            for name in ("x-request-id", "request-id", "x-correlation-id", "cf-ray")
            if response.headers.get(name)
        ),
        None,
    )


def _stream_usage_diagnostics(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    input_details = value.get("input_tokens_details")
    output_details = value.get("output_tokens_details")
    return {
        "input_tokens": _optional_int(value.get("input_tokens")),
        "output_tokens": _optional_int(value.get("output_tokens")),
        "total_tokens": _optional_int(value.get("total_tokens")),
        "cached_input_tokens": (
            _optional_int(input_details.get("cached_tokens"))
            if isinstance(input_details, dict)
            else None
        ),
        "reasoning_tokens": (
            _optional_int(output_details.get("reasoning_tokens"))
            if isinstance(output_details, dict)
            else None
        ),
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
