"""Best-effort Langfuse tracing with local redaction and deterministic correlation."""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Literal, cast

import structlog
from langfuse import Langfuse

from robust_rag import __version__
from robust_rag.core.settings import Settings, get_settings

ObservationType = Literal[
    "span",
    "agent",
    "tool",
    "chain",
    "retriever",
    "evaluator",
    "guardrail",
    "generation",
    "embedding",
]

_current_trace_id: ContextVar[str | None] = ContextVar("robust_rag_trace_id", default=None)
_sensitive_key = re.compile(
    r"(^|_)(authorization|cookie|set_cookie|password|passwd|secret|secret_key|api_key|access_token|refresh_token|database_url|redis_url|connection_string)($|_)",
    re.IGNORECASE,
)
_bearer_value = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_credential_url = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)
_logger = structlog.get_logger(__name__)


def trace_id_from_seed(seed: str) -> str:
    """Return a stable 128-bit OpenTelemetry/Langfuse trace id."""

    value = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return value if int(value, 16) else "1".rjust(32, "0")


def current_trace_id() -> str | None:
    return _current_trace_id.get()


def bind_trace_id(trace_id: str) -> Token[str | None]:
    return _current_trace_id.set(_normalize_trace_id(trace_id))


def reset_trace_id(token: Token[str | None]) -> None:
    _current_trace_id.reset(token)


def _normalize_trace_id(value: str) -> str:
    compact = value.replace("-", "").lower()
    if (
        len(compact) == 32
        and all(character in "0123456789abcdef" for character in compact)
        and int(compact, 16)
    ):
        return compact
    return trace_id_from_seed(value)


def sanitize_payload(value: object, *, max_depth: int = 5) -> object:
    """Remove known credential fields and bound exported payload size."""

    return _sanitize(value, depth=0, max_depth=max_depth)


def _sanitize(value: object, *, depth: int, max_depth: int) -> object:
    if depth >= max_depth:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        redacted = _bearer_value.sub("Bearer [REDACTED]", value)
        redacted = _credential_url.sub(r"\g<scheme>[REDACTED]@", redacted)
        return redacted if len(redacted) <= 2048 else f"{redacted[:2048]}…[TRUNCATED]"
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 100:
                result["_truncated"] = True
                break
            key = str(raw_key)
            result[key] = (
                "[REDACTED]"
                if _sensitive_key.search(key)
                else _sanitize(item, depth=depth + 1, max_depth=max_depth)
            )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        sanitized = [_sanitize(item, depth=depth + 1, max_depth=max_depth) for item in items[:100]]
        if len(items) > 100:
            sanitized.append("[TRUNCATED]")
        return sanitized
    return _sanitize(str(value), depth=depth + 1, max_depth=max_depth)


