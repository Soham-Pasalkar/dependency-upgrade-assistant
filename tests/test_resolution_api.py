"""Resolution API (FR-29, FR-30, FR-32; M12 criteria 10, 11 API part, 14)."""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.resolution.policy import COMPATIBILITY_DISCLOSURE
from src.resolution.resolver_runtime import RunResult
from tests.resolution_api_helpers import PACKAGE_JSON, make_plan
from tests.resolution_doubles import install_service, wait_for_job


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def start(client, plan):
    return client.post(f"/api/proposals/{plan['proposal_id']}/resolve")


def test_resolve_job_status_and_view(client):
    install_service()
    plan = make_plan(client)
    resp = start(client, plan)
    assert resp.status_code == 202
    job = resp.json()["job"]
    assert job["status"] in ("QUEUED", "RUNNING", "SUCCEEDED")
    wait_for_job(job["id"])
    status = client.get(f"/api/jobs/{job['id']}").json()
    assert status["status"] == "SUCCEEDED" and status["started_at"] and status["deadline_at"]
    view = client.get(f"/api/proposals/{plan['proposal_id']}/resolution").json()
    assert view["status"] == "COMPATIBLE" and view["approval_eligible"] is True
    assert view["disclosure"] == COMPATIBILITY_DISCLOSURE
    assert view["proposed"]["role"] == "proposed" and view["original"]["role"] == "original"
    assert view["comparison"]["available"] is True
    packages = view["proposed"]["packages"]
    assert packages and all({"name", "version", "direct", "introduced", "upload", "filename"} <= set(p) for p in packages)
    assert all(p["upload"]["filename"] == p["filename"] for p in packages)


def test_duplicate_submission_is_409(client):
    import threading
    gate = threading.Event()
    install_service(lambda role, manifest: gate.wait(5) and None)
    plan = make_plan(client)
    first = start(client, plan)
    second = start(client, plan)
    gate.set()
    assert first.status_code == 202 and second.status_code == 409
    assert second.json()["detail"]["code"] == "job_active"
    wait_for_job(first.json()["job"]["id"])


def test_cancel_endpoint(client):
    import threading
    gate = threading.Event()

    def blocked(role, manifest):
        gate.wait(5)
        return RunResult("cancelled", None, b"", "", None, "x")
    install_service(blocked)
    plan = make_plan(client)
    job_id = start(client, plan).json()["job"]["id"]
    cancelled = client.post(f"/api/jobs/{job_id}/cancel").json()
    assert cancelled["cancel_requested"] is True or cancelled["status"] == "CANCELLED"
    gate.set()
    assert wait_for_job(job_id).status.value == "CANCELLED"
    view = client.get(f"/api/proposals/{plan['proposal_id']}/resolution").json()
    assert view["status"] == "UNKNOWN" and view["approval_eligible"] is False and view["proposed"] is None


def test_npm_plan_answers_immediately_as_unsupported(client):
    install_service()
    plan = make_plan(client, content=PACKAGE_JSON, manifest_type="package.json")
    resp = start(client, plan)
    assert resp.status_code == 200 and resp.json()["job"] is None
    view = resp.json()["resolution"]
    assert view["status"] == "UNSUPPORTED" and view["approval_eligible"] is False
    assert "npm compatibility verification is not yet supported" in view["proposed"]["unsupported"][0]["message"]


@pytest.mark.parametrize("path", ["/api/jobs/nope", "/api/proposals/prop_missing/resolution",
                                  "/api/jobs/..%2Fetc/cancel", "/api/proposals/a%20b/resolution"])
def test_unknown_or_invalid_ids(client, path):
    resp = client.post(path) if path.endswith("/cancel") else client.get(path)
    assert resp.status_code == 404


def test_resolve_unknown_plan(client):
    assert client.post("/api/proposals/prop_missing/resolve").status_code == 404


def test_request_protection_applies(client):
    plan = make_plan(client)
    with TestClient(app, headers={}) as bare:
        resp = bare.post(f"/api/proposals/{plan['proposal_id']}/resolve")
    assert resp.status_code == 403


def test_view_never_shows_compatible_before_evidence(client):
    import threading
    gate = threading.Event()
    install_service(lambda role, manifest: gate.wait(5) and None)
    plan = make_plan(client)
    job_id = start(client, plan).json()["job"]["id"]
    view = client.get(f"/api/proposals/{plan['proposal_id']}/resolution").json()
    assert view["status"] == "UNKNOWN" and view["approval_eligible"] is False and view["job"]["id"] == job_id
    gate.set()
    wait_for_job(job_id)
