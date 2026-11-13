"""FastAPI integration tests for upgrade planning, patch generation, and approval gate endpoints."""
from __future__ import annotations

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from tests.approval_helpers import approve_registered_proposal

from src.api.main import app
from src.models.manifest import Ecosystem
from src.models.plan import ProposalStatus
from src.models.recommendation import (
    PackageUpgradeRecommendation,
    RecommendationCategory,
    RecommendationPriority,
    ValidationStatus,
)


@pytest.fixture
def client():
    return TestClient(app)


def test_api_generate_upgrade_plan_success(client: TestClient):
    """Verify POST /api/plan/generate returns structured proposal with diff and integrity check."""
    payload = {
        "manifest_type": "requirements.txt",
        "original_content": "requests==2.28.0\nflask==2.2.5\n",
        "recommendations": [
            {
                "package_name": "requests",
                "ecosystem": "python",
                "current_version": "2.28.0",
                "declared_specifier": "==2.28.0",
                "candidate_version": "2.32.3",
                "category": "RECOMMENDED",
                "priority": "MEDIUM",
                "explanation": "Security patch release",
                "validation_status": "VALIDATED",
            }
        ],
    }
    response = client.post("/api/plan/generate", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data["manifest_type"] == "requirements.txt"
    assert data["ecosystem"] == "python"
    assert data["total_planned"] == 1
    assert data["total_unchanged"] == 1
    assert data["status"] == "READY_FOR_REVIEW"
    assert data["integrity"]["is_valid"] is True
    assert "-requests==2.28.0\n" in data["unified_diff"]
    assert "+requests==2.32.3\n" in data["unified_diff"]
    assert "flask==2.2.5\n" in data["proposed_content"]


def test_api_generate_upgrade_plan_v1_alias(client: TestClient):
    """Verify endpoint is also available under /api/v1 prefix."""
    payload = {
        "manifest_type": "requirements.txt",
        "original_content": "requests==2.28.0\n",
        "recommendations": [
            {
                "package_name": "requests",
                "ecosystem": "python",
                "current_version": "2.28.0",
                "declared_specifier": "==2.28.0",
                "candidate_version": "2.32.3",
                "category": "RECOMMENDED",
                "priority": "MEDIUM",
                "explanation": "Upgrade",
                "validation_status": "VALIDATED",
            }
        ],
    }
    response = client.post("/api/v1/plan/generate", json=payload)
    assert response.status_code == 200
    assert response.json()["total_planned"] == 1


def test_api_generate_upgrade_plan_package_json(client: TestClient):
    """Verify package.json planning works through API."""
    content = json.dumps({
        "name": "sample",
        "dependencies": {"react": "^18.2.0"},
    }, indent=2) + "\n"

    payload = {
        "manifest_type": "package.json",
        "original_content": content,
        "recommendations": [
            {
                "package_name": "react",
                "ecosystem": "npm",
                "current_version": "18.2.0",
                "declared_specifier": "^18.2.0",
                "candidate_version": "18.3.1",
                "category": "RECOMMENDED",
                "priority": "MEDIUM",
                "explanation": "Upgrade React",
                "validation_status": "VALIDATED",
            }
        ],
    }
    response = client.post("/api/plan/generate", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["ecosystem"] == "npm"
    assert data["total_planned"] == 1
    assert '"react": "^18.3.1"' in data["proposed_content"]


def test_api_validate_patch_endpoint(client: TestClient):
    """Verify POST /api/plan/validate-patch standalone validation."""
    orig = "requests==2.28.0\n"
    prop = "requests==2.32.3\n"
    diff = "--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1 @@\n-requests==2.28.0\n+requests==2.32.3\n"

    payload = {
        "manifest_type": "requirements.txt",
        "original_content": orig,
        "proposed_content": prop,
        "unified_diff": diff,
        "file_path": "requirements.txt",
    }
    response = client.post("/api/plan/validate-patch", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is True
    assert data["can_apply"] is True
    assert data["can_reverse"] is True


def test_api_apply_workspace_mutation_gate_enforcement(client: TestClient):
    """Verify Criterion 4: POST /api/plan/apply rejects mutation with HTTP 403 when approved=false."""
    plan_payload = {
        "manifest_type": "requirements.txt",
        "original_content": "requests==2.28.0\n",
        "recommendations": [
            {
                "package_name": "requests",
                "ecosystem": "python",
                "current_version": "2.28.0",
                "declared_specifier": "==2.28.0",
                "candidate_version": "2.32.3",
                "category": "RECOMMENDED",
                "priority": "MEDIUM",
                "explanation": "Upgrade",
                "validation_status": "VALIDATED",
            }
        ],
    }
    gen_res = client.post("/api/plan/generate", json=plan_payload)
    assert gen_res.status_code == 200
    proposal = gen_res.json()

    # Attempt mutation without human approval
    apply_payload = {
        "proposal": proposal,
        "approved": False,
    }
    apply_res = client.post("/api/plan/apply", json=apply_payload)
    assert apply_res.status_code == 403
    assert "Human approval gate enforced" in apply_res.json()["detail"]


def test_api_apply_workspace_mutation_success(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify POST /api/plan/apply succeeds with HTTP 200 when approved=true."""
    from src.api.config import settings
    from src.api.routes.plan import get_upgrade_planner, UpgradePlannerService
    from src.services.proposal_registry import proposal_registry

    # Configure server workspace root to tmp_path
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    # Re-initialize planner instance with tmp_path workspace root
    custom_planner = UpgradePlannerService(workspace_root=tmp_path, registry=proposal_registry)
    app.dependency_overrides[get_upgrade_planner] = lambda: custom_planner

    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    plan_payload = {
        "manifest_type": "requirements.txt",
        "original_content": "requests==2.28.0\n",
        "recommendations": [
            {
                "package_name": "requests",
                "ecosystem": "python",
                "current_version": "2.28.0",
                "declared_specifier": "==2.28.0",
                "candidate_version": "2.32.3",
                "category": "RECOMMENDED",
                "priority": "MEDIUM",
                "explanation": "Upgrade",
                "validation_status": "VALIDATED",
            }
        ],
    }
    gen_res = client.post("/api/plan/generate", json=plan_payload)
    proposal = gen_res.json()

    apply_payload = {
        "proposal_id": proposal["proposal_id"],
        "approved": True,
        "target_path": "requirements.txt",
    }
    # Without a server-recorded approval the mutation is refused (FR-16).
    refused = client.post("/api/plan/apply", json=apply_payload)
    assert refused.status_code == 403
    approve_registered_proposal(proposal["proposal_id"])
    apply_res = client.post("/api/plan/apply", json=apply_payload)
    assert apply_res.status_code == 200
    data = apply_res.json()
    assert data["success"] is True
    assert data["status"] == "APPLIED"
    assert data["applied_changes_count"] == 1
    assert data["sha256"] is not None
    assert target_file.read_text(encoding="utf-8") == "requests==2.32.3\n"
    app.dependency_overrides.clear()

