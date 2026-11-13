"""Plan creation helpers for the M12 API, job and enforcement tests."""
from __future__ import annotations

import hashlib
import json

MANIFEST = "requests==2.28.0\nurllib3==1.26.5\n"
PACKAGE_JSON = json.dumps({"name": "demo", "version": "1.0.0", "dependencies": {
    "lodash": "4.17.20", "express": "4.17.1", "axios": "0.21.1"}})


def make_plan(client, content: str = MANIFEST, manifest_type: str = "requirements.txt") -> dict:
    run = client.post("/api/runs/manifest", json={"manifest_type": manifest_type, "content": content}).json()
    client.post(f"/api/runs/{run['run_id']}/manifests/0/recommendations", json={"force_deterministic": True})
    plan = client.post(f"/api/runs/{run['run_id']}/manifests/0/plan", json={}).json()
    assert plan.get("proposal_id"), plan
    return plan


def digest(plan: dict) -> str:
    return hashlib.sha256(plan["unified_diff"].encode()).hexdigest()


def approve(client, plan: dict, **extra):
    return client.post(f"/api/proposals/{plan['proposal_id']}/approve", json={"diff_sha256": digest(plan), **extra})
