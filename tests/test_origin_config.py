"""Regression tests for the 'Request blocked: origin not allowed' failure on Vite port 5174.

Origin checks stay mandatory; the allow-list is configuration-driven:
explicit CORS_ORIGINS everywhere, plus loopback dev ports only when ENVIRONMENT=development.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api import main as main_module
from src.api.config import Settings, normalize_origin, settings

BODY = {"manifest_type": "requirements.txt", "content": "requests==2.28.0\n"}


def _client_for(monkeypatch, **overrides) -> TestClient:
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)
    app = main_module.create_app()
    return TestClient(app, base_url="http://127.0.0.1:8000", headers={"Accept": "application/json"})


def _post(client: TestClient, origin: str, path: str = "/api/proposals/prop_x/approve"):
    # Approve on an unknown plan: passes the protection layer only if the origin is accepted (then 409).
    return client.post(path, json={"diff_sha256": "0" * 64}, headers={"Origin": origin, "X-DUA-Client": "web"})


# --- Settings ---------------------------------------------------------------

def test_development_allows_vite_fallback_port_on_all_loopback_names():
    s = Settings(ENVIRONMENT="development", CORS_ORIGINS=[])
    for host in ("localhost", "127.0.0.1", "[::1]"):
        assert f"http://{host}:5173" in s.allowed_origins
        assert f"http://{host}:5174" in s.allowed_origins


def test_production_does_not_add_loopback_origins():
    s = Settings(ENVIRONMENT="production", CORS_ORIGINS=["https://dua.example.internal"])
    assert s.allowed_origins == ["https://dua.example.internal"]


def test_production_requires_explicit_origins():
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        Settings(ENVIRONMENT="production", CORS_ORIGINS=[])


@pytest.mark.parametrize("bad", ["*", "null", "https://*.example.com", "localhost:5173", "ftp://x.example",
                                 "http://x.example/path", "http://user:pw@x.example", "http://x.example?q=1"])
def test_wildcard_and_malformed_origins_are_refused(bad):
    with pytest.raises(ValidationError):
        Settings(CORS_ORIGINS=[bad])


def test_origins_are_normalized():
    assert normalize_origin("HTTP://LocalHost:5174/") == "http://localhost:5174"
    assert normalize_origin("https://[::1]:8443") == "https://[::1]:8443"


def test_unknown_environment_is_refused():
    with pytest.raises(ValidationError):
        Settings(ENVIRONMENT="staging-ish")


def test_dev_ports_are_configurable_from_environment(monkeypatch):
    monkeypatch.setenv("DEV_FRONTEND_PORTS", "[5180]")
    s = Settings(ENVIRONMENT="development", CORS_ORIGINS=[])
    assert "http://localhost:5180" in s.allowed_origins
    assert "http://localhost:5173" not in s.allowed_origins


# --- Middleware through the real application ---------------------------------

@pytest.mark.parametrize("origin", [
    "http://localhost:5173", "http://localhost:5174", "http://127.0.0.1:5174", "http://[::1]:5174",
])
def test_dev_frontend_origins_reach_the_routes(monkeypatch, origin):
    client = _client_for(monkeypatch, ENVIRONMENT="development", CORS_ORIGINS=[])
    resp = _post(client, origin)
    assert resp.status_code == 409, resp.text  # route reached; unknown plan refused


def test_manifest_analysis_accepted_from_5174(monkeypatch):
    client = _client_for(monkeypatch, ENVIRONMENT="development", CORS_ORIGINS=[])
    resp = client.post("/api/runs/manifest", json={"manifest_type": "Gemfile", "content": "x"},
                       headers={"Origin": "http://localhost:5174", "X-DUA-Client": "web"})
    assert resp.status_code == 422  # reached validation in the route, not blocked by origin


@pytest.mark.parametrize("origin", [
    "http://localhost:5175", "http://localhost", "https://localhost:5174", "http://evil.example:5174",
    "http://localhost.evil.example:5174", "null",
])
def test_other_origins_are_still_rejected_in_development(monkeypatch, origin):
    client = _client_for(monkeypatch, ENVIRONMENT="development", CORS_ORIGINS=[])
    resp = _post(client, origin)
    assert resp.status_code == 403
    assert resp.json()["detail"].startswith("Request blocked: origin not allowed")


def test_production_rejects_loopback_dev_origins(monkeypatch):
    client = _client_for(monkeypatch, ENVIRONMENT="production", CORS_ORIGINS=["https://dua.example.internal"])
    assert _post(client, "http://localhost:5174").status_code == 403
    assert _post(client, "https://dua.example.internal").status_code == 409


def test_missing_client_header_and_origin_still_rejected(monkeypatch):
    client = _client_for(monkeypatch, ENVIRONMENT="development", CORS_ORIGINS=[])
    no_header = client.post("/api/runs/manifest", json=BODY, headers={"Origin": "http://localhost:5174"})
    assert no_header.status_code == 403
    no_origin = client.post("/api/runs/manifest", json=BODY, headers={"X-DUA-Client": "web"})
    assert no_origin.status_code == 403


def test_cors_preflight_matches_origin_policy(monkeypatch):
    client = _client_for(monkeypatch, ENVIRONMENT="development", CORS_ORIGINS=[])
    pre = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,x-dua-client"}
    ok = client.options("/api/runs/manifest", headers={"Origin": "http://localhost:5174", **pre})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "http://localhost:5174"
    bad = client.options("/api/runs/manifest", headers={"Origin": "http://evil.example", **pre})
    assert "access-control-allow-origin" not in bad.headers


def test_unsupported_method_not_allowed_for_cors(monkeypatch):
    client = _client_for(monkeypatch, ENVIRONMENT="development", CORS_ORIGINS=[])
    pre = {"Origin": "http://localhost:5174", "Access-Control-Request-Method": "DELETE"}
    resp = client.options("/api/runs/manifest", headers=pre)
    assert resp.status_code == 400
