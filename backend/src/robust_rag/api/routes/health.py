"""Process health endpoints."""

from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from robust_rag.core.errors import AppError
from robust_rag.core.settings import get_settings
from robust_rag.db.session import get_db

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"
    database: Literal["ok"] = "ok"
    redis: Literal["ok"] = "ok"


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