def _content_summary(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str):
        return {"content_redacted": True, "type": "string", "characters": len(value)}
    if isinstance(value, Mapping):
        return {
            "content_redacted": True,
            "type": "object",
            "keys": sorted(str(key) for key in list(value)[:50]),
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return {"content_redacted": True, "type": "array", "items": len(value)}
    return {"content_redacted": True, "type": type(value).__name__}


@dataclass(frozen=True)
class ObservabilityHealth:
    status: Literal["ok", "degraded", "disabled", "unavailable"]
    enabled: bool
    configured: bool
    base_url: str
    sample_rate: float
    capture_content: bool
    last_trace_at: str | None
    last_remote_check_at: str | None
    last_error: str | None
    dropped_spans: int

    def snapshot(self) -> dict[str, object]:
        return {
            "status": self.status,
            "enabled": self.enabled,
            "configured": self.configured,
            "base_url": self.base_url,
            "sample_rate": self.sample_rate,
            "capture_content": self.capture_content,
            "last_trace_at": self.last_trace_at,
            "last_remote_check_at": self.last_remote_check_at,
            "last_error": self.last_error,
            "dropped_spans": self.dropped_spans,
        }


class Observation:
    """Exception-safe facade over a Langfuse observation."""

    def __init__(self, service: ObservabilityService, raw: Any | None, trace_id: str) -> None:
        self._service = service
        self._raw = raw
        self.trace_id = trace_id
        self.id = str(getattr(raw, "id", "")) or None

    def update(
        self,
        *,
        output: object | None = None,
        metadata: Mapping[str, object] | None = None,
        level: Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"] | None = None,
        status_message: str | None = None,
        usage_details: Mapping[str, int] | None = None,
        cost_details: Mapping[str, float] | None = None,
    ) -> None:
        if self._raw is None:
            return
        payload: dict[str, object] = {}
        if output is not None:
            payload["output"] = self._service.prepare_content(output)
        if metadata is not None:
            payload["metadata"] = sanitize_payload(metadata)
        if level is not None:
            payload["level"] = level
        if status_message is not None:
            payload["status_message"] = str(sanitize_payload(status_message))
        if usage_details is not None:
            payload["usage_details"] = dict(usage_details)
        if cost_details is not None:
            payload["cost_details"] = dict(cost_details)
        self._service.safe_call("observation_update", self._raw.update, **payload)

    def score(self, name: str, value: float | str, *, comment: str | None = None) -> None:
        if self._raw is None:
            return
        self._service.safe_call(
            "observation_score",
            self._raw.score,
            name=name,
            value=value,
            comment=str(sanitize_payload(comment)) if comment else None,
        )

    def score_trace(self, name: str, value: float | str, *, comment: str | None = None) -> None:
        if self._raw is None:
            return
        self._service.safe_call(
            "trace_score",
            self._raw.score_trace,
            name=name,
            value=value,
            comment=str(sanitize_payload(comment)) if comment else None,
        )


class ObservabilityService:
    """Own the optional Langfuse client without making telemetry a business dependency."""

    def __init__(self, settings: Settings, *, client: Langfuse | None = None) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._last_trace_at: datetime | None = None
        self._last_remote_check_at: datetime | None = None
        self._last_error: str | None = None
        self._dropped_spans = 0
        self._configured = bool(settings.langfuse_public_key and settings.langfuse_secret_key)
        self._client = client
        if self._client is None and settings.langfuse_enabled and self._configured:
            try:
                assert settings.langfuse_public_key is not None
                assert settings.langfuse_secret_key is not None
                self._client = Langfuse(
                    public_key=settings.langfuse_public_key.get_secret_value(),
                    secret_key=settings.langfuse_secret_key.get_secret_value(),
                    base_url=settings.langfuse_base_url,
                    timeout=settings.langfuse_timeout_seconds,
                    tracing_enabled=True,
                    sample_rate=settings.langfuse_sample_rate,
                    flush_at=settings.langfuse_flush_at,
                    flush_interval=settings.langfuse_flush_interval_seconds,
                    environment=settings.app_env,
                    release=__version__,
                    mask=self._mask,
                )
            except Exception as exc:
                self._record_failure("client_initialization", exc)

    def prepare_content(self, value: object) -> object:
        if self.settings.langfuse_capture_content:
            return sanitize_payload(value)
        return _content_summary(value)

    def _mask(self, *, data: Any, **_: Any) -> Any:
        return sanitize_payload(data)

    @contextmanager
    def observe(
        self,
        name: str,
        *,
        as_type: ObservationType = "span",
        trace_id: str | None = None,
        input: object | None = None,
        metadata: Mapping[str, object] | None = None,
        version: str | None = None,
        model: str | None = None,
        model_parameters: Mapping[str, str | int | float | bool | list[str] | None] | None = None,
    ) -> Iterator[Observation]:
        resolved_trace_id = _normalize_trace_id(trace_id or current_trace_id() or name)
        if self._client is None:
            yield Observation(self, None, resolved_trace_id)
            return
        manager: Any | None = None
        raw: Any | None = None
        try:
            manager = cast(Any, self._client).start_as_current_observation(
                trace_context={"trace_id": resolved_trace_id},
                name=name,
                as_type=as_type,
                input=self.prepare_content(input),
                metadata=sanitize_payload(metadata or {}),
                version=version,
                model=model,
                model_parameters=dict(model_parameters) if model_parameters else None,
            )
            raw = manager.__enter__()
            with self._lock:
                self._last_trace_at = datetime.now(UTC)
        except Exception as exc:
            self._record_failure("span_start", exc)
            yield Observation(self, None, resolved_trace_id)
            return

        observation = Observation(self, raw, resolved_trace_id)
        try:
            yield observation
        except BaseException as exc:
            observation.update(level="ERROR", status_message=type(exc).__name__)
            raise
        finally:
            try:
                manager.__exit__(None, None, None)
            except Exception as exc:
                self._record_failure("span_end", exc)

    def safe_call(self, operation: str, function: Any, **kwargs: object) -> None:
        try:
            function(**kwargs)
        except Exception as exc:
            self._record_failure(operation, exc)

    def health(self, *, remote: bool = False) -> ObservabilityHealth:
        if not self.settings.langfuse_enabled:
            status: Literal["ok", "degraded", "disabled", "unavailable"] = "disabled"
        elif not self._configured:
            status = "degraded"
        elif self._client is None:
            status = "unavailable"
        else:
            with self._lock:
                status = "degraded" if self._last_error else "ok"
            if remote:
                try:
                    authenticated = self._client.auth_check()
                    with self._lock:
                        self._last_remote_check_at = datetime.now(UTC)
                        if authenticated:
                            self._last_error = None
                    if not authenticated:
                        status = "unavailable"
                        self._set_error("authentication_failed")
                except Exception as exc:
                    status = "unavailable"
                    self._record_failure("auth_check", exc, count_drop=False)
        with self._lock:
            return ObservabilityHealth(
                status=status,
                enabled=self.settings.langfuse_enabled,
                configured=self._configured,
                base_url=self.settings.langfuse_base_url,
                sample_rate=self.settings.langfuse_sample_rate,
                capture_content=self.settings.langfuse_capture_content,
                last_trace_at=self._iso(self._last_trace_at),
                last_remote_check_at=self._iso(self._last_remote_check_at),
                last_error=self._last_error,
                dropped_spans=self._dropped_spans,
            )

    def flush(self, *, timeout_seconds: float = 3.0) -> bool:
        if self._client is None:
            return True
        return self._bounded_client_call("flush", self._client.flush, timeout_seconds)

    def shutdown(self, *, timeout_seconds: float = 3.0) -> bool:
        if self._client is None:
            return True
        return self._bounded_client_call("shutdown", self._client.shutdown, timeout_seconds)

    def _bounded_client_call(self, name: str, function: Any, timeout_seconds: float) -> bool:
        error: list[BaseException] = []

        def invoke() -> None:
            try:
                function()
            except BaseException as exc:
                error.append(exc)

        thread = threading.Thread(target=invoke, name=f"langfuse-{name}", daemon=True)
        thread.start()
        thread.join(timeout_seconds)
        if thread.is_alive():
            self._set_error(f"{name}_timeout")
            return False
        if error:
            self._record_failure(name, error[0], count_drop=False)
            return False
        return True

    def _record_failure(
        self, operation: str, exc: BaseException, *, count_drop: bool = True
    ) -> None:
        error = f"{operation}:{type(exc).__name__}"
        with self._lock:
            self._last_error = error
            if count_drop:
                self._dropped_spans += 1
        _logger.warning(
            "langfuse_export_degraded",
            operation=operation,
            error_type=type(exc).__name__,
        )

    def _set_error(self, error: str) -> None:
        with self._lock:
            self._last_error = error

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None


@lru_cache(maxsize=1)
def get_observability_service() -> ObservabilityService:
    return ObservabilityService(get_settings())


@contextmanager
def observe(
    name: str,
    *,
    as_type: ObservationType = "span",
    trace_id: str | None = None,
    input: object | None = None,
    metadata: Mapping[str, object] | None = None,
    version: str | None = None,
    model: str | None = None,
    model_parameters: Mapping[str, str | int | float | bool | list[str] | None] | None = None,
) -> Iterator[Observation]:
    with get_observability_service().observe(
        name,
        as_type=as_type,
        trace_id=trace_id,
        input=input,
        metadata=metadata,
        version=version,
        model=model,
        model_parameters=model_parameters,
    ) as observation:
        yield observation
