"""Request IDs and non-leaking server errors (audit F5, F6)."""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes.plan import get_upgrade_planner

client = TestClient(app)


def test_every_response_has_a_request_id():
    resp = client.get("/api/health")
    assert len(resp.headers["X-Request-ID"]) >= 8


def test_valid_incoming_request_id_is_kept_and_invalid_replaced():
    assert client.get("/api/health", headers={"X-Request-ID": "trace-12345678"}).headers["X-Request-ID"] == "trace-12345678"
    replaced = client.get("/api/health", headers={"X-Request-ID": "bad id\r\nInjected: x"}).headers["X-Request-ID"]
    assert "Injected" not in replaced and " " not in replaced


def test_blocked_requests_also_carry_a_request_id():
    resp = TestClient(app, headers={}).post("/api/runs/manifest", json={})
    assert resp.status_code == 403 and resp.headers.get("X-Request-ID")


def test_internal_errors_do_not_leak_exception_text():
    class Boom:
        def create_upgrade_proposal(self, _):
            raise RuntimeError("/Users/secret/path/to/internal.db is locked")

    app.dependency_overrides[get_upgrade_planner] = lambda: Boom()
    try:
        resp = client.post("/api/plan/generate", json={
            "manifest_type": "requirements.txt", "original_content": "x==1\n", "recommendations": [],
        })
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "/Users/secret" not in detail and "internal.db" not in detail
    assert resp.headers["X-Request-ID"] in detail
