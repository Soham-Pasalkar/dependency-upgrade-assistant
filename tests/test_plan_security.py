"""Comprehensive security test suite verifying filesystem boundaries, proposal authenticity, recommendation provenance, and API semantics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.models.manifest import Ecosystem
from src.models.plan import (
    PatchIntegrityResult,
    ProposalStatus,
    SelectedPackageUpgrade,
    UpgradeAction,
    UpgradePlanRequest,
    UpgradeProposalResponse,
    WorkspaceApplyRequest,
)
from src.models.recommendation import (
    PackageUpgradeRecommendation,
    RecommendationCategory,
    RecommendationPriority,
    ValidationStatus,
)
from src.services.patch_validator import validate_workspace_destination
from src.services.proposal_registry import ProposalRegistry
from src.services.upgrade_planner import (
    HumanApprovalRequiredError,
    UpgradePlannerService,
)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def test_registry():
    return ProposalRegistry(ttl_seconds=300.0)


@pytest.fixture
def planner(tmp_path: Path, test_registry: ProposalRegistry):
    return UpgradePlannerService(workspace_root=tmp_path, registry=test_registry)


# ===========================================================================
# 1. Filesystem Boundaries & Traversal Tests
# ===========================================================================

def test_reject_absolute_path_outside_workspace(planner: UpgradePlannerService, tmp_path: Path):
    """Verify absolute paths are strictly rejected and cannot mutate arbitrary filesystem locations."""
    target_abs = "/etc/passwd"
    with pytest.raises(ValueError, match="Absolute paths are strictly prohibited"):
        validate_workspace_destination(target_abs, planner.workspace_root)


def test_reject_dot_dot_path_traversal(planner: UpgradePlannerService):
    """Verify directory traversal (..) sequences are rejected."""
    traversal_paths = [
        "../../requirements.txt",
        "sub/../../requirements.txt",
        "sub/dir/../../../requirements.txt",
        "..\\..\\requirements.txt",
    ]
    for path in traversal_paths:
        with pytest.raises(ValueError, match="traversal"):
            validate_workspace_destination(path, planner.workspace_root)


def test_reject_url_encoded_traversal(planner: UpgradePlannerService):
    """Verify URL-encoded directory traversal attempts (%2e%2e) are rejected."""
    encoded_paths = [
        "%2e%2e/requirements.txt",
        "sub/%2e%2e/%2e%2e/requirements.txt",
        "%2e%2e%2frequirements.txt",
    ]
    for path in encoded_paths:
        with pytest.raises(ValueError, match="illegal|traversal"):
            validate_workspace_destination(path, planner.workspace_root)


def test_reject_windows_drive_letter_paths(planner: UpgradePlannerService):
    """Verify Windows drive letter paths are rejected."""
    drive_paths = ["C:\\requirements.txt", "D:/sub/requirements.txt", "c:requirements.txt"]
    for path in drive_paths:
        with pytest.raises(ValueError, match="prohibited"):
            validate_workspace_destination(path, planner.workspace_root)


def test_reject_symlink_pointing_outside_workspace(tmp_path: Path):
    """Verify symlinks pointing outside workspace boundary are rejected."""
    outside_dir = tmp_path.parent / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_secret = outside_dir / "requirements.txt"
    outside_secret.write_text("secret==1.0.0\n", encoding="utf-8")

    ws_root = tmp_path / "ws"
    ws_root.mkdir(parents=True, exist_ok=True)

    # Create symlink inside ws pointing outside
    symlink_target = ws_root / "requirements.txt"
    try:
        os.symlink(str(outside_secret), str(symlink_target))
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not permitted in this environment")

    with pytest.raises(ValueError, match="symlink"):
        validate_workspace_destination("requirements.txt", ws_root)


def test_reject_unrelated_or_system_file_names(planner: UpgradePlannerService):
    """Verify writes are strictly restricted to authorized manifest filenames."""
    forbidden_files = [
        "app.py",
        "config.py",
        ".env",
        ".bashrc",
        "main.py",
        "id_rsa",
        "data.csv",
        "malicious.sh",
    ]
    for filename in forbidden_files:
        with pytest.raises(ValueError, match="Restricted target filename"):
            validate_workspace_destination(filename, planner.workspace_root)


def test_apply_fails_when_destination_does_not_exist(planner: UpgradePlannerService):
    """Verify mutation fails cleanly when target manifest does not exist on disk."""
    content = "requests==2.28.0\n"
    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.28.0",
        declared_specifier="==2.28.0",
        candidate_version="2.32.3",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.MEDIUM,
        explanation="Upgrade",
        validation_status=ValidationStatus.VALIDATED,
    )
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(manifest_type="requirements.txt", original_content=content, recommendations=[rec])
    )

    apply_req = WorkspaceApplyRequest(
        proposal_id=proposal.proposal_id,
        approved=True,
        target_path="nonexistent/requirements.txt",
    )
    resp = planner.apply_workspace_mutation(apply_req)
    assert resp.success is False
    assert resp.status == "TARGET_NOT_FOUND"


def test_apply_fails_when_on_disk_content_is_stale(planner: UpgradePlannerService, tmp_path: Path):
    """Verify stale proposal detection: aborts if file was modified after proposal generation."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.28.0",
        declared_specifier="==2.28.0",
        candidate_version="2.32.3",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.MEDIUM,
        explanation="Upgrade",
        validation_status=ValidationStatus.VALIDATED,
    )
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(manifest_type="requirements.txt", original_content="requests==2.28.0\n", recommendations=[rec])
    )

    # Simulate concurrent modification on disk
    target_file.write_text("requests==2.28.0\nflask==3.0.0\n", encoding="utf-8")

    apply_req = WorkspaceApplyRequest(
        proposal_id=proposal.proposal_id,
        approved=True,
        target_path="requirements.txt",
    )
    resp = planner.apply_workspace_mutation(apply_req)
    assert resp.success is False
    assert resp.status == "STALE_BASE_CONTENT"
    # Ensure file was not overwritten
    assert target_file.read_text(encoding="utf-8") == "requests==2.28.0\nflask==3.0.0\n"


