"""Server-authoritative runs, approval enforcement and repository analysis API (FR-1R, FR-2R, FR-15, FR-16, FR-18)."""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.config import settings
from src.api.main import app
from src.models.sandbox import ValidationReportResponse, ValidationStatus
from src.services import repository_service as rs
from src.services.sandbox_service import get_sandbox_service

MANIFEST = "requests==2.28.0\nurllib3==1.26.5\n"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manual_plan(client: TestClient):
    run = client.post("/api/runs/manifest", json={"manifest_type": "requirements.txt", "content": MANIFEST}).json()
    run_id = run["run_id"]
    recs = client.post(f"/api/runs/{run_id}/manifests/0/recommendations", json={"force_deterministic": True})
    assert recs.status_code == 200
    plan = client.post(f"/api/runs/{run_id}/manifests/0/plan", json={})
    assert plan.status_code == 200
    return run_id, plan.json()


def test_manual_run_is_stored_and_retrievable(client):
    run = client.post("/api/runs/manifest", json={"manifest_type": "requirements.txt", "content": MANIFEST})
    assert run.status_code == 200
    body = run.json()
    assert body["source"] == "manifest" and body["manifests"][0]["analysis"]["total_dependencies"] == 2
    again = client.get(f"/api/runs/{body['run_id']}")
    assert again.status_code == 200 and again.json()["manifests"][0]["path"] == "requirements.txt"
    assert "content" not in again.json()["manifests"][0]


def test_unknown_run_and_unsupported_format(client):
    assert client.get("/api/runs/run_missing").status_code == 404
    bad = client.post("/api/runs/manifest", json={"manifest_type": "Gemfile", "content": "x"})
    assert bad.status_code == 422


def test_plan_requires_recommendations_first(client):
    run_id = client.post("/api/runs/manifest", json={"manifest_type": "requirements.txt", "content": MANIFEST}).json()["run_id"]
    assert client.post(f"/api/runs/{run_id}/manifests/0/plan", json={}).status_code == 409


def test_client_supplied_recommendations_are_ignored(client):
    run_id = client.post("/api/runs/manifest", json={"manifest_type": "requirements.txt", "content": MANIFEST}).json()["run_id"]
    client.post(f"/api/runs/{run_id}/manifests/0/recommendations", json={"force_deterministic": True})
    forged = {
        "recommendations": [{"package_name": "requests", "candidate_version": "99.0.0", "validation_status": "VALIDATED"}],
        "original_content": "evil==1.0\n",
    }
    plan = client.post(f"/api/runs/{run_id}/manifests/0/plan", json=forged).json()
    assert "99.0.0" not in plan["unified_diff"] and "evil" not in plan["unified_diff"]
    assert plan["original_content"] == MANIFEST


def test_validation_and_export_refused_until_approved(client):
    _, plan = _manual_plan(client)
    pid = plan["proposal_id"]
    assert client.post("/api/validation/execute", json={"proposal_id": pid}).status_code == 409
    assert client.post("/api/export", json={"proposal_id": pid}).status_code == 409
    assert client.post("/api/validation/execute", json={}).status_code == 409


def test_approval_requires_matching_diff(client):
    _, plan = _manual_plan(client)
    pid = plan["proposal_id"]
    wrong = client.post(f"/api/proposals/{pid}/approve", json={"diff_sha256": "0" * 64})
    assert wrong.status_code == 409
    ok = client.post(f"/api/proposals/{pid}/approve", json={"diff_sha256": _sha(plan["unified_diff"]), "note": "lgtm"})
    assert ok.status_code == 200 and ok.json()["approved"] is True
    state = client.get(f"/api/proposals/{pid}/approval").json()
    assert state["approved"] is True and state["note"] == "lgtm"
    export = client.post("/api/export", json={"proposal_id": pid})
    assert export.status_code == 200


def test_proposal_from_client_supplied_plan_cannot_be_approved_or_exported(client):
    legacy = client.post("/api/plan/generate", json={
        "manifest_type": "requirements.txt",
        "original_content": "requests==2.28.0\n",
        "recommendations": [{
            "package_name": "requests", "ecosystem": "python", "current_version": "2.28.0",
            "declared_specifier": "==2.28.0", "candidate_version": "2.32.3", "category": "RECOMMENDED",
            "priority": "MEDIUM", "explanation": "x", "validation_status": "VALIDATED",
        }],
    }).json()
    pid = legacy["proposal_id"]
    assert client.post(f"/api/proposals/{pid}/approve", json={"diff_sha256": _sha(legacy["unified_diff"])}).status_code == 409
    assert client.post("/api/export", json={"proposal_id": pid}).status_code == 409
    assert client.post("/api/validation/execute", json={"proposal_id": pid}).status_code == 409


