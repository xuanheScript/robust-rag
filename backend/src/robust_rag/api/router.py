"""Top-level API router."""

from fastapi import APIRouter

from robust_rag.api.routes.system import router as system_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(system_router)
