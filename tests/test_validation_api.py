"""Integration tests for FastAPI validation endpoints (/api/validation and /api/v1/validation)."""
import time
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
from src.models.sandbox import ExecutionLifecycleState, ValidationStatus
from src.sandbox.runner import DockerAvailability, SandboxExecutionResult
from src.services.proposal_registry import get_proposal_registry
from tests.approval_helpers import approve_registered_proposal
from src.services.sandbox_service import SandboxService, get_sandbox_service

client = TestClient(app)



def _approved_proposal_id() -> str:
    """Register a minimal proposal and record server-side approval for it (FR-16)."""
    proposal = UpgradeProposalResponse(
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        file_path="requirements.txt",
        plan_items=[],
        original_content="requests==2.28.0\n",
        proposed_content="requests==2.32.3\n",
        unified_diff="--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1 @@\n-requests==2.28.0\n+requests==2.32.3\n",
        integrity=PatchIntegrityResult(
            is_valid=True, can_apply=True, can_reverse=True, syntax_valid=True, target_versions_match=True,
            unrelated_dependencies_unchanged=True, original_unchanged=True,
        ),
        status=ProposalStatus.READY_FOR_REVIEW,
        requires_human_approval=True,
        diagnostics=[],
        summary="Approved test proposal",
    )
    proposal_id = get_proposal_registry().store(proposal)
    approve_registered_proposal(proposal_id)
    return proposal_id


def test_validation_status_endpoint():
    """Verify GET /api/validation/status returns availability probe result."""
    response = client.get("/api/validation/status")
    assert response.status_code == 200
    data = response.json()
    assert "is_available" in data
    assert isinstance(data["is_available"], bool)


