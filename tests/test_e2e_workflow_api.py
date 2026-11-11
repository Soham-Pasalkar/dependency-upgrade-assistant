"""End-to-end integration tests verifying the complete 10-step user journey via FastAPI REST API.

Journey verified:
1. Ingest manifest (raw editor / upload).
2. Analyze dependencies and evaluate constraints.
3. Generate and inspect upgrade recommendations.
4. Synthesize deterministic upgrade plan and unified diff.
5. Review proposed patch and verify patch integrity.
6. Enforce explicit human-in-the-loop approval gate.
7. Execute sandboxed validation.
8. Receive truthful validation report and command logs.
9. Generate verified export deliverables.
10. Download .patch, summary.json, execution.log, checksums.sha256, and bundle.zip.
"""
from __future__ import annotations

import io
import json
import zipfile
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.models.manifest import Ecosystem


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """FastAPI TestClient fixture."""
    with TestClient(app) as test_client:
        yield test_client


PYTHON_MANIFEST = (
    "# Production dependencies\n"
    "requests==2.28.0\n"
    "urllib3==1.26.5\n"
    "certifi>=2022.12.7\n"
)

PACKAGE_JSON_MANIFEST = json.dumps(
    {
        "name": "sample-service",
        "version": "1.0.0",
        "dependencies": {
            "express": "^4.17.1",
            "lodash": "4.17.20",
        },
        "devDependencies": {
            "jest": "^27.0.0",
        },
    },
    indent=2,
)


def test_complete_e2e_journey_python_requirements(client: TestClient):
    """Verify complete 10-step upgrade lifecycle for a Python requirements.txt manifest."""
    # Step 1 & 2: Manifest Ingestion and Dependency Analysis
    analysis_resp = client.post(
        "/api/manifest/analyze",
        json={
            "manifest_type": "requirements.txt",
            "content": PYTHON_MANIFEST,
        },
    )
    assert analysis_resp.status_code == 200
    analysis_data = analysis_resp.json()
    assert analysis_data["ecosystem"] == "python"
    assert analysis_data["total_dependencies"] == 3
    assert len(analysis_data["dependencies"]) == 3

    # Step 3: Upgrade Recommendations Generation
    recs_resp = client.post(
        "/api/recommendations/generate",
        json={
            "manifest_analysis": analysis_data,
            "force_deterministic": True,
        },
    )
    assert recs_resp.status_code == 200
    recs_data = recs_resp.json()
    assert len(recs_data["recommendations"]) >= 1
    recommendations = recs_data["recommendations"]

    # Step 4 & 5: Upgrade Plan & Patch Synthesis
    plan_resp = client.post(
        "/api/plan/generate",
        json={
            "manifest_type": "requirements.txt",
            "original_content": PYTHON_MANIFEST,
            "recommendations": recommendations,
            "file_path": "requirements.txt",
        },
    )
    assert plan_resp.status_code == 200
    proposal = plan_resp.json()
    proposal_id = proposal["proposal_id"]
    assert proposal_id.startswith("prop_")
    assert proposal["total_planned"] >= 1
    assert proposal["integrity"]["is_valid"] is True
    assert proposal["unified_diff"] != ""
    assert "--- a/requirements.txt" in proposal["unified_diff"]
    assert "+++ b/requirements.txt" in proposal["unified_diff"]

    # Step 6: Explicit Human Approval Gate Enforcement
    # Verify rejection when approved=False
    unapproved_resp = client.post(
        "/api/plan/apply",
        json={
            "proposal_id": proposal_id,
            "approved": False,
        },
    )
    assert unapproved_resp.status_code == 403
    assert "approval" in unapproved_resp.json()["detail"].lower()

    # Step 7 & 8: Sandboxed Validation Execution & Reporting
    val_resp = client.post(
        "/api/validation/execute",
        json={
            "proposal_id": proposal_id,
            "manifest_type": "requirements.txt",
            "timeout_seconds": 60,
        },
    )
    assert val_resp.status_code == 200
    val_data = val_resp.json()
    execution_id = val_data["execution_id"]
    assert execution_id.startswith("val-")
    # Status is truthful: either PASSED or NOT_RUNNABLE if Docker is unavailable
    assert val_data["status"] in ("PASSED", "NOT_RUNNABLE", "NO_TESTS_FOUND")

    # Verify execution report retrieval
    exec_report_resp = client.get(f"/api/validation/{execution_id}")
    assert exec_report_resp.status_code == 200
    assert exec_report_resp.json()["execution_id"] == execution_id

    # Verify logs retrieval
    exec_logs_resp = client.get(f"/api/validation/{execution_id}/logs")
    assert exec_logs_resp.status_code == 200
    assert "logs" in exec_logs_resp.json()

    # Step 9: Deliverables Export Generation
    export_resp = client.post(
        "/api/export",
        json={
            "proposal_id": proposal_id,
            "execution_id": execution_id,
            "include_logs": True,
            "custom_notes": "E2E automated verification run",
        },
    )
    assert export_resp.status_code == 200
    export_data = export_resp.json()
    export_id = export_data["export_id"]
    assert export_id.startswith("exp_")
    assert export_data["sanitized"] is True
    assert export_data["secret_scan_passed"] is True

    items = {item["artifact_type"]: item for item in export_data["items"]}
    assert "patch" in items
    assert "summary_json" in items
    assert "execution_log" in items
    assert "checksums" in items
    assert "bundle_zip" in items

    # Step 10: Artifact Downloads and Checksum Verification
    # 1. Download Patch
    patch_file_resp = client.get(f"/api/export/{export_id}/download/patch")
    assert patch_file_resp.status_code == 200
    assert "text/x-diff" in patch_file_resp.headers["content-type"]
    assert "X-Checksum-SHA256" in patch_file_resp.headers
    assert patch_file_resp.headers["X-Checksum-SHA256"] == items["patch"]["sha256"]
    assert "Dependency Upgrade Assistant 2.0 Export Patch" in patch_file_resp.text

    # 2. Download JSON Summary
    summary_file_resp = client.get(f"/api/export/{export_id}/download/summary_json")
    assert summary_file_resp.status_code == 200
    assert "application/json" in summary_file_resp.headers["content-type"]
    assert summary_file_resp.headers["X-Checksum-SHA256"] == items["summary_json"]["sha256"]
    summary_obj = summary_file_resp.json()
    assert summary_obj["dua_version"] == "2.0.0"
    assert summary_obj["proposal_id"] == proposal_id
    assert summary_obj["security_audit"]["zero_secrets_verified"] is True

    # 3. Download Execution Log
    log_file_resp = client.get(f"/api/export/{export_id}/download/execution_log")
    assert log_file_resp.status_code == 200
    assert "text/plain" in log_file_resp.headers["content-type"]
    assert log_file_resp.headers["X-Checksum-SHA256"] == items["execution_log"]["sha256"]

    # 4. Download SHA-256 Checksums Manifest
    chk_file_resp = client.get(f"/api/export/{export_id}/download/checksums")
    assert chk_file_resp.status_code == 200
    assert items["patch"]["sha256"] in chk_file_resp.text

    # 5. Download Deliverable ZIP Bundle
    zip_file_resp = client.get(f"/api/export/{export_id}/download/bundle_zip")
    assert zip_file_resp.status_code == 200
    assert "application/zip" in zip_file_resp.headers["content-type"]
    assert zip_file_resp.headers["X-Checksum-SHA256"] == items["bundle_zip"]["sha256"]

    # Validate ZIP integrity
    zip_bytes = io.BytesIO(zip_file_resp.content)
    with zipfile.ZipFile(zip_bytes, "r") as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        assert any(n.endswith(".patch") for n in names)
        assert "summary.json" in names
        assert "execution.log" in names
        assert "checksums.sha256" in names


