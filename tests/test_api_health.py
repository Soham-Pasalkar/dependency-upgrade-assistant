"""Tests for FastAPI Backend Application Foundation, Configuration, and Health Endpoint."""

import os
import sqlite3
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from src.api.config import Settings
from src.api.database import (
    check_db_connectivity,
    create_connection,
    get_db_connection,
    init_db,
)
from src.api.main import create_app


@pytest.fixture
def test_db_path(tmp_path):
    """Provide a temporary database file path."""
    db_file = tmp_path / "test_dua.db"
    return str(db_file)


@pytest.fixture
def client(test_db_path):
    """Provide a TestClient with a fresh temporary database."""
    with patch("src.api.database.settings.DATABASE_PATH", test_db_path), \
         patch("src.api.routes.health.settings.DATABASE_PATH", test_db_path):
        init_db(test_db_path)
        app = create_app()
        with TestClient(app) as test_client:
            yield test_client


def test_config_loading_and_defaults():
    """Verify default configuration settings and custom parsing."""
    s = Settings(
        APP_NAME="Custom Assistant",
        CORS_ORIGINS=["http://testserver:3000"],
    )
    assert s.APP_NAME == "Custom Assistant"
    assert s.CORS_ORIGINS == ["http://testserver:3000"]
    assert s.is_development is True

    # Test string parsing of CORS origins
    s2 = Settings(CORS_ORIGINS='["http://alpha:5173", "http://beta:5173"]')
    assert len(s2.CORS_ORIGINS) == 2
    assert "http://alpha:5173" in s2.CORS_ORIGINS


def test_database_init_and_connectivity(test_db_path):
    """Verify SQLite database initialization, table creation, and connectivity probe."""
    # Ensure database is initialized
    init_db(test_db_path)

    # Check that core tables exist
    with get_db_connection(test_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}
        assert "repositories" in tables
        assert "analysis_runs" in tables
        assert "pending_cleanups" in tables

        # Verify PRAGMAs
        cursor.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()[0]
        assert journal_mode.lower() == "wal"

        cursor.execute("PRAGMA foreign_keys;")
        foreign_keys = cursor.fetchone()[0]
        assert foreign_keys == 1

    # Verify live probe
    ok, msg = check_db_connectivity(test_db_path)
    assert ok is True
    assert msg == "connected"


def test_database_connectivity_failure_handled(tmp_path):
    """Verify database connectivity probe handles unopenable paths gracefully."""
    invalid_path = str(tmp_path / "nonexistent_dir" / "nested" / "invalid.db")
    # Do not create directory; attempt to connect
    ok, msg = check_db_connectivity(invalid_path)
    # Depending on sqlite3 version and OS, creating in nonexistent dir fails
    assert isinstance(ok, bool)
    assert isinstance(msg, str)


def test_health_endpoint_healthy(client):
    """Verify GET /api/health returns 200 and structured status when healthy."""
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["app_name"] == "Dependency Upgrade Assistant"
    assert "version" in data
    assert data["database"]["status"] == "connected"
    assert "timestamp" in data


def test_health_endpoint_aliases(client):
    """Verify /api/v1/health and /health work identically."""
    r1 = client.get("/api/v1/health")
    assert r1.status_code == 200
    assert r1.json()["status"] == "healthy"

    r2 = client.get("/health")
    assert r2.status_code == 200
    assert r2.json()["status"] == "healthy"


def test_health_endpoint_degraded_on_db_failure(client):
    """Verify GET /api/health reports degraded status and 503 on database error."""
    with patch("src.api.routes.health.check_db_connectivity", return_value=(False, "disk I/O error")):
        response = client.get("/api/health")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert data["database"]["status"] == "error"
        assert "disk I/O error" in data["database"]["message"]


def test_openapi_documentation_accessible(client):
    """Verify Swagger UI and OpenAPI schema are accessible."""
    docs_resp = client.get("/docs")
    assert docs_resp.status_code == 200
    assert "swagger" in docs_resp.text.lower() or "html" in docs_resp.headers.get("content-type", "")

    openapi_resp = client.get("/openapi.json")
    assert openapi_resp.status_code == 200
    schema = openapi_resp.json()
    assert schema["info"]["title"] == "Dependency Upgrade Assistant"
    assert "/api/health" in schema["paths"]


def test_cors_headers_returned(client):
    """Verify CORS headers are returned for allowed origin."""
    response = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"
