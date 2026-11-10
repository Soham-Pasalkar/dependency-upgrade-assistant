"""Unit tests for UpgradePlannerService covering planning, conflict detection, deterministic behavior, and approval gates."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from src.models.manifest import Ecosystem
from src.models.plan import (
    ProposalStatus,
    SelectedPackageUpgrade,
    UpgradeAction,
    UpgradePlanRequest,
    WorkspaceApplyRequest,
)
from src.models.recommendation import (
    PackageUpgradeRecommendation,
    RecommendationCategory,
    RecommendationPriority,
    ValidationStatus,
)
from src.services.upgrade_planner import (
    HumanApprovalRequiredError,
    UpgradePlannerService,
)


@pytest.fixture
def planner():
    return UpgradePlannerService()


def test_planner_single_dependency_upgrade(planner: UpgradePlannerService):
    """Verify planner produces upgrade plan and patch for a single valid recommendation."""
    content = "requests==2.28.0\nflask==2.2.5\n"
    recs = [
        PackageUpgradeRecommendation(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            declared_specifier="==2.28.0",
            candidate_version="2.32.3",
            category=RecommendationCategory.RECOMMENDED,
            priority=RecommendationPriority.MEDIUM,
            explanation="Safe patch upgrade.",
            validation_status=ValidationStatus.VALIDATED,
        )
    ]
    request = UpgradePlanRequest(
        manifest_type="requirements.txt",
        original_content=content,
        recommendations=recs,
    )
    proposal = planner.create_upgrade_proposal(request)

    assert proposal.total_planned == 1
    assert proposal.total_unchanged == 1
    assert proposal.total_conflicts == 0
    assert proposal.status == ProposalStatus.READY_FOR_REVIEW
    assert proposal.integrity.is_valid is True
    assert "-requests==2.28.0\n" in proposal.unified_diff
    assert "+requests==2.32.3\n" in proposal.unified_diff
    assert "flask==2.2.5" in proposal.proposed_content


def test_planner_selected_packages_filter(planner: UpgradePlannerService):
    """Verify user can selectively upgrade only a chosen subset of recommended packages."""
    content = "requests==2.28.0\nflask==2.2.5\nurllib3==1.26.0\n"
    recs = [
        PackageUpgradeRecommendation(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            declared_specifier="==2.28.0",
            candidate_version="2.32.3",
            category=RecommendationCategory.RECOMMENDED,
            priority=RecommendationPriority.MEDIUM,
            explanation="Upgrade requests.",
            validation_status=ValidationStatus.VALIDATED,
        ),
        PackageUpgradeRecommendation(
            package_name="flask",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.2.5",
            declared_specifier="==2.2.5",
            candidate_version="3.1.0",
            category=RecommendationCategory.CAUTION,
            priority=RecommendationPriority.HIGH,
            explanation="Upgrade flask.",
            validation_status=ValidationStatus.VALIDATED,
        ),
    ]
    # User selects only requests!
    request = UpgradePlanRequest(
        manifest_type="requirements.txt",
        original_content=content,
        recommendations=recs,
        selected_packages=["requests"],
    )
    proposal = planner.create_upgrade_proposal(request)

    assert proposal.total_planned == 1
    assert proposal.total_unchanged == 2
    # requests was updated, flask and urllib3 remained untouched
    assert "requests==2.32.3\n" in proposal.proposed_content
    assert "flask==2.2.5\n" in proposal.proposed_content
    assert "urllib3==1.26.0\n" in proposal.proposed_content


def test_planner_rejects_hallucinated_or_incompatible_recommendation(planner: UpgradePlannerService):
    """Verify recommendation flagged as REJECTED_HALLUCINATION is marked as conflict and not applied."""
    content = "requests==2.28.0\n"
    recs = [
        PackageUpgradeRecommendation(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            declared_specifier="==2.28.0",
            candidate_version="2.99.0",
            category=RecommendationCategory.CAUTION,
            priority=RecommendationPriority.HIGH,
            explanation="AI proposed nonexistent version",
            validation_status=ValidationStatus.REJECTED_HALLUCINATION,
        )
    ]
    request = UpgradePlanRequest(
        manifest_type="requirements.txt",
        original_content=content,
        recommendations=recs,
    )
    proposal = planner.create_upgrade_proposal(request)

    assert proposal.total_planned == 0
    assert proposal.total_conflicts == 1
    assert proposal.status == ProposalStatus.CONFLICT_DETECTED
    assert proposal.proposed_content == content
    assert proposal.unified_diff == ""


def test_planner_detects_downgrade_attempt(planner: UpgradePlannerService):
    """Verify recommendation proposing version downgrade is rejected as conflict."""
    content = "requests==2.32.0\n"
    recs = [
        PackageUpgradeRecommendation(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.32.0",
            declared_specifier="==2.32.0",
            candidate_version="2.28.0",
            category=RecommendationCategory.RECOMMENDED,
            priority=RecommendationPriority.LOW,
            explanation="Proposed downgrade.",
            validation_status=ValidationStatus.VALIDATED,
        )
    ]
    request = UpgradePlanRequest(
        manifest_type="requirements.txt",
        original_content=content,
        recommendations=recs,
    )
    proposal = planner.create_upgrade_proposal(request)

    assert proposal.total_planned == 0
    assert proposal.total_conflicts == 1
    assert proposal.status == ProposalStatus.CONFLICT_DETECTED


def test_planner_detects_unknown_package_recommendation(planner: UpgradePlannerService):
    """Verify recommendation for package not present in manifest is reported as conflict."""
    content = "requests==2.28.0\n"
    recs = [
        PackageUpgradeRecommendation(
            package_name="phantom-package",
            ecosystem=Ecosystem.PYTHON,
            current_version="1.0.0",
            declared_specifier="==1.0.0",
            candidate_version="2.0.0",
            category=RecommendationCategory.RECOMMENDED,
            priority=RecommendationPriority.LOW,
            explanation="Injected package",
            validation_status=ValidationStatus.VALIDATED,
        )
    ]
    request = UpgradePlanRequest(
        manifest_type="requirements.txt",
        original_content=content,
        recommendations=recs,
    )
    proposal = planner.create_upgrade_proposal(request)

    assert proposal.total_conflicts >= 1
    assert any(item.package_name == "phantom-package" for item in proposal.plan_items)


def test_planner_deterministic_output(planner: UpgradePlannerService):
    """Verify identical requests generate byte-for-byte identical proposals and diffs."""
    content = "requests==2.28.0\nflask==2.2.5\n"
    recs = [
        PackageUpgradeRecommendation(
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
    ]
    req1 = UpgradePlanRequest(manifest_type="requirements.txt", original_content=content, recommendations=recs)
    req2 = UpgradePlanRequest(manifest_type="requirements.txt", original_content=content, recommendations=recs)

    p1 = planner.create_upgrade_proposal(req1)
    p2 = planner.create_upgrade_proposal(req2)

    assert p1.proposed_content == p2.proposed_content
    assert p1.unified_diff == p2.unified_diff
    assert p1.total_planned == p2.total_planned


def test_planner_package_json_planning(planner: UpgradePlannerService):
    """Verify planner operates on package.json preserving range prefixes."""
    content = json.dumps({
        "name": "web-app",
        "dependencies": {"react": "^18.2.0"},
    }, indent=2) + "\n"

    recs = [
        PackageUpgradeRecommendation(
            package_name="react",
            ecosystem=Ecosystem.NPM,
            current_version="18.2.0",
            declared_specifier="^18.2.0",
            candidate_version="18.3.1",
            category=RecommendationCategory.RECOMMENDED,
            priority=RecommendationPriority.MEDIUM,
            explanation="Upgrade react",
            validation_status=ValidationStatus.VALIDATED,
        )
    ]
    request = UpgradePlanRequest(
        manifest_type="package.json",
        original_content=content,
        recommendations=recs,
    )
    proposal = planner.create_upgrade_proposal(request)

    assert proposal.total_planned == 1
    assert proposal.status == ProposalStatus.READY_FOR_REVIEW
    assert '"react": "^18.3.1"' in proposal.proposed_content


def test_workspace_mutation_requires_human_approval(planner: UpgradePlannerService):
    """Verify Criterion 4: Attempting workspace mutation without approved=True raises HumanApprovalRequiredError."""
    content = "requests==2.28.0\n"
    recs = [
        PackageUpgradeRecommendation(
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
    ]
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(manifest_type="requirements.txt", original_content=content, recommendations=recs)
    )

    # Attempt mutation with approved=False!
    apply_req = WorkspaceApplyRequest(
        proposal=proposal,
        approved=False,
    )
    with pytest.raises(HumanApprovalRequiredError, match="Human approval gate enforced"):
        planner.apply_workspace_mutation(apply_req)


def test_workspace_mutation_succeeds_with_human_approval(tmp_path: Path, planner: UpgradePlannerService):
    """Verify workspace mutation writes file and reports SHA-256 when approved=True."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    recs = [
        PackageUpgradeRecommendation(
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
    ]
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="requirements.txt",
            original_content="requests==2.28.0\n",
            recommendations=recs,
            file_path="requirements.txt",
        )
    )

    # Apply with approved=True
    apply_req = WorkspaceApplyRequest(
        proposal=proposal,
        approved=True,
        target_path=str(target_file),
    )
    resp = planner.apply_workspace_mutation(apply_req)

    assert resp.success is True
    assert resp.status == "APPLIED"
    assert resp.applied_changes_count == 1
    assert resp.sha256 is not None
    assert target_file.read_text(encoding="utf-8") == "requests==2.32.3\n"
