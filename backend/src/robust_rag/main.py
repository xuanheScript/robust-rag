"""FastAPI application factory."""

import structlog
from fastapi import FastAPI

from robust_rag import __version__
from robust_rag.api.router import api_router
from robust_rag.api.routes.health import router as health_router
from robust_rag.core.errors import AppError, app_error_handler
from robust_rag.core.logging import configure_logging
from robust_rag.core.middleware import TraceContextMiddleware
from robust_rag.core.settings import get_settings


def create_app() -> FastAPI:
    """Build the API application with deterministic configuration."""

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    logger = structlog.get_logger(__name__)

    application = FastAPI(
        title=settings.app_name,
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    application.add_middleware(TraceContextMiddleware)
    application.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    application.include_router(health_router)
    application.include_router(api_router)

    @application.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"service": settings.app_name, "version": __version__}

    logger.info("application_configured", environment=settings.app_env, version=__version__)
    return application


app = create_app()
