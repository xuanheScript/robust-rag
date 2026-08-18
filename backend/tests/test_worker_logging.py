from __future__ import annotations

import json
import logging

import pytest
import structlog

from robust_rag.core.logging import configure_structlog
from robust_rag.workers import celery_app as worker_celery


def test_structlog_configuration_emits_worker_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    structlog.reset_defaults()
    configure_structlog("INFO", "json")

    structlog.get_logger("worker-test").info(
        "graph_extraction_started",
        document_version_id="version-1",
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "graph_extraction_started"
    assert payload["document_version_id"] == "version-1"
    assert payload["level"] == "info"
    structlog.reset_defaults()


def test_worker_logging_hook_uses_celery_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        worker_celery,
        "configure_structlog",
        lambda level, output_format: configured.append((level, output_format)),
    )

    worker_celery._configure_worker_logging(loglevel=logging.INFO)

    assert configured == [("INFO", worker_celery.settings.log_format)]
