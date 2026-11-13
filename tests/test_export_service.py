"""Unit and integration tests for ArtifactExporter proposal exports, checksums, and ZIP packaging."""
import json
import zipfile
from pathlib import Path
from typing import List

import pytest

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
from src.services.exporter import (
    ArtifactExporter,
    compute_sha256_file,
)
from src.services.secret_scanner import SecretLeakageError


@pytest.fixture
def sample_proposal() -> UpgradeProposalResponse:
    """Provide a realistic sample upgrade proposal for requirements.txt."""
    plan_item = PlannedUpgradeItem(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.28.0",
        target_version="2.32.3",
        declared_specifier="==2.28.0",
        proposed_specifier="==2.32.3",
        action=UpgradeAction.UPGRADE,
        rationale="Upgrade to latest security release",
        line_number=1,
        dependency_type=DependencyType.RUNTIME,
    )
    diff_text = (
        "--- a/requirements.txt\n"
        "+++ b/requirements.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-requests==2.28.0\n"
        "+requests==2.32.3\n"
    )
    integrity = PatchIntegrityResult(
        is_valid=True,
        can_apply=True,
        can_reverse=True,
        syntax_valid=True,
        target_versions_match=True,
        unrelated_dependencies_unchanged=True,
        original_unchanged=True,
        checks_passed=["syntax_check", "reverse_check"],
    )
    return UpgradeProposalResponse(
        proposal_id="prop_test_12345",
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        file_path="requirements.txt",
        plan_items=[plan_item],
        total_planned=1,
        total_unchanged=0,
        total_conflicts=0,
        original_content="requests==2.28.0\n",
        proposed_content="requests==2.32.3\n",
        unified_diff=diff_text,
        integrity=integrity,
        status=ProposalStatus.READY_FOR_REVIEW,
        requires_human_approval=True,
    )


@pytest.fixture
def sample_validation_report() -> ValidationReportResponse:
    """Provide a realistic validation report."""
    cmd = ValidationCommandResult(
        command_name="py_compile",
        command_args=["python", "-m", "py_compile", "requirements.txt"],
        status=ValidationStatus.PASSED,
        exit_code=0,
        stdout="Syntax OK",
        stderr="",
        duration_seconds=0.45,
    )
    return ValidationReportResponse(
        execution_id="exec_test_67890",
        proposal_id="prop_test_12345",
        status=ValidationStatus.PASSED,
        is_runnable=True,
        summary="Validation completed with zero errors.",
        commands=[cmd],
        total_duration_seconds=0.45,
        image_used="python:3.11-slim",
        image_digest="sha256:abc123pinneddigest",
        network_enabled=False,
        logs="[py_compile] Syntax OK\nAll checks passed successfully.\n",
        created_at=1731300000.0,
    )


def test_export_proposal_successful_bundle(tmp_path: Path, sample_proposal: UpgradeProposalResponse, sample_validation_report: ValidationReportResponse):
    """Verify complete export generation including .patch, summary.json, logs, checksums, and ZIP."""
    store_dir = tmp_path / "exports"
    exporter = ArtifactExporter(export_store_dir=store_dir)

    bundle = exporter.export_proposal(
        proposal=sample_proposal,
        validation_report=sample_validation_report,
        run_id="run_test_abc",
        custom_notes="Audited by Senior Security Engineer",
    )

    assert bundle.export_id.startswith("exp_")
    assert bundle.validation_status == "PASSED"
    assert bundle.sanitized is True
    assert bundle.secret_scan_passed is True

    # 1. Verify Patch file
    patch_rec = bundle.artifacts[ArtifactType.PATCH]
    assert patch_rec.file_path.is_file()
    assert patch_rec.filename == "requirements.txt-upgrade.patch"
    patch_content = patch_rec.file_path.read_text(encoding="utf-8")
    assert "Dependency Upgrade Assistant 2.0 Export Patch" in patch_content
    assert "Proposal ID: prop_test_12345" in patch_content
    assert "-requests==2.28.0" in patch_content
    assert "+requests==2.32.3" in patch_content
    assert compute_sha256_file(patch_rec.file_path) == patch_rec.sha256

    # 2. Verify Execution Logs
    log_rec = bundle.artifacts[ArtifactType.EXECUTION_LOG]
    assert log_rec.file_path.is_file()
    assert log_rec.filename == "execution.log"
    log_content = log_rec.file_path.read_text(encoding="utf-8")
    assert "Syntax OK" in log_content
    assert compute_sha256_file(log_rec.file_path) == log_rec.sha256

    # 3. Verify JSON Execution Summary
    summary_rec = bundle.artifacts[ArtifactType.SUMMARY_JSON]
    assert summary_rec.file_path.is_file()
    summary_data = json.loads(summary_rec.file_path.read_text(encoding="utf-8"))
    assert summary_data["dua_version"] == "2.0.0"
    assert summary_data["proposal_id"] == "prop_test_12345"
    assert summary_data["total_planned"] == 1
    assert summary_data["planned_upgrades"][0]["package_name"] == "requests"
    assert summary_data["validation"]["status"] == "PASSED"
    assert summary_data["security_audit"]["zero_secrets_verified"] is True
    assert summary_data["custom_notes"] == "Audited by Senior Security Engineer"

    # 4. Verify Checksums Manifest
    chk_rec = bundle.artifacts[ArtifactType.CHECKSUMS]
    assert chk_rec.file_path.is_file()
    chk_content = chk_rec.file_path.read_text(encoding="utf-8")
    assert f"{patch_rec.sha256}  {patch_rec.filename}" in chk_content
    assert f"{log_rec.sha256}  execution.log" in chk_content

    # 5. Verify ZIP Bundle Integrity and test extraction
    zip_rec = bundle.artifacts[ArtifactType.BUNDLE_ZIP]
    assert zip_rec.file_path.is_file()
    assert zip_rec.filename == "requirements.txt-upgrade-bundle.zip"
    assert zipfile.is_zipfile(zip_rec.file_path)

    with zipfile.ZipFile(zip_rec.file_path, "r") as zf:
        assert zf.testzip() is None
        namelist = zf.namelist()
        assert patch_rec.filename in namelist
        assert "summary.json" in namelist
        assert "execution.log" in namelist
        assert "checksums.sha256" in namelist
        # Strictly verify no path traversal entries
        for name in namelist:
            assert not name.startswith(("/", "\\"))
            assert ".." not in name