# ===========================================================================
# 2. Proposal Authenticity & Lifecycle Management Tests
# ===========================================================================

def test_apply_rejects_unknown_proposal_id(planner: UpgradePlannerService):
    """Verify apply operation fails if proposal_id is not in server registry."""
    apply_req = WorkspaceApplyRequest(
        proposal_id="prop_unknown123456",
        approved=True,
        target_path="requirements.txt",
    )
    resp = planner.apply_workspace_mutation(apply_req)
    assert resp.success is False
    assert resp.status == "PROPOSAL_NOT_FOUND"


def test_apply_cannot_be_forged_by_client_supplying_proposal_payload(planner: UpgradePlannerService, tmp_path: Path):
    """Verify client cannot construct arbitrary proposal object and force write."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("safe==1.0.0\n", encoding="utf-8")

    forged_proposal = UpgradeProposalResponse(
        proposal_id="forged_id",
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        file_path="requirements.txt",
        plan_items=[],
        total_planned=1,
        original_content="safe==1.0.0\n",
        proposed_content="malicious==6.6.6\n",
        unified_diff="--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1 @@\n-safe==1.0.0\n+malicious==6.6.6\n",
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

    apply_req = WorkspaceApplyRequest(
        proposal_id="forged_id",
        approved=True,
        target_path="requirements.txt",
        proposal=forged_proposal,
    )
    resp = planner.apply_workspace_mutation(apply_req)
    assert resp.success is False
    assert resp.status == "PROPOSAL_NOT_FOUND"
    assert target_file.read_text(encoding="utf-8") == "safe==1.0.0\n"


def test_apply_cannot_reuse_consumed_proposal(planner: UpgradePlannerService, tmp_path: Path):
    """Verify already-consumed proposal cannot be re-applied."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.28.0",
        declared_specifier="==2.28.0",
        candidate_version="2.32.3",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.MEDIUM,
        explanation="Upgrade",
        validation_status=ValidationStatus.VALIDATED,
    )
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(manifest_type="requirements.txt", original_content="requests==2.28.0\n", recommendations=[rec])
    )

    # First apply succeeds
    apply_req = WorkspaceApplyRequest(
        proposal_id=proposal.proposal_id,
        approved=True,
        target_path="requirements.txt",
    )
    resp1 = planner.apply_workspace_mutation(apply_req)
    assert resp1.success is True
    assert resp1.status == "APPLIED"

    # Second apply on same proposal_id fails
    resp2 = planner.apply_workspace_mutation(apply_req)
    assert resp2.success is False
    assert resp2.status == "PROPOSAL_ALREADY_CONSUMED"


# ===========================================================================
# 3. Recommendation Provenance & ValidationStatus Tests
# ===========================================================================

def test_reject_arbitrary_custom_target_version(planner: UpgradePlannerService):
    """Verify custom candidate version cannot override validated candidate with an unverified version."""
    content = "requests==2.28.0\n"
    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.28.0",
        declared_specifier="==2.28.0",
        candidate_version="2.32.3",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.MEDIUM,
        explanation="Safe bump",
        validation_status=ValidationStatus.VALIDATED,
    )
    # User attempts to force version 2.35.0 (arbitrary unvalidated version)
    custom = SelectedPackageUpgrade(package_name="requests", candidate_version="2.35.0", selected=True)
    request = UpgradePlanRequest(
        manifest_type="requirements.txt",
        original_content=content,
        recommendations=[rec],
        custom_selections=[custom],
    )
    proposal = planner.create_upgrade_proposal(request)

    assert proposal.total_planned == 0
    assert proposal.total_conflicts == 1
    assert proposal.status == ProposalStatus.CONFLICT_DETECTED
    assert any("Arbitrary custom target version" in item.rationale for item in proposal.plan_items)


