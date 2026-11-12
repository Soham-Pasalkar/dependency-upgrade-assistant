"""Main FastAPI Application Entrypoint for Dependency Upgrade Assistant 2.0."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator
import logging

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.config import settings
from src.api.database import init_db
from src.api.security import RequestProtectionMiddleware
from src.api.routes.health import router as health_router
from src.api.routes.manifest import router as manifest_router
from src.api.routes.plan import router as plan_router
from src.api.routes.recommendations import router as recommendations_router
from src.api.routes.validation import router as validation_router
from src.api.routes.export import router as export_router
from src.api.routes.runs import router as runs_router

logger = logging.getLogger("dua.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan context manager for startup and shutdown events."""
    logger.info("Initializing database on startup...")
    try:
        init_db()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        # Allow app to start so health check can report degraded state
    try:
        from src.services.repository_service import cleanup_expired_workspaces
        removed = cleanup_expired_workspaces(settings.resolved_workspace_root)
        if removed:
            logger.info(f"Removed {removed} expired repository workspace(s).")
    except Exception as e:
        logger.warning(f"Workspace cleanup skipped: {e}")
    yield
    logger.info("Application shutting down...")


def create_app() -> FastAPI:
    """FastAPI application factory."""
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="Interactive, security-first developer platform for Python and Node.js dependency upgrades.",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Host, Origin, content-type and client-header checks (innermost: runs after CORS preflight handling)
    app.add_middleware(
        RequestProtectionMiddleware,
        allowed_hosts=settings.ALLOWED_HOSTS,
        allowed_origins=settings.CORS_ORIGINS,
    )

    # Configure CORS for local development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS", "HEAD"],
        allow_headers=["Content-Type", "Accept", "X-DUA-Client"],
    )

    # Global Exception Handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(f"Unhandled exception on {request.method} {request.url.path}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An unexpected server error occurred.",
                    "details": str(exc) if settings.DEBUG else None,
                }
            },
        )

    # Register Routers
    # Register under /api for frontend integration
    app.include_router(health_router, prefix="/api")
    app.include_router(manifest_router, prefix="/api")
    app.include_router(recommendations_router, prefix="/api")
    app.include_router(plan_router, prefix="/api")
    app.include_router(validation_router, prefix="/api")
    app.include_router(export_router, prefix="/api")
    app.include_router(runs_router, prefix="/api")
    # Also register under /api/v1 for REST API compliance with architecture spec
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(manifest_router, prefix="/api/v1")
    app.include_router(recommendations_router, prefix="/api/v1")
    app.include_router(plan_router, prefix="/api/v1")
    app.include_router(validation_router, prefix="/api/v1")
    app.include_router(export_router, prefix="/api/v1")
    app.include_router(runs_router, prefix="/api/v1")
    # Convenience root health check
    app.include_router(health_router)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.main:app", host=settings.HOST, port=settings.PORT, reload=settings.is_development)
