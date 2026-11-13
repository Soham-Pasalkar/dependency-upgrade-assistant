"""Request protection tests: Host (DNS rebinding), Origin, content type and client header (NFR-1, T-1, T-2)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.security import hostname_of

GOOD = {"Origin": "http://localhost:5173", "X-DUA-Client": "web"}
BODY = {"manifest_type": "requirements.txt", "content": "requests==2.28.0\n"}


def make_client(host: str = "localhost:8000") -> TestClient:
    # Explicit headers bypass the suite-wide defaults so each check can be isolated.
    return TestClient(app, base_url=f"http://{host}", headers={"Accept": "application/json"})


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000", "localhost"])
def test_loopback_hosts_are_accepted(host):
    assert make_client(host).get("/api/health").status_code == 200


@pytest.mark.parametrize("host", ["attacker.example:8000", "evil.localhost.example", "192.168.1.5:8000", "0.0.0.0:8000"])
def test_rebinding_hosts_are_rejected(host):
    resp = make_client(host).get("/api/health")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid Host header."


def test_malformed_host_is_rejected():
    assert hostname_of("localhost:abc") is None
    assert hostname_of("") is None
    assert hostname_of("a:b:c") is None
    assert hostname_of("[::1]:5173") == "::1"


def test_post_without_origin_is_rejected():
    resp = make_client().post("/api/runs/manifest", json=BODY, headers={"X-DUA-Client": "web"})
    assert resp.status_code == 403


@pytest.mark.parametrize("origin", ["null", "https://attacker.example", "http://localhost:9999"])
def test_post_from_foreign_origin_is_rejected(origin):
    resp = make_client().post("/api/runs/manifest", json=BODY, headers={"Origin": origin, "X-DUA-Client": "web"})
    assert resp.status_code == 403


def test_post_without_client_header_is_rejected():
    resp = make_client().post("/api/runs/manifest", json=BODY, headers={"Origin": "http://localhost:5173"})
    assert resp.status_code == 403
    assert "client header" in resp.json()["detail"]


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"])
def test_simple_request_content_types_are_rejected(ctype):
    resp = make_client().post(
        "/api/proposals/prop_x/approve", content=b'{"diff_sha256":"' + b"0" * 64 + b'"}',
        headers={**GOOD, "Content-Type": ctype},
    )
    assert resp.status_code == 415


def test_multipart_is_allowed_only_on_upload():
    resp = make_client().post(
        "/api/manifest/upload", files={"file": ("requirements.txt", b"requests==2.28.0\n", "text/plain")}, headers=GOOD
    )
    assert resp.status_code != 415


def test_well_formed_request_passes_protection():
    resp = make_client().post("/api/proposals/prop_missing/approve", json={"diff_sha256": "0" * 64}, headers=GOOD)
    # Passes the protection layer and reaches the route (which rejects the unknown plan).
    assert resp.status_code == 409


def test_cors_preflight_is_answered_for_allowed_origin():
    resp = make_client().options(
        "/api/runs/manifest",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-dua-client",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"
