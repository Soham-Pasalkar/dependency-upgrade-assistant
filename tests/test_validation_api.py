"""Integration tests for FastAPI validation endpoints (/api/validation and /api/v1/validation)."""
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.models.manifest import Ecosystem
from src.models.plan import (
    PatchIntegrityResult,
    ProposalStatus,
    UpgradeProposalResponse,
)
from src.models.sandbox import ValidationStatus
from src.sandbox.runner import DockerAvailability, SandboxExecutionResult
from src.services.proposal_registry import get_proposal_registry
from src.services.sandbox_service import SandboxService, get_sandbox_service

client = TestClient(app)


def test_validation_status_endpoint():
    """Verify GET /api/validation/status returns availability probe result."""
    response = client.get("/api/validation/status")
    assert response.status_code == 200
    data = response.json()
    assert "is_available" in data
    assert isinstance(data["is_available"], bool)


def test_validation_execute_when_docker_unavailable():
    """Verify POST /api/validation/execute truthfully returns NOT_RUNNABLE when Docker is down."""
    with patch.object(
        SandboxService,
        "get_availability",
        return_value=DockerAvailability(
            is_available=False,
            error_message="Docker binary not found on host.",
        ),
    ):
        payload = {
            "manifest_type": "requirements.txt",
            "network_enabled": False,
        }
        response = client.post("/api/validation/execute", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "NOT_RUNNABLE"
        assert data["is_runnable"] is False
        assert "Docker sandbox unavailable" in data["summary"]
        assert len(data["commands"]) == 0


def test_validation_execute_with_proposal_success():
    """Verify POST /api/validation/execute succeeds and executes tests inside sandbox."""
    # Register a proposal in ProposalRegistry
    registry = get_proposal_registry()
    proposal = UpgradeProposalResponse(
        proposal_id="prop-test-api-001",
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        file_path="requirements.txt",
        plan_items=[],
        total_planned=1,
        total_unchanged=0,
        total_conflicts=0,
        original_content="requests==2.28.0\n",
        proposed_content="requests==2.32.0\n",
        unified_diff="--- a/requirements.txt\n+++ b/requirements.txt\n",
        integrity=PatchIntegrityResult(
            is_valid=True,
            can_apply=True,
            can_reverse=True,
            syntax_valid=True,
            target_versions_match=True,
            unrelated_dependencies_unchanged=True,
            original_unchanged=True,
            checks_passed=["syntax"],
            checks_failed=[],
            errors=[],
            warnings=[],
        ),
        status=ProposalStatus.READY_FOR_REVIEW,
        requires_human_approval=True,
        diagnostics=[],
        summary="Test proposal",
    )
    registry.store(proposal)

    # Mock runner to simulate active docker daemon and passed tests
    mock_avail = DockerAvailability(is_available=True, version="26.1.0")
    mock_exec_res = SandboxExecutionResult(
        exit_code=0,
        stdout="Bytecode compiled.\n1 passed in 0.1s\n",
        stderr="",
        duration_seconds=0.3,
        timed_out=False,
        is_runnable=True,
        container_id="dua-sandbox-mock123",
    )

    service = get_sandbox_service()
    with patch.object(service.runner, "check_availability", return_value=mock_avail):
        with patch.object(service.runner, "run_command", return_value=mock_exec_res):
            payload = {
                "proposal_id": proposal.proposal_id,
                "timeout_seconds": 30,
            }
            response = client.post("/api/validation/execute", json=payload)
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "PASSED"
            assert data["is_runnable"] is True
            assert len(data["commands"]) > 0
            assert "passed successfully" in data["summary"].lower()

            # Verify report can be fetched by ID
            exec_id = data["execution_id"]
            rep_res = client.get(f"/api/validation/{exec_id}")
            assert rep_res.status_code == 200
            assert rep_res.json()["execution_id"] == exec_id

            # Verify logs can be fetched by ID
            log_res = client.get(f"/api/validation/{exec_id}/logs")
            assert log_res.status_code == 200
            assert "Bytecode compiled" in log_res.json()["logs"]


def test_validation_execute_unknown_proposal_returns_404():
    """Verify proposal_id not present in registry returns 404."""
    payload = {
        "proposal_id": "non-existent-prop-9999",
    }
    response = client.post("/api/validation/execute", json=payload)
    assert response.status_code == 404
    assert "not found in registry" in response.json()["detail"].lower()


def test_validation_execute_unauthorized_image_returns_400():
    """CRITICAL SECURITY TEST: Requesting arbitrary/unapproved container image returns 400 Bad Request."""
    payload = {
        "manifest_type": "requirements.txt",
        "image_override": "attacker/pwn-box:latest",
    }
    response = client.post("/api/validation/execute", json=payload)
    assert response.status_code == 400
    assert "unauthorized container image" in response.json()["detail"].lower()


def test_validation_cancel_endpoint():
    """Verify POST /api/validation/{execution_id}/cancel cancels in-progress execution."""
    service = get_sandbox_service()
    mock_avail = DockerAvailability(is_available=False, error_message="Offline")
    with patch.object(service.runner, "check_availability", return_value=mock_avail):
        # Create an execution report in service
        report = service.execute_validation(
            request=service._executions.get("dummy") or MagicMock(proposal_id=None, manifest_type="requirements.txt", network_enabled=False, memory_limit_mb=512, cpu_limit="1.0", pids_limit=64, timeout_seconds=60, image_override=None, ecosystem="python", custom_test_cmd=None, workspace_path=None)
        )
        exec_id = report.execution_id

        # Cancel it
        cancel_res = client.post(f"/api/validation/{exec_id}/cancel")
        assert cancel_res.status_code == 200
        assert cancel_res.json()["success"] is True

        # Check updated status
        updated_rep = client.get(f"/api/validation/{exec_id}").json()
        assert updated_rep["status"] == "CANCELLED"


def test_validation_api_v1_parity():
    """Verify /api/v1/validation/... endpoints mirror /api/validation/..."""
    res = client.get("/api/v1/validation/status")
    assert res.status_code == 200
    assert "is_available" in res.json()
