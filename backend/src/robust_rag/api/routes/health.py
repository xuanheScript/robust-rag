"""Process health endpoints."""

import json
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from robust_rag.core.errors import AppError
from robust_rag.core.observability import get_observability_service
from robust_rag.core.settings import get_settings
from robust_rag.db.session import get_db

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"
    database: Literal["ok"] = "ok"
    redis: Literal["ok"] = "ok"


class DependencyResponse(BaseModel):
    database: dict[str, object]
    redis: dict[str, object]
    graph: dict[str, object]
    worker: dict[str, object]
    queue: dict[str, object]
    scheduler: dict[str, object]
    langfuse: dict[str, object]
    providers: dict[str, object]


@lru_cache(maxsize=1)
def get_redis_client() -> Redis:
    return Redis.from_url(get_settings().redis_url)


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    """Report that the API process is alive without checking dependencies."""

    return LiveResponse()


@router.get("/ready", response_model=ReadyResponse)
def ready(
    db: Session = Depends(get_db),  # noqa: B008
    redis_client: Redis = Depends(get_redis_client),  # noqa: B008
) -> ReadyResponse:
    """Report whether durable job dependencies are reachable."""

    try:
        db.execute(text("SELECT 1"))
        redis_client.ping()
    except Exception as exc:
        raise AppError(
            code="DEPENDENCY_UNAVAILABLE",
            message="A required service is unavailable",
            status_code=503,
        ) from exc
    return ReadyResponse()


@router.get("/dependencies", response_model=DependencyResponse)
def dependencies(
    db: Session = Depends(get_db),  # noqa: B008
    redis_client: Redis = Depends(get_redis_client),  # noqa: B008
) -> DependencyResponse:
    """Report required services plus the optional graph enhancement independently."""

    database: dict[str, object]
    redis: dict[str, object]
    try:
        db.execute(text("SELECT 1"))
        database = {"status": "ok"}
    except Exception as exc:
        database = {"status": "unavailable", "error": type(exc).__name__}
    try:
        redis_client.ping()
        redis = {"status": "ok"}
    except Exception as exc:
        redis = {"status": "unavailable", "error": type(exc).__name__}

    settings = get_settings()
    from robust_rag.graph.factory import get_graph_store, graph_is_configured

    graph = (
        get_graph_store().health()
        if graph_is_configured(settings)
        else {"status": "disabled", "schema_version": settings.graph_schema_version}
    )
    worker, queue, scheduler = _celery_health(redis_client)
    langfuse = get_observability_service().health(remote=False).snapshot()
    llm_configured = bool(settings.llm_base_url and settings.llm_model and settings.llm_api_key)
    providers = {
        "status": "ok" if llm_configured else "unavailable",
        "llm": "configured" if llm_configured else "missing_api_key",
        "voyage": "configured" if settings.voyage_api_key else "missing",
        "mineru": "configured" if settings.mineru_token else "missing",
    }
    return DependencyResponse(
        database=database,
        redis=redis,
        graph=graph,
        worker=worker,
        queue=queue,
        scheduler=scheduler,
        langfuse=langfuse,
        providers=providers,
    )


@router.get("/observability")
def observability_health(remote: bool = False) -> dict[str, object]:
    """Expose safe Langfuse configuration and optionally validate Cloud credentials."""

    return get_observability_service().health(remote=remote).snapshot()


def _celery_health(
    redis_client: Redis,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    settings = get_settings()
    try:
        depth = int(cast(Any, redis_client).llen("celery"))
        queue: dict[str, object] = {
            "status": "warning" if depth >= settings.celery_queue_warning_depth else "ok",
            "depth": depth,
            "warning_depth": settings.celery_queue_warning_depth,
        }
    except Exception as exc:
        queue = {"status": "unknown", "error": type(exc).__name__}

    try:
        value = cast(Any, redis_client).get("robust-rag:worker:last_seen")
        worker = _heartbeat_status(value, settings.celery_heartbeat_ttl_seconds)
    except Exception as exc:
        worker = {"status": "unknown", "error": type(exc).__name__}
    worker["observability"] = _worker_observability_health(redis_client)

    try:
        value = cast(Any, redis_client).get("robust-rag:beat:last_seen")
        scheduler = _heartbeat_status(value, settings.celery_heartbeat_ttl_seconds)
    except Exception as exc:
        scheduler = {"status": "unknown", "error": type(exc).__name__}
    return worker, queue, scheduler


def _worker_observability_health(redis_client: Redis) -> dict[str, object]:
    try:
        value = cast(Any, redis_client).get("robust-rag:worker:observability")
    except Exception as exc:
        return {"status": "unknown", "error": type(exc).__name__}
    if value is None:
        return {"status": "unavailable", "last_flush_at": None}
    raw = value.decode("utf-8") if isinstance(value, bytes) else str(value)
    try:
        snapshot = json.loads(raw)
    except (TypeError, ValueError):
        return {"status": "unavailable", "error": "invalid_snapshot"}
    if not isinstance(snapshot, dict):
        return {"status": "unavailable", "error": "invalid_snapshot"}
    return {str(key): item for key, item in snapshot.items()}


def _heartbeat_status(value: object, ttl_seconds: int) -> dict[str, object]:
    if value is None:
        return {"status": "unavailable", "last_seen": None}
    raw = value.decode("utf-8") if isinstance(value, bytes) else str(value)
    try:
        last_seen = datetime.fromisoformat(raw)
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        age_seconds = max(0.0, (datetime.now(UTC) - last_seen).total_seconds())
    except ValueError:
        return {"status": "unavailable", "last_seen": raw, "error": "invalid_timestamp"}
    return {
        "status": "ok" if age_seconds <= ttl_seconds else "unavailable",
        "last_seen": last_seen.isoformat(),
        "age_seconds": round(age_seconds, 3),
        "ttl_seconds": ttl_seconds,
    }
