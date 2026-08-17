"""Process health endpoints."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    """Report that the API process is alive without checking dependencies."""

    return LiveResponse()