def test_reject_skipped_validation_status(planner: UpgradePlannerService):
    """Verify recommendations with SKIPPED validation status are rejected as untrusted."""
    content = "requests==2.28.0\n"
    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.28.0",
        declared_specifier="==2.28.0",
        candidate_version="2.32.3",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.MEDIUM,
        explanation="Skipped verification",
        validation_status=ValidationStatus.SKIPPED,
    )
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(manifest_type="requirements.txt", original_content=content, recommendations=[rec])
    )
    assert proposal.total_planned == 0
    assert proposal.total_conflicts == 1
    assert any("SKIPPED" in item.rationale for item in proposal.plan_items)


def test_reject_hallucinated_candidate_status(planner: UpgradePlannerService):
    """Verify REJECTED_HALLUCINATION status is strictly rejected."""
    content = "requests==2.28.0\n"
    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.28.0",
        declared_specifier="==2.28.0",
        candidate_version="2.99.0",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.MEDIUM,
        explanation="Hallucination",
        validation_status=ValidationStatus.REJECTED_HALLUCINATION,
    )
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(manifest_type="requirements.txt", original_content=content, recommendations=[rec])
    )
    assert proposal.total_planned == 0
    assert proposal.total_conflicts == 1


def test_reject_incompatible_candidate_status(planner: UpgradePlannerService):
    """Verify REJECTED_INCOMPATIBLE status is strictly rejected."""
    content = "requests==2.28.0\n"
    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.28.0",
        declared_specifier="==2.28.0",
        candidate_version="3.0.0",
        category=RecommendationCategory.CAUTION,
        priority=RecommendationPriority.HIGH,
        explanation="Incompatible constraint",
        validation_status=ValidationStatus.REJECTED_INCOMPATIBLE,
    )
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(manifest_type="requirements.txt", original_content=content, recommendations=[rec])
    )
    assert proposal.total_planned == 0
    assert proposal.total_conflicts == 1


def test_accept_deterministic_fallback_status(planner: UpgradePlannerService):
    """Verify FALLBACK_DETERMINISTIC status is accepted as safe."""
    content = "requests==2.28.0\n"
    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.28.0",
        declared_specifier="==2.28.0",
        candidate_version="2.32.3",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.MEDIUM,
        explanation="Deterministic SemVer bump",
        validation_status=ValidationStatus.FALLBACK_DETERMINISTIC,
    )
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(manifest_type="requirements.txt", original_content=content, recommendations=[rec])
    )
    assert proposal.total_planned == 1
    assert proposal.status == ProposalStatus.READY_FOR_REVIEW


def test_reject_ecosystem_mismatch(planner: UpgradePlannerService):
    """Verify recommendation with mismatched ecosystem (e.g. npm on python manifest) is rejected."""
    content = "requests==2.28.0\n"
    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.NPM,  # Mismatch!
        current_version="2.28.0",
        declared_specifier="==2.28.0",
        candidate_version="2.32.3",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.MEDIUM,
        explanation="Ecosystem mismatch",
        validation_status=ValidationStatus.VALIDATED,
    )
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(manifest_type="requirements.txt", original_content=content, recommendations=[rec])
    )
    assert proposal.total_planned == 0
    assert proposal.total_conflicts == 1
    assert any("Ecosystem mismatch" in item.rationale for item in proposal.plan_items)


# ===========================================================================
# 4. API Success Semantics & Approval Gate Tests
# ===========================================================================

def test_api_approved_false_strictly_raises_403(client: TestClient):
    """Verify approved=false never mutates and strictly returns HTTP 403."""
    res = client.post("/api/plan/apply", json={"proposal_id": "prop_test", "approved": False})
    assert res.status_code == 403
    assert "Human approval gate enforced" in res.json()["detail"]


def test_api_never_returns_applied_if_no_write_executed(planner: UpgradePlannerService):
    """Verify service returns NOT_APPLIED when no target path is provided."""
    content = "requests==2.28.0\n"
    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.28.0",
        declared_specifier="==2.28.0",
        candidate_version="2.32.3",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.MEDIUM,
        explanation="Upgrade",
        validation_status=ValidationStatus.VALIDATED,
    )
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(manifest_type="requirements.txt", original_content=content, recommendations=[rec])
    )

    # Calling with empty target path and empty proposal file_path
    proposal.file_path = ""
    apply_req = WorkspaceApplyRequest(
        proposal_id=proposal.proposal_id,
        approved=True,
        target_path="",
    )
    resp = planner.apply_workspace_mutation(apply_req)
    assert resp.success is False
    assert resp.status == "NOT_APPLIED"
