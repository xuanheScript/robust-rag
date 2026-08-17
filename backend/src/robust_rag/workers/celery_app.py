"""Celery application shared by all ingestion workers."""

from typing import TypedDict

from celery import Celery

from robust_rag import __version__
from robust_rag.core.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "robust_rag",
    broker=settings.redis_url,
    backend=settings.celery_result_backend,
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
    worker_send_task_events=True,
    task_send_sent_event=True,
)


class PingResult(TypedDict):
    status: str
    version: str


@celery_app.task(name="system.ping")  # type: ignore[untyped-decorator]
def ping() -> PingResult:
    """Cheap task used to verify broker and worker connectivity."""

    return {"status": "ok", "version": __version__}
