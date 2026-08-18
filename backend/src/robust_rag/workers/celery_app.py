"""Celery application shared by all ingestion workers."""

import logging
from datetime import UTC, datetime
from functools import lru_cache
from typing import TypedDict

from celery import Celery
from celery.signals import after_setup_logger, heartbeat_sent, worker_process_init, worker_ready
from redis import Redis

from robust_rag import __version__
from robust_rag.core.logging import configure_structlog
from robust_rag.core.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "robust_rag",
    broker=settings.redis_url,
    backend=settings.celery_result_backend,
    include=["robust_rag.workers.tasks"],
)
celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    result_expires=86400,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_send_task_events=True,
    task_send_sent_event=True,
    broker_transport_options={"visibility_timeout": 6 * 60 * 60},
    result_backend_transport_options={"global_keyprefix": "robust-rag:"},
    beat_schedule={
        "recover-stale-ingestion-jobs": {
            "task": "ingestion.recover_pending",
            "schedule": 300.0,
        },
        "record-celery-beat-heartbeat": {
            "task": "system.record_beat_heartbeat",
            "schedule": 30.0,
        },
    },
)


class PingResult(TypedDict):
    status: str
    version: str


@celery_app.task(name="system.ping")  # type: ignore[untyped-decorator]
def ping() -> PingResult:
    """Cheap task used to verify broker and worker connectivity."""

    return {"status": "ok", "version": __version__}


@celery_app.task(name="system.record_beat_heartbeat")  # type: ignore[untyped-decorator]
def record_beat_heartbeat() -> dict[str, str]:
    """Record that Beat dispatched and a worker consumed the scheduled probe."""

    timestamp = datetime.now(UTC).isoformat()
    _heartbeat_redis().setex(
        "robust-rag:beat:last_seen",
        settings.celery_heartbeat_ttl_seconds * 2,
        timestamp,
    )
    return {"status": "ok", "last_seen": timestamp}


@lru_cache(maxsize=1)
def _heartbeat_redis() -> Redis:
    return Redis.from_url(settings.redis_url)


def _record_worker_heartbeat(**_: object) -> None:
    try:
        _heartbeat_redis().setex(
            "robust-rag:worker:last_seen",
            settings.celery_heartbeat_ttl_seconds * 2,
            datetime.now(UTC).isoformat(),
        )
    except Exception:
        # Broker connectivity is surfaced by health checks and Celery itself.
        return


def _configure_worker_logging(loglevel: int | str | None = None, **_: object) -> None:
    """Attach structlog to Celery's terminal in the main and child worker processes."""

    if isinstance(loglevel, int):
        resolved_level = logging.getLevelName(loglevel)
    elif isinstance(loglevel, str):
        resolved_level = loglevel
    else:
        resolved_level = settings.log_level
    configure_structlog(resolved_level, settings.log_format)


after_setup_logger.connect(_configure_worker_logging, weak=False)
worker_process_init.connect(_configure_worker_logging, weak=False)
heartbeat_sent.connect(_record_worker_heartbeat, weak=False)
worker_ready.connect(_record_worker_heartbeat, weak=False)
