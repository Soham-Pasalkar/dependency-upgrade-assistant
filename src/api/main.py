"""Main FastAPI Application Entrypoint for Dependency Upgrade Assistant 2.0."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator
import logging

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.config import settings
from src.api.database import init_db
from src.api.routes.health import router as health_router

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

    # Configure CORS for local development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"],
        allow_headers=["*"],
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
    # Also register under /api/v1 for REST API compliance with architecture spec
    app.include_router(health_router, prefix="/api/v1")
    # Convenience root health check
    app.include_router(health_router)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.main:app", host=settings.HOST, port=settings.PORT, reload=settings.is_development)
