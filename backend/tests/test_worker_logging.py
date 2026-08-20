from __future__ import annotations

import json
import logging
from types import SimpleNamespace

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


def test_worker_observability_flushes_and_publishes_status_after_failed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []
    published: list[tuple[str, int, str]] = []

    class Service:
        def flush(self, *, timeout_seconds: float) -> bool:
            calls.append(("flush", timeout_seconds))
            return True

        def health(self, *, remote: bool = False) -> SimpleNamespace:
            assert remote is False
            return SimpleNamespace(
                snapshot=lambda: {
                    "status": "ok",
                    "enabled": True,
                    "configured": True,
                    "dropped_spans": 0,
                }
            )

    class RedisClient:
        def setex(self, key: str, ttl: int, value: str) -> None:
            published.append((key, ttl, value))

    monkeypatch.setattr(worker_celery, "get_observability_service", Service)
    monkeypatch.setattr(worker_celery, "_heartbeat_redis", RedisClient)

    worker_celery._flush_worker_observability(
        task_id="task-1",
        task=SimpleNamespace(name="graph.extract"),
        state="FAILURE",
    )

    assert calls == [("flush", worker_celery.settings.langfuse_timeout_seconds)]
    assert len(published) == 1
    key, ttl, raw = published[0]
    assert key == "robust-rag:worker:observability"
    assert ttl == worker_celery.settings.celery_heartbeat_ttl_seconds * 2
    snapshot = json.loads(raw)
    assert snapshot["status"] == "ok"
    assert snapshot["flush_ok"] is True
    assert snapshot["task_name"] == "graph.extract"
    assert snapshot["task_id"] == "task-1"
    assert snapshot["task_state"] == "FAILURE"
    assert snapshot["last_flush_at"]


def test_worker_observability_resets_after_fork_and_shuts_down_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float | None]] = []

    class Service:
        def shutdown(self, *, timeout_seconds: float) -> bool:
            calls.append(("shutdown", timeout_seconds))
            return True

    service = Service()
    monkeypatch.setattr(
        worker_celery,
        "reset_observability_service_after_fork",
        lambda: calls.append(("reset", None)),
    )
    monkeypatch.setattr(
        worker_celery,
        "get_initialized_observability_service",
        lambda: service,
    )

    worker_celery._initialize_worker_observability()
    worker_celery._shutdown_worker_observability()
    worker_celery._shutdown_worker_observability()

    assert calls == [
        ("reset", None),
        ("shutdown", worker_celery.settings.langfuse_timeout_seconds),
    ]