def test_validation_execute_when_docker_unavailable():
    """Verify POST /api/validation/execute truthfully returns NOT_RUNNABLE when Docker is down."""
    service = get_sandbox_service()
    mock_down = DockerAvailability(
        is_available=False,
        error_message="Docker binary not found on host.",
    )
    with patch.object(service, "get_availability", return_value=mock_down):
        with patch.object(service.runner, "check_availability", return_value=mock_down):
            payload = {
                "proposal_id": _approved_proposal_id(),
                "manifest_type": "requirements.txt",
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
    approve_registered_proposal(proposal.proposal_id)

    mock_avail = DockerAvailability(is_available=True, version="26.1.0")
    mock_exec_res = SandboxExecutionResult(
        exit_code=0,
        stdout="Bytecode compiled.\n1 passed in 0.1s\n",
        stderr="",
        duration_seconds=0.3,
        timed_out=False,
        is_runnable=True,
        container_id="dua-sandbox-mock123",
        image_digest="sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535",
    )

    service = get_sandbox_service()
    with patch.object(service.runner, "check_availability", return_value=mock_avail):
        with patch.object(service.runner, "run_command", return_value=mock_exec_res):
            payload = {
                "proposal_id": proposal.proposal_id,
            }
            response = client.post("/api/validation/execute", json=payload)
            assert response.status_code == 200
            data = response.json()
            assert data["is_runnable"] is True
            assert len(data["commands"]) > 0

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
    assert "not found" in response.json()["detail"].lower()


def test_validation_execute_unauthorized_image_returns_400():
    """CRITICAL SECURITY TEST: a client-chosen container image is refused.

    Since M11 (F13) the server selects the pinned image for an approved plan; any image_override,
    allowlisted or not, is rejected before validation starts.
    """
    for image in ("attacker/pwn-box:latest", "python:3.12-slim"):
        payload = {
            "proposal_id": _approved_proposal_id(),
            "manifest_type": "requirements.txt",
            "image_override": image,
        }
        response = client.post("/api/validation/execute", json=payload)
        assert response.status_code == 422
        assert "image_override is not accepted" in response.text


def test_validation_execute_strictly_rejects_network_enabled_override():
    """CRITICAL SECURITY TEST: Any attempt to enable container network must be rejected with 422/400."""
    payload = {
        "manifest_type": "requirements.txt",
        "network_enabled": True,
    }
    response = client.post("/api/validation/execute", json=payload)
    assert response.status_code in (400, 422)


def test_validation_execute_strictly_rejects_arbitrary_workspace_path():
    """CRITICAL SECURITY TEST: Any attempt to submit arbitrary host paths like /etc or ../ must be rejected."""
    for bad_path in ["/etc", "/", "../../etc", "C:\\Windows"]:
        payload = {
            "manifest_type": "requirements.txt",
            "workspace_path": bad_path,
        }
        response = client.post("/api/validation/execute", json=payload)
        assert response.status_code in (400, 422)


def test_validation_real_time_cancellation_and_repeated_cancel():
    """CRITICAL TEST: Verify real-time cancellation terminates container and is idempotent."""
    service = get_sandbox_service()
    mock_avail = DockerAvailability(is_available=False, error_message="Offline")
    with patch.object(service.runner, "check_availability", return_value=mock_avail):
        # Create an execution report in service
        report = service.execute_validation(
            request=service._executions.get("dummy") or MagicMock(
                proposal_id=None,
                manifest_type="requirements.txt",
                timeout_seconds=60,
                memory_limit_mb=512,
                cpu_limit="1.0",
                pids_limit=64,
                image_override=None,
                ecosystem="python",
                custom_test_cmd=None,
            )
        )
        exec_id = report.execution_id

        # Mark state as RUNNING to simulate active in-flight execution
        with service._lock:
            service._execution_states[exec_id] = ExecutionLifecycleState.RUNNING
            service._execution_containers[exec_id] = "dua-sandbox-running-test"

        # Cancel it
        cancel_res = client.post(f"/api/validation/{exec_id}/cancel")
        assert cancel_res.status_code == 200
        assert cancel_res.json()["success"] is True

        # Check updated status
        updated_rep = client.get(f"/api/validation/{exec_id}").json()
        assert updated_rep["status"] == "CANCELLED"
        assert updated_rep["lifecycle_state"] == "CANCELLED"

        # Repeated cancellation is idempotent and succeeds without error
        repeat_res = client.post(f"/api/validation/{exec_id}/cancel")
        assert repeat_res.status_code == 200
        assert repeat_res.json()["success"] is True


def test_validation_cancel_non_existent_execution_returns_404():
    """Verify cancelling non-existent execution returns 404."""
    res = client.post("/api/validation/val-non-existent-9999/cancel")
    assert res.status_code == 404


def test_validation_reports_no_tests_found_when_repo_has_no_tests(tmp_path):
    """CRITICAL CORRECTNESS TEST: When zero tests exist, report must be NO_TESTS_FOUND, not PASSED."""
    service = get_sandbox_service()
    mock_avail = DockerAvailability(is_available=True, version="26.0.0")
    # Syntax check succeeds
    mock_exec_res = SandboxExecutionResult(
        exit_code=0,
        stdout="Compiled 1 file\n",
        stderr="",
        duration_seconds=0.1,
        is_runnable=True,
    )

    with patch.object(service.runner, "check_availability", return_value=mock_avail):
        with patch.object(service.runner, "run_command", return_value=mock_exec_res):
            payload = {
                "proposal_id": _approved_proposal_id(),
                "manifest_type": "requirements.txt",
            }
            res = client.post("/api/validation/execute", json=payload)
            assert res.status_code == 200
            data = res.json()
            # Must report NO_TESTS_FOUND because zero test files were provided!
            assert data["status"] == "NO_TESTS_FOUND"
            assert "zero runnable tests were discovered" in data["summary"].lower()


def test_validation_api_v1_parity():
    """Verify /api/v1/validation/... endpoints mirror /api/validation/..."""
    res = client.get("/api/v1/validation/status")
    assert res.status_code == 200
    assert "is_available" in res.json()
