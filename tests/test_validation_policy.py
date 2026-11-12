"""M11 / F13: the validation operation is server-controlled and bound into approval."""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.config import settings
from src.api.main import app
from src.models.sandbox import SandboxExecutionRequest, ValidationReportResponse, ValidationStatus
from src.sandbox.runner import DockerAvailability, SandboxExecutionResult
from src.sandbox.validation_policy import derive_validation_policy, policy_for_proposal
from src.services.proposal_registry import get_proposal_registry
from src.services.sandbox_service import get_sandbox_service

MANIFEST = "requests==2.28.0\n"
UP = DockerAvailability(is_available=True, version="test")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _approved_plan(client, approve=True):
    run = client.post("/api/runs/manifest", json={"manifest_type": "requirements.txt", "content": MANIFEST}).json()
    client.post(f"/api/runs/{run['run_id']}/manifests/0/recommendations", json={"force_deterministic": True})
    plan = client.post(f"/api/runs/{run['run_id']}/manifests/0/plan", json={}).json()
    if approve:
        resp = client.post(f"/api/proposals/{plan['proposal_id']}/approve",
                           json={"diff_sha256": hashlib.sha256(plan["unified_diff"].encode()).hexdigest()})
        assert resp.status_code == 200
        plan["_approval"] = resp.json()
    return plan


class Recorder:
    """Stands in for the container runner and records exactly what would execute."""

    def __init__(self, exit_code=0, stdout=""):
        self.calls = []
        self.exit_code = exit_code
        self.stdout = stdout

    def __call__(self, command_args, working_dir, **kwargs):
        self.calls.append({"cmd": list(command_args), "config": kwargs.get("config")})
        return SandboxExecutionResult(exit_code=self.exit_code, stdout=self.stdout, stderr="", duration_seconds=0.1)


def _run(client, body, recorder):
    svc = get_sandbox_service()
    with patch.object(svc, "get_availability", return_value=UP), \
         patch.object(svc.runner, "check_availability", return_value=UP), \
         patch.object(svc.runner, "run_command", side_effect=recorder):
        return client.post("/api/validation/execute", json=body)


# --- The Phase A false-PASSED scenario ------------------------------------------------------

def test_noop_custom_command_is_rejected_and_never_executed(client):
    plan = _approved_plan(client)
    rec = Recorder()
    resp = _run(client, {"proposal_id": plan["proposal_id"], "custom_test_cmd": ["python", "-c", "pass"]}, rec)
    assert resp.status_code == 422
    assert "custom_test_cmd is not accepted" in resp.text
    assert rec.calls == []


@pytest.mark.parametrize("image", ["attacker/pwn-box:latest", "python:3.12-slim", "node:22-slim"])
def test_client_image_choice_is_rejected(client, image):
    plan = _approved_plan(client)
    rec = Recorder()
    resp = _run(client, {"proposal_id": plan["proposal_id"], "image_override": image}, rec)
    assert resp.status_code == 422 and rec.calls == []


@pytest.mark.parametrize("field,value", [
    ("timeout_seconds", 5), ("memory_limit_mb", 1024), ("pids_limit", 128), ("cpu_limit", "2.0"),
    ("manifest_type", "package.json"), ("ecosystem", "npm"),
])
def test_client_cannot_change_validation_settings(client, field, value):
    plan = _approved_plan(client)
    rec = Recorder()
    resp = _run(client, {"proposal_id": plan["proposal_id"], field: value}, rec)
    assert resp.status_code == 422 and rec.calls == []


def test_values_equal_to_the_policy_are_accepted_for_compatibility(client):
    plan = _approved_plan(client)
    rec = Recorder()
    body = {"proposal_id": plan["proposal_id"], "manifest_type": "requirements.txt", "timeout_seconds": 60,
            "memory_limit_mb": 512, "cpu_limit": "1.0", "pids_limit": 64}
    assert _run(client, body, rec).status_code == 200


def test_server_runs_only_the_approved_policy(client):
    plan = _approved_plan(client)
    rec = Recorder()
    report = _run(client, {"proposal_id": plan["proposal_id"]}, rec).json()
    policy = plan["_approval"]["validation_policy"]
    assert report["validation_fingerprint"] == plan["_approval"]["validation_fingerprint"]
    assert report["validation_policy"] == policy
    for call in rec.calls:
        cfg = call["config"]
        assert f"{cfg.image}@" in policy["image"] or cfg.image == policy["image"].split("@")[0]
        assert cfg.network_enabled is False
        assert cfg.memory_mb == policy["memory_limit_mb"] and cfg.pids_limit == policy["pids_limit"]
        assert cfg.timeout_seconds == policy["timeout_seconds"]


# --- Outcomes stay distinguishable; nothing that did not run is a pass -----------------------

def test_no_tests_is_not_a_pass(client):
    plan = _approved_plan(client)  # manifest-only plan: the snapshot has no tests
    report = _run(client, {"proposal_id": plan["proposal_id"]}, Recorder()).json()
    assert report["status"] == "NO_TESTS_FOUND"
    assert [c["command_name"] for c in report["commands"]] == ["py_compile"]