def test_export_proposal_without_validation_truthful_status(tmp_path: Path, sample_proposal: UpgradeProposalResponse):
    """Verify that export without validation records truthful NOT_PERFORMED status."""
    store_dir = tmp_path / "exports"
    exporter = ArtifactExporter(export_store_dir=store_dir)

    bundle = exporter.export_proposal(
        proposal=sample_proposal,
        validation_report=None,
    )

    assert bundle.validation_status == "NOT_PERFORMED"
    summary_rec = bundle.artifacts[ArtifactType.SUMMARY_JSON]
    summary_data = json.loads(summary_rec.file_path.read_text(encoding="utf-8"))
    assert summary_data["validation"]["status"] == "NOT_PERFORMED"

    log_rec = bundle.artifacts[ArtifactType.EXECUTION_LOG]
    log_text = log_rec.file_path.read_text(encoding="utf-8")
    assert "NOT_PERFORMED" in log_text


def test_export_proposal_with_secret_leakage_fails_closed(tmp_path: Path, sample_proposal: UpgradeProposalResponse):
    """Verify that detected secrets in diff or content fail closed without leaking credentials."""
    store_dir = tmp_path / "exports"
    exporter = ArtifactExporter(export_store_dir=store_dir)

    # Infiltrate a synthetic AWS key into the proposal diff
    dummy_key = "AKIA" + "1234567890ABCDEF"  # aidlc:allow-secret
    leaked_proposal = sample_proposal.model_copy(deep=True)
    leaked_proposal.unified_diff += f"\n+AWS_KEY = '{dummy_key}'\n"  # aidlc:allow-secret

    with pytest.raises(SecretLeakageError) as exc_info:
        exporter.export_proposal(proposal=leaked_proposal)

    assert "Pre-export secret verification failed" in str(exc_info.value)
    # The raw key must not be displayed in cleartext in the error message
    assert dummy_key not in str(exc_info.value)  # aidlc:allow-secret

    # Verify fail-closed cleanup: no files left in store_dir
    created_dirs = list(store_dir.iterdir())
    assert len(created_dirs) == 0


def test_get_export_and_filesystem_recovery(tmp_path: Path, sample_proposal: UpgradeProposalResponse):
    """Verify retrieval of export bundles and recovery after cache eviction."""
    store_dir = tmp_path / "exports"
    exporter = ArtifactExporter(export_store_dir=store_dir)

    bundle = exporter.export_proposal(proposal=sample_proposal)
    export_id = bundle.export_id

    # 1. Direct in-memory lookup
    retrieved = exporter.get_export(export_id)
    assert retrieved is not None
    assert retrieved.export_id == export_id

    # 2. Simulate server restart: clear memory cache
    exporter._exports.clear()
    recovered = exporter.get_export(export_id)
    assert recovered is not None
    assert recovered.export_id == export_id
    assert ArtifactType.PATCH in recovered.artifacts
    assert ArtifactType.BUNDLE_ZIP in recovered.artifacts


def test_get_artifact_file_path_traversal_rejection(tmp_path: Path, sample_proposal: UpgradeProposalResponse):
    """Verify path traversal attempts on artifact endpoints are rejected."""
    store_dir = tmp_path / "exports"
    exporter = ArtifactExporter(export_store_dir=store_dir)

    bundle = exporter.export_proposal(proposal=sample_proposal)
    export_id = bundle.export_id

    # 1. Valid retrieval
    p, name, mime, sha = exporter.get_artifact_file(export_id, ArtifactType.PATCH)
    assert p.is_file()
    assert mime == "text/x-diff; charset=utf-8"

    # 2. Invalid artifact type
    with pytest.raises(ValueError):
        exporter.get_artifact_file(export_id, "malicious_type")

    # 3. Path traversal in export_id
    with pytest.raises(FileNotFoundError):
        exporter.get_artifact_file("../../etc/passwd", ArtifactType.PATCH)
