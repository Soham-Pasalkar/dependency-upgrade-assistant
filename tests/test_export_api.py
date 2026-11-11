"""Integration tests for Export FastAPI endpoints, download streams, and checksum headers."""
import json
import zipfile
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.models.export import ArtifactType
from src.models.manifest import DependencyType, Ecosystem
from src.models.plan import (
    PatchIntegrityResult,
    PlannedUpgradeItem,
    ProposalStatus,
    UpgradeAction,
    UpgradeProposalResponse,
)
from src.models.sandbox import (
    ValidationCommandResult,
    ValidationReportResponse,
    ValidationStatus,
)
from src.services.proposal_registry import get_proposal_registry
from src.services.sandbox_service import get_sandbox_service


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Provide a TestClient for FastAPI."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_registered_proposal() -> str:
    """Register a realistic sample proposal in the server ProposalRegistry."""
    registry = get_proposal_registry()
    plan_item = PlannedUpgradeItem(
        package_name="urllib3",
        ecosystem=Ecosystem.PYTHON,
        current_version="1.26.5",
        target_version="2.2.2",
        declared_specifier="==1.26.5",
        proposed_specifier="==2.2.2",
        action=UpgradeAction.UPGRADE,
        rationale="Upgrade to modern 2.x release",
        line_number=2,
        dependency_type=DependencyType.RUNTIME,
    )
    diff_text = (
        "--- a/requirements.txt\n"
        "+++ b/requirements.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " requests==2.32.3\n"
        "-urllib3==1.26.5\n"
        "+urllib3==2.2.2\n"
    )
    integrity = PatchIntegrityResult(
        is_valid=True,
        can_apply=True,
        can_reverse=True,
        syntax_valid=True,
        target_versions_match=True,
        unrelated_dependencies_unchanged=True,
        original_unchanged=True,
        checks_passed=["syntax_check"],
    )
    proposal = UpgradeProposalResponse(
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        file_path="requirements.txt",
        plan_items=[plan_item],
        total_planned=1,
        total_unchanged=1,
        total_conflicts=0,
        original_content="requests==2.32.3\nurllib3==1.26.5\n",
        proposed_content="requests==2.32.3\nurllib3==2.2.2\n",
        unified_diff=diff_text,
        integrity=integrity,
        status=ProposalStatus.READY_FOR_REVIEW,
        requires_human_approval=True,
    )
    proposal_id = registry.store(proposal)
    return proposal_id


@pytest.fixture
def sample_registered_execution(sample_registered_proposal: str) -> str:
    """Register a realistic validation execution report in SandboxService."""
    sandbox = get_sandbox_service()
    cmd = ValidationCommandResult(
        command_name="pytest",
        command_args=["pytest", "-v", "tests/"],
        status=ValidationStatus.PASSED,
        exit_code=0,
        stdout="==== 42 passed in 1.2s ====",
        stderr="",
        duration_seconds=1.2,
    )
    report = ValidationReportResponse(
        execution_id="exec_api_test_999",
        proposal_id=sample_registered_proposal,
        status=ValidationStatus.PASSED,
        is_runnable=True,
        summary="All tests passed in container.",
        commands=[cmd],
        total_duration_seconds=1.2,
        image_used="python:3.11-slim",
        image_digest="sha256:pin999",
        network_enabled=False,
        logs="[pytest] 42 passed in 1.2s\n",
        created_at=1731301000.0,
    )
    with sandbox._lock:
        sandbox._executions[report.execution_id] = report
    return report.execution_id


