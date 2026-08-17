"""Top-level API router."""

from fastapi import APIRouter

from robust_rag.api.routes.chat import router as chat_router
from robust_rag.api.routes.documents import router as documents_router
from robust_rag.api.routes.graph import router as graph_router
from robust_rag.api.routes.jobs import router as jobs_router
from robust_rag.api.routes.retrieval import router as retrieval_router
from robust_rag.api.routes.search_admin import router as search_admin_router
from robust_rag.api.routes.system import router as system_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(system_router)
api_router.include_router(documents_router)
api_router.include_router(graph_router)
api_router.include_router(jobs_router)
api_router.include_router(search_admin_router)
api_router.include_router(retrieval_router)
api_router.include_router(chat_router)
