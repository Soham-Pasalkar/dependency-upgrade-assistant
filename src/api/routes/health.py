"""Health Check Endpoints for Dependency Upgrade Assistant 2.0."""

from datetime import datetime, timezone
from typing import Any, Dict
from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from src.api.config import settings
from src.api.database import check_db_connectivity, get_db_path

router = APIRouter(tags=["Health"])


class DatabaseHealth(BaseModel):
    status: str = Field(..., description="Database connection status: 'connected' or 'error'")
    path: str = Field(..., description="Path to SQLite database file")
    message: str = Field(default="ok", description="Diagnostic message")


class HealthResponse(BaseModel):
    status: str = Field(..., description="'healthy' when all subsystems work, otherwise 'degraded'")
    app_name: str = Field(..., description="Name of the application")
    version: str = Field(..., description="Semantic version of the application")
    environment: str = Field(..., description="Runtime environment")
    database: DatabaseHealth = Field(..., description="Database connectivity details")
    timestamp: str = Field(..., description="ISO 8601 UTC timestamp")


@router.get("/health", response_model=HealthResponse, summary="Application health check")
def health_check(response: Response) -> HealthResponse:
    """Verify application health and live database connectivity."""
    db_ok, db_msg = check_db_connectivity()
    
    overall_status = "healthy" if db_ok else "degraded"
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=overall_status,
        app_name=settings.APP_NAME,
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
        database=DatabaseHealth(
            status="connected" if db_ok else "error",
            path=str(get_db_path()),
            message=db_msg,
        ),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
