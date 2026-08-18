"""Structured logging configuration."""

import logging
import sys
from typing import Any, cast

import structlog

from robust_rag.core.observability import sanitize_payload


def redact_sensitive_log_fields(
    _: object, __: str, event_dict: dict[str, object]
) -> dict[str, object]:
    """Apply the same credential redaction policy used by exported traces."""

    return cast(dict[str, object], sanitize_payload(event_dict))


def configure_logging(level: str, output_format: str) -> None:
    """Configure stdlib logging and structlog with shared context variables."""

    logging.basicConfig(
        format="%(message)s",
        level=level.upper(),
        stream=sys.stdout,
        force=True,
    )

    configure_structlog(level, output_format)


def configure_structlog(level: str, output_format: str) -> None:
    """Configure structlog without replacing logging handlers owned by Celery."""

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_sensitive_log_fields,
    ]
    renderer: Any
    if output_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