def test_failing_checks_are_failed(client):
    plan = _approved_plan(client)
    report = _run(client, {"proposal_id": plan["proposal_id"]}, Recorder(exit_code=1)).json()
    assert report["status"] == "FAILED"


def test_docker_unavailable_is_not_runnable(client):
    plan = _approved_plan(client)
    down = DockerAvailability(is_available=False, error_message="no docker")
    svc = get_sandbox_service()
    with patch.object(svc, "get_availability", return_value=down):
        report = client.post("/api/validation/execute", json={"proposal_id": plan["proposal_id"]}).json()
    assert report["status"] == "NOT_RUNNABLE" and report["commands"] == []
    assert report["validation_fingerprint"] == plan["_approval"]["validation_fingerprint"]


# --- Approval binding ---------------------------------------------------------------------

def test_approval_records_the_server_policy(client):
    plan = _approved_plan(client)
    approval = plan["_approval"]
    proposal = get_proposal_registry().get(plan["proposal_id"]).proposal
    assert approval["validation_fingerprint"] == policy_for_proposal(proposal).fingerprint
    assert approval["validation_policy"]["network"] == "none"
    assert "@sha256:" in approval["validation_policy"]["image"]
    state = client.get(f"/api/proposals/{plan['proposal_id']}/approval").json()
    assert state["validation_fingerprint"] == approval["validation_fingerprint"]


def test_changing_validation_settings_after_approval_requires_fresh_approval(client, monkeypatch):
    plan = _approved_plan(client)
    monkeypatch.setattr(settings, "DOCKER_MAX_MEMORY_MB", 768)
    rec = Recorder()
    resp = _run(client, {"proposal_id": plan["proposal_id"]}, rec)
    assert resp.status_code == 409 and "validation settings changed" in resp.json()["detail"]
    assert rec.calls == []
    assert client.post("/api/export", json={"proposal_id": plan["proposal_id"]}).status_code == 409
    # Approving again covers the new settings.
    digest = hashlib.sha256(plan["unified_diff"].encode()).hexdigest()
    assert client.post(f"/api/proposals/{plan['proposal_id']}/approve", json={"diff_sha256": digest}).status_code == 200
    assert _run(client, {"proposal_id": plan["proposal_id"]}, Recorder()).status_code == 200


def test_legacy_approval_without_fingerprint_fails_closed(client, isolated_run_store):
    plan = _approved_plan(client)
    with isolated_run_store._conn() as c:
        c.execute("UPDATE dua_approvals SET validation_fingerprint=NULL WHERE proposal_id=?", (plan["proposal_id"],))
    rec = Recorder()
    assert _run(client, {"proposal_id": plan["proposal_id"]}, rec).status_code == 409
    assert rec.calls == []


def test_export_rejects_a_run_with_other_validation_settings(client):
    plan = _approved_plan(client)
    svc = get_sandbox_service()
    forged = ValidationReportResponse(
        execution_id="val-forged", proposal_id=plan["proposal_id"], status=ValidationStatus.PASSED,
        is_runnable=True, summary="x", commands=[], image_used="python:3.11-slim", network_enabled=False,
        logs="", created_at=0.0, validation_fingerprint="0" * 64,
    )
    with svc._lock:
        svc._executions["val-forged"] = forged
    resp = client.post("/api/export", json={"proposal_id": plan["proposal_id"], "execution_id": "val-forged"})
    assert resp.status_code == 409 and "approved validation settings" in resp.json()["detail"]


def test_service_ignores_client_limits_even_when_called_directly():
    req = SandboxExecutionRequest.model_construct(proposal_id=None, manifest_type="requirements.txt",
                                                  timeout_seconds=5, memory_limit_mb=1024, pids_limit=128,
                                                  cpu_limit="2.0", custom_test_cmd=None, image_override=None,
                                                  ecosystem=None)
    svc = get_sandbox_service()
    rec = Recorder()
    with patch.object(svc, "get_availability", return_value=UP), patch.object(svc.runner, "run_command", side_effect=rec):
        report = svc.execute_validation(req)
    policy = derive_validation_policy("requirements.txt", None, None)
    assert report.resource_limits["memory_limit_mb"] == policy.memory_limit_mb
    assert all(c["config"].timeout_seconds == policy.timeout_seconds for c in rec.calls)


def test_policy_fingerprint_is_deterministic_and_sensitive(monkeypatch):
    a = derive_validation_policy("requirements.txt", "python", "requirements.txt")
    b = derive_validation_policy("requirements.txt", "python", "requirements.txt")
    assert a.fingerprint == b.fingerprint
    assert derive_validation_policy("package.json", "npm", "package.json").fingerprint != a.fingerprint
    assert derive_validation_policy("requirements.txt", "python", "api/requirements.txt").fingerprint != a.fingerprint
    monkeypatch.setattr(settings, "DOCKER_TIMEOUT_SECONDS", 90)
    assert derive_validation_policy("requirements.txt", "python", "requirements.txt").fingerprint != a.fingerprint