def test_post_export_creates_bundle(client: TestClient, sample_registered_proposal: str, sample_registered_execution: str):
    """Verify POST /api/export returns 200 with deliverable items and checksums."""
    payload = {
        "proposal_id": sample_registered_proposal,
        "execution_id": sample_registered_execution,
        "include_logs": True,
        "custom_notes": "Security approved",
    }
    resp = client.post("/api/export", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["export_id"].startswith("exp_")
    assert data["proposal_id"] == sample_registered_proposal
    assert data["execution_id"] == sample_registered_execution
    assert data["sanitized"] is True
    assert data["secret_scan_passed"] is True
    assert data["validation_status"] == "PASSED"

    items = data["items"]
    assert len(items) == 5
    types = {item["artifact_type"] for item in items}
    assert types == {"patch", "summary_json", "execution_log", "checksums", "bundle_zip"}

    # Verify SHA-256 digests exist for all items
    for item in items:
        assert len(item["sha256"]) == 64
        assert item["download_url"].startswith(f"/api/export/{data['export_id']}/download/")


def test_get_export_metadata(client: TestClient, sample_registered_proposal: str):
    """Verify GET /api/export/{export_id} returns stored metadata."""
    create_resp = client.post("/api/export", json={"proposal_id": sample_registered_proposal})
    assert create_resp.status_code == 200
    export_id = create_resp.json()["export_id"]

    get_resp = client.get(f"/api/export/{export_id}")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["export_id"] == export_id
    assert data["validation_status"] == "NOT_PERFORMED"


def test_download_patch_endpoint(client: TestClient, sample_registered_proposal: str):
    """Verify GET /api/export/{export_id}/download/patch streams .patch with checksum header."""
    create_resp = client.post("/api/export", json={"proposal_id": sample_registered_proposal})
    export_id = create_resp.json()["export_id"]

    resp = client.get(f"/api/export/{export_id}/download/patch")
    assert resp.status_code == 200
    assert "text/x-diff" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    assert "X-Checksum-SHA256" in resp.headers
    assert len(resp.headers["X-Checksum-SHA256"]) == 64
    assert "-urllib3==1.26.5" in resp.text
    assert "+urllib3==2.2.2" in resp.text


def test_download_summary_json_endpoint(client: TestClient, sample_registered_proposal: str):
    """Verify GET /api/export/{export_id}/download/summary_json returns valid JSON."""
    create_resp = client.post("/api/export", json={"proposal_id": sample_registered_proposal})
    export_id = create_resp.json()["export_id"]

    resp = client.get(f"/api/export/{export_id}/download/summary_json")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    summary = resp.json()
    assert summary["dua_version"] == "2.0.0"
    assert summary["proposal_id"] == sample_registered_proposal


def test_download_bundle_zip_endpoint(client: TestClient, sample_registered_proposal: str):
    """Verify GET /api/export/{export_id}/download/bundle_zip streams valid ZIP archive."""
    create_resp = client.post("/api/export", json={"proposal_id": sample_registered_proposal})
    export_id = create_resp.json()["export_id"]

    resp = client.get(f"/api/export/{export_id}/download/bundle_zip")
    assert resp.status_code == 200
    assert "application/zip" in resp.headers["content-type"]

    # Verify content as a valid zip archive
    import io
    zip_bytes = io.BytesIO(resp.content)
    with zipfile.ZipFile(zip_bytes, "r") as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        assert "summary.json" in names
        assert "checksums.sha256" in names


def test_download_checksums_endpoint(client: TestClient, sample_registered_proposal: str):
    """Verify GET /api/export/{export_id}/download/checksums streams SHA-256 manifest."""
    create_resp = client.post("/api/export", json={"proposal_id": sample_registered_proposal})
    export_id = create_resp.json()["export_id"]

    resp = client.get(f"/api/export/{export_id}/download/checksums")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "execution.log" in resp.text
    assert "summary.json" in resp.text


def test_export_nonexistent_proposal_returns_404(client: TestClient):
    """Verify 404 when attempting to export an unknown or expired proposal."""
    resp = client.post("/api/export", json={"proposal_id": "prop_nonexistent_123"})
    assert resp.status_code == 404
    assert "not found or has expired" in resp.json()["detail"]


def test_export_with_secret_leakage_returns_422(client: TestClient):
    """Verify that detected secrets in proposal fail with 422 Unprocessable Content."""
    registry = get_proposal_registry()
    dummy_key = "AKIA" + "1234567890ABCDEF"  # aidlc:allow-secret
    leaked_proposal = UpgradeProposalResponse(
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        file_path="requirements.txt",
        original_content="requests==2.32.3\n",
        proposed_content="requests==2.32.3\n",
        unified_diff=f"+API_KEY = '{dummy_key}'\n",  # aidlc:allow-secret
        integrity=PatchIntegrityResult(
            is_valid=True,
            can_apply=True,
            can_reverse=True,
            syntax_valid=True,
            target_versions_match=True,
            unrelated_dependencies_unchanged=True,
            original_unchanged=True,
        ),
        status=ProposalStatus.READY_FOR_REVIEW,
    )
    prop_id = registry.store(leaked_proposal)

    resp = client.post("/api/export", json={"proposal_id": prop_id})
    assert resp.status_code == 422
    assert "Pre-export secret verification failed" in resp.json()["detail"]
    assert dummy_key not in resp.json()["detail"]  # aidlc:allow-secret


def test_download_invalid_artifact_type_returns_400(client: TestClient, sample_registered_proposal: str):
    """Verify 400 when requesting unsupported artifact type."""
    create_resp = client.post("/api/export", json={"proposal_id": sample_registered_proposal})
    export_id = create_resp.json()["export_id"]

    resp = client.get(f"/api/export/{export_id}/download/arbitrary_type")
    assert resp.status_code == 400
    assert "Invalid artifact type" in resp.json()["detail"]


def test_download_path_traversal_rejected_with_404(client: TestClient):
    """Verify path traversal in export_id is safely rejected."""
    resp = client.get("/api/export/../../etc/passwd/download/patch")
    assert resp.status_code in (404, 400)


def test_api_v1_route_parity(client: TestClient, sample_registered_proposal: str):
    """Verify endpoints function under /api/v1/ prefix for architecture spec compliance."""
    resp = client.post("/api/v1/export", json={"proposal_id": sample_registered_proposal})
    assert resp.status_code == 200
    export_id = resp.json()["export_id"]

    get_resp = client.get(f"/api/v1/export/{export_id}")
    assert get_resp.status_code == 200
