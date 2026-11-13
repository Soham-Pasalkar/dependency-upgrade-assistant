"""Unit tests for multi-point patch integrity verification."""
from __future__ import annotations

from src.models.manifest import Ecosystem
from src.models.plan import PlannedUpgradeItem, UpgradeAction
from src.services.patch_service import generate_unified_diff
from src.services.patch_validator import PatchValidator


def test_validator_passes_clean_patch():
    """Verify clean, traceable patch passes all integrity checks."""
    original = "requests==2.28.0\nflask==2.2.5\n"
    proposed = "requests==2.32.3\nflask==2.2.5\n"
    diff = generate_unified_diff(original, proposed, file_path="requirements.txt")
    plan_items = [
        PlannedUpgradeItem(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            target_version="2.32.3",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        ),
        PlannedUpgradeItem(
            package_name="flask",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.2.5",
            target_version="2.2.5",
            action=UpgradeAction.RETAIN,
            rationale="Retained",
        ),
    ]

    result = PatchValidator.validate_patch(
        manifest_type="requirements.txt",
        original_content=original,
        proposed_content=proposed,
        unified_diff=diff,
        plan_items=plan_items,
        file_path="requirements.txt",
    )

    assert result.is_valid is True
    assert result.can_apply is True
    assert result.can_reverse is True
    assert result.syntax_valid is True
    assert result.target_versions_match is True
    assert result.unrelated_dependencies_unchanged is True
    assert len(result.errors) == 0


def test_validator_rejects_unsafe_file_path():
    """Verify directory traversal attempts in file_path are flagged and failed."""
    original = "requests==2.28.0\n"
    proposed = "requests==2.32.3\n"
    diff = generate_unified_diff(original, proposed, file_path="../../requirements.txt")
    plan_items = [
        PlannedUpgradeItem(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            target_version="2.32.3",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        )
    ]

    result = PatchValidator.validate_patch(
        manifest_type="requirements.txt",
        original_content=original,
        proposed_content=proposed,
        unified_diff=diff,
        plan_items=plan_items,
        file_path="../../requirements.txt",
    )

    assert result.is_valid is False
    assert "path_security" in result.checks_failed
    assert any("Rejected unsafe" in err for err in result.errors)


def test_validator_detects_unplanned_modification_of_unrelated_dependency():
    """Verify modification of a dependency that was not in the plan is caught as an integrity failure."""
    original = "requests==2.28.0\nflask==2.2.5\n"
    # Unplanned modification of flask
    proposed = "requests==2.32.3\nflask==3.0.0\n"
    diff = generate_unified_diff(original, proposed, file_path="requirements.txt")
    plan_items = [
        # Plan only authorized requests to be updated!
        PlannedUpgradeItem(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            target_version="2.32.3",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        )
    ]

    result = PatchValidator.validate_patch(
        manifest_type="requirements.txt",
        original_content=original,
        proposed_content=proposed,
        unified_diff=diff,
        plan_items=plan_items,
        file_path="requirements.txt",
    )

    assert result.is_valid is False
    assert "unrelated_preservation" in result.checks_failed
    assert any("flask" in err for err in result.errors)


def test_validator_detects_target_version_mismatch():
    """Verify if proposed version does not match planned version, check fails."""
    original = "requests==2.28.0\n"
    # Proposed 2.30.0 instead of planned 2.32.3
    proposed = "requests==2.30.0\n"
    diff = generate_unified_diff(original, proposed, file_path="requirements.txt")
    plan_items = [
        PlannedUpgradeItem(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.28.0",
            target_version="2.32.3",
            action=UpgradeAction.UPGRADE,
            rationale="Upgrade",
        )
    ]

    result = PatchValidator.validate_patch(
        manifest_type="requirements.txt",
        original_content=original,
        proposed_content=proposed,
        unified_diff=diff,
        plan_items=plan_items,
        file_path="requirements.txt",
    )

    assert result.is_valid is False
    assert "traceability" in result.checks_failed
    assert any("does not match planned target" in err for err in result.errors)


def test_validator_handles_noop_cleanly():
    """Verify no-op patch is valid and reports clean applicability."""
    content = "requests==2.32.3\n"
    result = PatchValidator.validate_patch(
        manifest_type="requirements.txt",
        original_content=content,
        proposed_content=content,
        unified_diff="",
        plan_items=[],
        file_path="requirements.txt",
    )

    assert result.is_valid is True
    assert result.can_apply is True
    assert result.can_reverse is True
    assert len(result.errors) == 0