def test_complete_e2e_journey_npm_package_json(client: TestClient):
    """Verify complete 10-step upgrade lifecycle for a Node.js package.json manifest."""
    # Step 1 & 2: Ingest and Analyze Node.js Manifest
    analysis_resp = client.post(
        "/api/manifest/analyze",
        json={
            "manifest_type": "package.json",
            "content": PACKAGE_JSON_MANIFEST,
        },
    )
    assert analysis_resp.status_code == 200
    analysis_data = analysis_resp.json()
    assert analysis_data["ecosystem"] == "npm"
    assert analysis_data["total_dependencies"] == 3

    # Step 3: Recommendations Generation
    recs_resp = client.post(
        "/api/recommendations/generate",
        json={
            "manifest_analysis": analysis_data,
            "force_deterministic": True,
        },
    )
    assert recs_resp.status_code == 200
    recs_data = recs_resp.json()
    recommendations = recs_data["recommendations"]

    # Step 4 & 5: Upgrade Plan & Patch Synthesis
    plan_resp = client.post(
        "/api/plan/generate",
        json={
            "manifest_type": "package.json",
            "original_content": PACKAGE_JSON_MANIFEST,
            "recommendations": recommendations,
            "file_path": "package.json",
        },
    )
    assert plan_resp.status_code == 200
    proposal = plan_resp.json()
    proposal_id = proposal["proposal_id"]
    assert proposal_id.startswith("prop_")
    assert proposal["integrity"]["is_valid"] is True
    assert proposal["unified_diff"] != ""
    assert "--- a/package.json" in proposal["unified_diff"]
    assert "+++ b/package.json" in proposal["unified_diff"]

    # Step 6: Human Approval Gate
    unapproved_resp = client.post(
        "/api/plan/apply",
        json={"proposal_id": proposal_id, "approved": False},
    )
    assert unapproved_resp.status_code == 403

    # Step 7 & 8: Sandboxed Validation
    val_resp = client.post(
        "/api/validation/execute",
        json={
            "proposal_id": proposal_id,
            "manifest_type": "package.json",
            "timeout_seconds": 60,
        },
    )
    assert val_resp.status_code == 200
    val_data = val_resp.json()
    execution_id = val_data["execution_id"]

    # Step 9: Export Deliverables
    export_resp = client.post(
        "/api/export",
        json={
            "proposal_id": proposal_id,
            "execution_id": execution_id,
            "include_logs": True,
            "custom_notes": "Node.js package.json E2E test",
        },
    )
    assert export_resp.status_code == 200
    export_data = export_resp.json()
    export_id = export_data["export_id"]

    # Step 10: Download and Verify ZIP bundle
    zip_resp = client.get(f"/api/export/{export_id}/download/bundle_zip")
    assert zip_resp.status_code == 200
    assert "application/zip" in zip_resp.headers["content-type"]

    zip_bytes = io.BytesIO(zip_resp.content)
    with zipfile.ZipFile(zip_bytes, "r") as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        assert any(n.endswith(".patch") for n in names)
        assert "summary.json" in names
        assert "checksums.sha256" in names
