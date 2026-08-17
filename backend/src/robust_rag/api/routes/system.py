"""Non-sensitive system metadata endpoints."""

from fastapi import APIRouter
from pydantic import BaseModel

from robust_rag import __version__
from robust_rag.core.settings import get_settings

router = APIRouter(prefix="/system", tags=["system"])


class SystemInfoResponse(BaseModel):
    name: str
    version: str
    environment: str


@router.get("/info", response_model=SystemInfoResponse)
async def system_info() -> SystemInfoResponse:
    """Expose safe build and environment information."""

    settings = get_settings()
    return SystemInfoResponse(
        name=settings.app_name,
        version=__version__,
        environment=settings.app_env,
    )
