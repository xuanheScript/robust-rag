"""ASGI middleware for request correlation."""

import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from robust_rag.core.observability import (
    bind_trace_id,
    observe,
    reset_trace_id,
    trace_id_from_seed,
)


class TraceContextMiddleware:
    """Bind a request ID to logs and expose it in every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_headers = Headers(scope=scope)
        request_id = request_headers.get("x-request-id", str(uuid4()))
        supplied_trace_id = request_headers.get("x-trace-id")
        trace_id = trace_id_from_seed(supplied_trace_id or request_id)
        trace_token = bind_trace_id(trace_id)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, trace_id=trace_id)
        logger = structlog.get_logger(__name__)
        started = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = MutableHeaders(scope=message)
                response_headers.append("x-request-id", request_id)
                response_headers.append("x-trace-id", trace_id)
            await send(message)

        try:
            logger.info("http_request_started", method=scope["method"], path=scope["path"])
            with observe(
                "http.request",
                trace_id=trace_id,
                input={"method": scope["method"], "path": scope["path"]},
                metadata={"request_id": request_id},
            ) as observation:
                try:
                    await self.app(scope, receive, send_with_request_id)
                finally:
                    observation.update(
                        output={"status_code": status_code},
                        metadata={"duration_ms": round((time.perf_counter() - started) * 1000, 3)},
                        level="ERROR" if status_code >= 500 else "DEFAULT",
                    )
            logger.info(
                "http_request_completed",
                method=scope["method"],
                path=scope["path"],
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        finally:
            structlog.contextvars.clear_contextvars()
            reset_trace_id(trace_token)


SendCallable = Callable[[Message], Awaitable[None]]