def test_export_rejects_validation_run_from_another_plan(client):
    _, plan_a = _manual_plan(client)
    _, plan_b = _manual_plan(client)
    for p in (plan_a, plan_b):
        client.post(f"/api/proposals/{p['proposal_id']}/approve", json={"diff_sha256": _sha(p["unified_diff"])})
    sandbox = get_sandbox_service()
    report = ValidationReportResponse(
        execution_id="val-other", proposal_id=plan_b["proposal_id"], status=ValidationStatus.NOT_RUNNABLE,
        is_runnable=False, summary="x", commands=[], total_duration_seconds=0, image_used="python:3.11-slim",
        network_enabled=False, logs="", created_at=0.0,
    )
    with sandbox._lock:
        sandbox._executions["val-other"] = report
    resp = client.post("/api/export", json={"proposal_id": plan_a["proposal_id"], "execution_id": "val-other"})
    assert resp.status_code == 409


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path / "ws"))
    root = settings.resolved_workspace_root

    def fake_acquire(url, workspace_root, **_):
        ref = rs.parse_repository_url(url)
        dest = rs.repositories_root(workspace_root) / "fake"
        (dest / "api").mkdir(parents=True)
        (dest / "api" / "requirements.txt").write_text(MANIFEST)
        (dest / "web").mkdir()
        (dest / "web" / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}')
        (dest / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = ["requests>=2.0"]\n')
        return rs.AcquiredRepository(
            ref=ref, metadata=rs.RepositoryMetadata(full_name="octo/demo", default_branch="main", size_kb=1),
            workspace=dest, commit_sha="abc123", bytes_on_disk=10, file_count=3,
        )

    monkeypatch.setattr(rs, "acquire_repository", fake_acquire)
    return root


def test_repository_analysis_discovers_all_manifests(client, fake_repo):
    resp = client.post("/api/repositories/analyze", json={"url": "https://github.com/octo/demo"})
    assert resp.status_code == 200
    run = resp.json()
    assert run["source"] == "github" and run["has_workspace"] is True
    assert run["repository"]["full_name"] == "octo/demo" and run["repository"]["commit_sha"] == "abc123"
    by_path = {m["path"]: m for m in run["manifests"]}
    assert set(by_path) == {"api/requirements.txt", "web/package.json", "pyproject.toml"}
    assert by_path["web/package.json"]["manifest_type"] == "package.json"
    assert by_path["pyproject.toml"]["plannable"] is False
    assert by_path["pyproject.toml"]["analysis"]["total_dependencies"] == 1


def test_repository_plan_validates_against_workspace(client, fake_repo):
    run = client.post("/api/repositories/analyze", json={"url": "https://github.com/octo/demo"}).json()
    idx = next(m["index"] for m in run["manifests"] if m["path"] == "api/requirements.txt")
    py_idx = next(m["index"] for m in run["manifests"] if m["path"] == "pyproject.toml")
    client.post(f"/api/runs/{run['run_id']}/manifests/{py_idx}/recommendations", json={"force_deterministic": True})
    assert client.post(f"/api/runs/{run['run_id']}/manifests/{py_idx}/plan", json={}).status_code == 409

    client.post(f"/api/runs/{run['run_id']}/manifests/{idx}/recommendations", json={"force_deterministic": True})
    plan = client.post(f"/api/runs/{run['run_id']}/manifests/{idx}/plan", json={}).json()
    assert plan["file_path"] == "api/requirements.txt"
    pid = plan["proposal_id"]
    client.post(f"/api/proposals/{pid}/approve", json={"diff_sha256": _sha(plan["unified_diff"])})

    sandbox = get_sandbox_service()
    captured = {}

    def fake_execute(request, source_workspace=None):
        captured["ws"] = source_workspace
        return ValidationReportResponse(
            execution_id="val-ws", proposal_id=request.proposal_id, status=ValidationStatus.NOT_RUNNABLE,
            is_runnable=False, summary="x", commands=[], total_duration_seconds=0, image_used="python:3.11-slim",
            network_enabled=False, logs="", created_at=0.0,
        )

    with patch.object(sandbox, "execute_validation", side_effect=fake_execute):
        resp = client.post("/api/validation/execute", json={"proposal_id": pid})
    assert resp.status_code == 200
    assert captured["ws"] and Path(captured["ws"]).name == "fake"

    assert not Path(captured["ws"]).is_absolute()  # sandbox boundary takes root-relative paths
    import shutil
    shutil.rmtree(settings.resolved_workspace_root / captured["ws"])
    with patch.object(sandbox, "execute_validation", side_effect=fake_execute):
        gone = client.post("/api/validation/execute", json={"proposal_id": pid})
    assert gone.status_code == 410


@pytest.mark.parametrize("url,status", [
    ("http://github.com/a/b", 422),
    ("https://gitlab.com/a/b", 422),
    ("https://github.com/a/b/tree/x", 422),
])
def test_repository_url_errors_are_reported(client, url, status):
    resp = client.post("/api/repositories/analyze", json={"url": url})
    assert resp.status_code == status
    assert resp.json()["detail"]["code"] in {"invalid_url", "unsupported_host"}
