"""Plans survive backend restarts and in-memory expiry (audit F4), within PLAN_RETENTION_HOURS."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.api.config import settings
from src.api.main import app
from src.services.proposal_registry import ProposalLifecycleState, get_proposal_registry
from src.services.run_store import RunStore

MANIFEST = "requests==2.28.0\nurllib3==1.26.5\n"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _approved_plan(client):
    run = client.post("/api/runs/manifest", json={"manifest_type": "requirements.txt", "content": MANIFEST}).json()
    client.post(f"/api/runs/{run['run_id']}/manifests/0/recommendations", json={"force_deterministic": True})
    plan = client.post(f"/api/runs/{run['run_id']}/manifests/0/plan", json={}).json()
    digest = hashlib.sha256(plan["unified_diff"].encode()).hexdigest()
    assert client.post(f"/api/proposals/{plan['proposal_id']}/approve", json={"diff_sha256": digest}).status_code == 200
    return plan


def _simulate_restart():
    get_proposal_registry().clear()


def test_export_works_after_restart(client):
    plan = _approved_plan(client)
    _simulate_restart()
    resp = client.post("/api/export", json={"proposal_id": plan["proposal_id"]})
    assert resp.status_code == 200
    restored = get_proposal_registry().get(plan["proposal_id"])
    assert restored is not None and restored.proposal.unified_diff == plan["unified_diff"]


def test_approval_can_be_recorded_after_restart(client):
    run = client.post("/api/runs/manifest", json={"manifest_type": "requirements.txt", "content": MANIFEST}).json()
    client.post(f"/api/runs/{run['run_id']}/manifests/0/recommendations", json={"force_deterministic": True})
    plan = client.post(f"/api/runs/{run['run_id']}/manifests/0/plan", json={}).json()
    _simulate_restart()
    digest = hashlib.sha256(plan["unified_diff"].encode()).hexdigest()
    assert client.post(f"/api/proposals/{plan['proposal_id']}/approve", json={"diff_sha256": digest}).status_code == 200


def test_validation_refusal_still_applies_after_restart(client):
    run = client.post("/api/runs/manifest", json={"manifest_type": "requirements.txt", "content": MANIFEST}).json()
    client.post(f"/api/runs/{run['run_id']}/manifests/0/recommendations", json={"force_deterministic": True})
    plan = client.post(f"/api/runs/{run['run_id']}/manifests/0/plan", json={}).json()
    _simulate_restart()
    assert client.post("/api/validation/execute", json={"proposal_id": plan["proposal_id"]}).status_code == 409


def test_plans_expire_after_retention(client, monkeypatch):
    plan = _approved_plan(client)
    _simulate_restart()
    monkeypatch.setattr(settings, "PLAN_RETENTION_HOURS", 1e-9)
    assert client.post("/api/export", json={"proposal_id": plan["proposal_id"]}).status_code in (404, 410)
    assert client.post("/api/validation/execute", json={"proposal_id": plan["proposal_id"]}).status_code == 410


def test_consumed_plan_is_restored_as_consumed(isolated_run_store, client):
    plan = _approved_plan(client)
    isolated_run_store.mark_consumed(plan["proposal_id"])
    _simulate_restart()
    from src.services.approval_service import ensure_registered

    restored = ensure_registered(plan["proposal_id"], isolated_run_store, get_proposal_registry())
    assert restored.state == ProposalLifecycleState.CONSUMED
    claim = get_proposal_registry().claim_for_application(plan["proposal_id"])
    assert claim.success is False and claim.status == "PROPOSAL_ALREADY_CONSUMED"


def test_legacy_database_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE dua_runs (id TEXT PRIMARY KEY, source TEXT NOT NULL, repository TEXT, "
                 "workspace_dir TEXT, skipped TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL)")
    conn.execute("CREATE TABLE dua_proposals (proposal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, idx INTEGER NOT NULL, "
                 "diff_sha256 TEXT NOT NULL, created_at REAL NOT NULL)")
    conn.commit()
    conn.close()
    store = RunStore(db_path=str(db))
    assert store.get_binding("missing") is None
    cols = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(dua_proposals)")}
    assert {"proposal_json", "consumed_at"} <= cols


def test_plan_altered_after_approval_is_refused(client):
    """Audit scenario 15: if the stored diff no longer matches the approved checksum, nothing proceeds."""
    plan = _approved_plan(client)
    stored = get_proposal_registry().get(plan["proposal_id"])
    stored.proposal.unified_diff += "+injected==6.6.6\n"
    resp = client.post("/api/validation/execute", json={"proposal_id": plan["proposal_id"]})
    assert resp.status_code == 409 and "changed after it was approved" in resp.json()["detail"]
    assert client.post("/api/export", json={"proposal_id": plan["proposal_id"]}).status_code == 409
