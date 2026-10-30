"""Comprehensive end-to-end scenario verification against deterministic fixture repositories."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.analysis.code_improver import ImprovementStatus, ProposedImprovement
from src.data.catalog_loader import Catalog, CatalogMetadata, PackageInfo
from src.pipeline.orchestrator import (
    ApprovalCallback,
    PipelineOrchestrator,
    PipelineResult,
    PipelineStage,
    ProgressCallback,
    StageStatus,
)
from src.services.exporter import (
    ArtifactExporter,
    ExportArtifactsResult,
    PatchValidationResult,
    ZipValidationResult,
    create_and_validate_patch,
    create_and_validate_zip,
)
from src.services.git_service import GitError, GitService
from src.services.ollama_client import OllamaClient, OllamaHealthStatus
from src.services.workspace_manager import WorkspaceManager
from src.validation.sandbox_manager import DockerAvailability, DockerSandboxManager, SandboxExecutionResult
from src.validation.test_runner import ValidationCommandResult, ValidationStatus
from src.validation.validator import RepositoryValidator, ValidationReport

SAMPLE_REPO_DIR = Path(__file__).resolve().parent.parent / "sample-data" / "sample_repo"


@pytest.fixture
def deterministic_catalog() -> Catalog:
    """Catalog aligned with sample_repo versions."""
    meta = CatalogMetadata("Test Catalog", "1.0", "2026-10-30", "Verified test catalog")
    packages = {
        "requests": PackageInfo("2.32.3", ["2.28.0", "2.32.3"], "HTTP library"),
        "pytest": PackageInfo("8.2.0", ["7.0.0", "8.2.0"], "Testing framework"),
        "urllib3": PackageInfo("2.2.3", ["1.26.15", "2.2.3"], "HTTP client library"),
    }
    return Catalog(metadata=meta, packages=packages)


@pytest.fixture
def fixture_git_service():
    """GitService fixture that copies sample-data/sample_repo into the target directory."""
    service = MagicMock(spec=GitService)
    service.validate_github_url.return_value = True
    service.get_head_commit.return_value = "e2e_commit_sha_12345678"

    def clone_sample(repo_url: str, target_dir: Path | str, pat: Optional[str] = None):
        dest = Path(target_dir)
        dest.mkdir(parents=True, exist_ok=True)
        # Deep copy all files from sample_repo
        for item in SAMPLE_REPO_DIR.rglob("*"):
            rel = item.relative_to(SAMPLE_REPO_DIR)
            target = dest / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                shutil.copy2(item, target)

        # Mock git commit
        git_dir = dest / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    service.clone_repository.side_effect = clone_sample
    return service


@pytest.fixture
def mock_ollama_for_fixture():
    """Ollama client mock providing structured responses for the sample repo."""
    client = MagicMock(spec=OllamaClient)
    client.check_health.return_value = OllamaHealthStatus(is_available=True, base_url="http://localhost:11434")

    def fake_chat_json(model: str, messages: list, schema: dict, options=None, timeout=None):
        prompt_content = messages[-1]["content"] if messages else ""
        if "risk" in prompt_content.lower() or "compatibility" in prompt_content.lower():
            return {
                "risk_level": "LOW",
                "breaking_changes_summary": "Requests 2.28 to 2.32 upgrades certifi and charset normalizer dependencies.",
                "deprecated_features": [],
                "migration_notes": "Drop-in replacement for standard requests.get/post.",
                "confidence": "HIGH",
            }, 0.05
        # Code fix for bare except in main.py
        return {
            "original_snippet": "    except:\n        return {}",
            "replacement_snippet": "    except Exception:\n        return {}",
            "explanation": "Replace bare except with explicit Exception type to avoid catching SystemExit.",
        }, 0.05

    client.chat_json.side_effect = fake_chat_json
    return client


# ---------------------------------------------------------------------------
# Scenario 1: Successful execution with all required services available
# ---------------------------------------------------------------------------
def test_scenario_1_full_successful_execution(tmp_path: Path, fixture_git_service, mock_ollama_for_fixture, deterministic_catalog):
    """Scenario 1: Complete pipeline execution with Git, Ollama, and approvals."""
    orchestrator = PipelineOrchestrator(
        git_service=fixture_git_service,
        ollama_client=mock_ollama_for_fixture,
        catalog=deterministic_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    def approve_all(proposed: List[ProposedImprovement]) -> List[ProposedImprovement]:
        return proposed

    result = orchestrator.run(
        repo_url="https://github.com/example/sample-service",
        ollama_model="llama3.1:8b",
        approval_callback=approve_all,
        approved_upgrades={"requests": "2.32.3"},
    )

    # Assert all 8 stages are COMPLETED
    for stage in PipelineStage:
        assert result.get_stage_record(stage).status == StageStatus.COMPLETED

    assert result.dependency_report is not None
    assert result.dependency_report.total_dependencies >= 2
    assert result.inspection_report is not None
    assert len(result.inspection_report.inspected_files) > 0

    # Deliverables verification
    assert result.export_deliverables is not None
    assert result.export_deliverables.patch_result.is_valid is True
    assert result.export_deliverables.zip_result.is_valid is True
    assert "# Repository Analysis & Improvement Report" in result.export_deliverables.report_markdown


# ---------------------------------------------------------------------------
# Scenario 2: Ollama unavailable
# ---------------------------------------------------------------------------
def test_scenario_2_ollama_unavailable(tmp_path: Path, fixture_git_service, deterministic_catalog):
    """Scenario 2: Ollama is unreachable; deterministic evaluation and AST inspection succeed."""
    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.check_health.return_value = OllamaHealthStatus(
        is_available=False,
        base_url="http://localhost:11434",
        error_message="Connection refused: Ollama daemon not running.",
    )

    orchestrator = PipelineOrchestrator(
        git_service=fixture_git_service,
        ollama_client=mock_ollama,
        catalog=deterministic_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(
        repo_url="https://github.com/example/sample-service",
        ollama_model="llama3.1:8b",
    )

    # Dependency evaluation completed
    assert result.get_stage_record(PipelineStage.EVALUATE_DEPENDENCIES).status == StageStatus.COMPLETED
    # AI risk analysis marked FAILED with diagnostic message
    stage4 = result.get_stage_record(PipelineStage.AI_RISK_ANALYSIS)
    assert stage4.status == StageStatus.FAILED
    assert "Connection refused" in stage4.message

    # Source code inspection still completes via deterministic AST scan
    assert result.get_stage_record(PipelineStage.INSPECT_SOURCE_CODE).status == StageStatus.COMPLETED
    assert result.inspection_report is not None
    assert len(result.inspection_report.findings) > 0  # Static AST found bare except

    # Deliverables generated cleanly
    assert result.export_deliverables is not None


# ---------------------------------------------------------------------------
# Scenario 3: Docker unavailable
# ---------------------------------------------------------------------------
def test_scenario_3_docker_unavailable(tmp_path: Path, fixture_git_service, deterministic_catalog):
    """Scenario 3: Docker daemon unavailable; marks validation NOT_RUNNABLE without host execution."""
    sandbox_mgr = MagicMock(spec=DockerSandboxManager)
    sandbox_mgr.check_availability.return_value = DockerAvailability(is_available=False, error_message="Docker unavailable.")
    sandbox_mgr.run_command.return_value = SandboxExecutionResult(
        exit_code=-1, stdout="", stderr="", duration_seconds=0.0, is_runnable=False, error_message="Docker unavailable."
    )

    orchestrator = PipelineOrchestrator(
        git_service=fixture_git_service,
        catalog=deterministic_catalog,
        sandbox_manager=sandbox_mgr,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(repo_url="https://github.com/example/sample-service")

    val_rec = result.get_stage_record(PipelineStage.VALIDATE_CHANGES)
    assert val_rec.status == StageStatus.COMPLETED
    assert result.validation_report is not None
    assert result.validation_report.overall_status == ValidationStatus.NOT_RUNNABLE


# ---------------------------------------------------------------------------
# Scenario 4: Invalid GitHub repository URL
# ---------------------------------------------------------------------------
def test_scenario_4_invalid_github_url(tmp_path: Path, deterministic_catalog):
    """Scenario 4: Malformed or non-GitHub URL fails Stage 1 and skips remaining stages."""
    orchestrator = PipelineOrchestrator(
        catalog=deterministic_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(repo_url="https://gitlab.com/invalid/repo")

    assert result.get_stage_record(PipelineStage.ACQUIRE_REPO).status == StageStatus.FAILED
    assert result.is_partial is True
    assert result.get_stage_record(PipelineStage.DISCOVER_MANIFESTS).status == StageStatus.SKIPPED


# ---------------------------------------------------------------------------
# Scenario 5: Repository with no supported manifests
# ---------------------------------------------------------------------------
def test_scenario_5_no_manifests(tmp_path: Path, deterministic_catalog):
    """Scenario 5: Repository containing source code but zero dependency manifests."""
    empty_git = MagicMock(spec=GitService)
    empty_git.validate_github_url.return_value = True
    empty_git.get_head_commit.return_value = "no_manifest_commit"

    def clone_no_manifests(repo_url, target_dir, pat=None):
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "main.py").write_text("print('hello')\n", encoding="utf-8")

    empty_git.clone_repository.side_effect = clone_no_manifests

    orchestrator = PipelineOrchestrator(
        git_service=empty_git,
        catalog=deterministic_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(repo_url="https://github.com/example/no-manifests")

    assert result.get_stage_record(PipelineStage.DISCOVER_MANIFESTS).status == StageStatus.COMPLETED
    assert len(result.manifests) == 0
    assert result.get_stage_record(PipelineStage.EVALUATE_DEPENDENCIES).status == StageStatus.COMPLETED
    assert result.dependency_report.total_dependencies == 0


# ---------------------------------------------------------------------------
# Scenario 6: Repository with no source files
# ---------------------------------------------------------------------------
def test_scenario_6_no_source_files(tmp_path: Path, deterministic_catalog):
    """Scenario 6: Repository containing requirements.txt but no Python source code."""
    data_git = MagicMock(spec=GitService)
    data_git.validate_github_url.return_value = True
    data_git.get_head_commit.return_value = "no_code_commit"

    def clone_only_manifest(repo_url, target_dir, pat=None):
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "requirements.txt").write_text("requests==2.28.0\n", encoding="utf-8")

    data_git.clone_repository.side_effect = clone_only_manifest

    orchestrator = PipelineOrchestrator(
        git_service=data_git,
        catalog=deterministic_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(repo_url="https://github.com/example/no-code")

    assert result.get_stage_record(PipelineStage.INSPECT_SOURCE_CODE).status == StageStatus.COMPLETED
    assert len(result.inspection_report.inspected_files) == 0
    assert len(result.proposed_improvements) == 0


# ---------------------------------------------------------------------------
# Scenario 7: No AI improvements proposed
# ---------------------------------------------------------------------------
def test_scenario_7_no_ai_improvements_proposed(tmp_path: Path, deterministic_catalog):
    """Scenario 7: Clean source code generates zero findings and zero improvements."""
    clean_git = MagicMock(spec=GitService)
    clean_git.validate_github_url.return_value = True
    clean_git.get_head_commit.return_value = "clean_commit"

    def clone_clean(repo_url, target_dir, pat=None):
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "utils.py").write_text(
            "def add(a: int, b: int) -> int:\n"
            "    \"\"\"Add two integers.\"\"\"\n"
            "    return a + b\n",
            encoding="utf-8",
        )

    clean_git.clone_repository.side_effect = clone_clean

    orchestrator = PipelineOrchestrator(
        git_service=clean_git,
        catalog=deterministic_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(repo_url="https://github.com/example/clean-code")

    assert result.get_stage_record(PipelineStage.INSPECT_SOURCE_CODE).status == StageStatus.COMPLETED
    assert len(result.inspection_report.findings) == 0
    assert len(result.proposed_improvements) == 0


# ---------------------------------------------------------------------------
# Scenario 8: User rejects every proposed improvement
# ---------------------------------------------------------------------------
def test_scenario_8_user_rejects_every_improvement(tmp_path: Path, fixture_git_service, mock_ollama_for_fixture, deterministic_catalog):
    """Scenario 8: Human approval rejects all improvements; zero changes applied to working copy."""
    orchestrator = PipelineOrchestrator(
        git_service=fixture_git_service,
        ollama_client=mock_ollama_for_fixture,
        catalog=deterministic_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    def reject_everything(proposed: List[ProposedImprovement]) -> List[ProposedImprovement]:
        return []

    result = orchestrator.run(
        repo_url="https://github.com/example/sample-service",
        ollama_model="llama3.1:8b",
        approval_callback=reject_everything,
        approved_upgrades={},
    )

    assert result.get_stage_record(PipelineStage.APPLY_IMPROVEMENTS).status == StageStatus.COMPLETED
    assert len(result.approved_improvements) == 0
    assert len(result.applied_improvements) == 0
    assert len(result.applied_manifest_upgrades) == 0
    assert len(result.export_deliverables.patch_result.modified_files) == 0


# ---------------------------------------------------------------------------
# Scenario 9: User approves selected improvements
# ---------------------------------------------------------------------------
def test_scenario_9_user_approves_selected_improvements(tmp_path: Path, fixture_git_service, mock_ollama_for_fixture, deterministic_catalog):
    """Scenario 9: Human approval selectively approves code fixes and manifest upgrades."""
    orchestrator = PipelineOrchestrator(
        git_service=fixture_git_service,
        ollama_client=mock_ollama_for_fixture,
        catalog=deterministic_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    def select_first_only(proposed: List[ProposedImprovement]) -> List[ProposedImprovement]:
        return [proposed[0]] if proposed else []

    result = orchestrator.run(
        repo_url="https://github.com/example/sample-service",
        ollama_model="llama3.1:8b",
        approval_callback=select_first_only,
        approved_upgrades={"requests": "2.32.3"},
    )

    assert len(result.approved_improvements) == 1
    assert len(result.applied_manifest_upgrades) >= 1
    assert result.export_deliverables.patch_result.is_valid is True
    assert "requests==2.32.3" in result.export_deliverables.patch_result.patch_content


# ---------------------------------------------------------------------------
# Scenario 10: Invalid patch generation
# ---------------------------------------------------------------------------
def test_scenario_10_invalid_patch_handling(tmp_path: Path):
    """Scenario 10: Patch validation fails git apply --check; accurately reported as invalid."""
    ref_dir = tmp_path / "reference"
    work_dir = tmp_path / "working"
    ref_dir.mkdir()
    work_dir.mkdir()

    (ref_dir / "app.py").write_text("x = 1\n", encoding="utf-8")
    (work_dir / "app.py").write_text("x = 2\n", encoding="utf-8")

    # Patch git apply check to simulate validation failure
    with patch("src.services.exporter.validate_patch_against_reference") as mock_val:
        mock_val.return_value = (False, "git apply: patch does not apply (corrupt hunk)")

        patch_res = create_and_validate_patch(ref_dir, work_dir)
        assert patch_res.is_valid is False
        assert "patch does not apply" in patch_res.error_message


# ---------------------------------------------------------------------------
# Scenario 11: ZIP integrity failure
# ---------------------------------------------------------------------------
def test_scenario_11_zip_integrity_failure(tmp_path: Path):
    """Scenario 11: ZIP testzip() detects CRC corruption; reports invalid archive."""
    work_dir = tmp_path / "working"
    work_dir.mkdir()
    (work_dir / "app.py").write_text("x = 1\n", encoding="utf-8")

    out_zip = tmp_path / "corrupt.zip"

    with patch("zipfile.ZipFile.testzip") as mock_testzip:
        mock_testzip.return_value = "app.py"  # simulate corrupted entry

        zip_res = create_and_validate_zip(work_dir, out_zip)
        assert zip_res.is_valid is False
        assert "ZIP archive corrupted" in zip_res.error_message
        assert not out_zip.exists()  # corrupt file removed


# ---------------------------------------------------------------------------
# Scenario 12: Pipeline cancellation and workspace cleanup
# ---------------------------------------------------------------------------
def test_scenario_12_cancellation_and_cleanup(tmp_path: Path, fixture_git_service, deterministic_catalog):
    """Scenario 12: Pipeline cancellation stops execution and cleans up workspace."""
    cancel_flag = False

    def check_cancel() -> bool:
        return cancel_flag

    def cancel_callback(stage, status, message, details=None):
        nonlocal cancel_flag
        if stage == PipelineStage.DISCOVER_MANIFESTS and status == StageStatus.COMPLETED:
            cancel_flag = True

    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    orchestrator = PipelineOrchestrator(
        git_service=fixture_git_service,
        workspace_manager=ws_mgr,
        catalog=deterministic_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(
        repo_url="https://github.com/example/sample-service",
        progress_callback=cancel_callback,
        cancel_token=check_cancel,
    )

    assert result.is_partial is True
    # ACQUIRE and DISCOVER completed before cancel took effect
    assert result.get_stage_record(PipelineStage.ACQUIRE_REPO).status == StageStatus.COMPLETED
    assert result.get_stage_record(PipelineStage.DISCOVER_MANIFESTS).status == StageStatus.COMPLETED
    # Next stage cancelled
    assert result.get_stage_record(PipelineStage.EVALUATE_DEPENDENCIES).status == StageStatus.SKIPPED

    # Workspace directory removed
    remaining = list((tmp_path / "workspaces").glob("ws_*"))
    assert len(remaining) == 0


# ---------------------------------------------------------------------------
# Scenario 13: Failure in an intermediate pipeline stage
# ---------------------------------------------------------------------------
def test_scenario_13_intermediate_stage_failure(tmp_path: Path, fixture_git_service, deterministic_catalog):
    """Scenario 13: Exception during code inspection does not crash pipeline; intermediate report is saved."""
    orchestrator = PipelineOrchestrator(
        git_service=fixture_git_service,
        catalog=deterministic_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    with patch("src.analysis.code_inspector.CodeInspector.inspect_repository") as mock_inspect:
        mock_inspect.side_effect = RuntimeError("AST parser crashed unexpectedly.")

        result = orchestrator.run(repo_url="https://github.com/example/sample-service")

        stage5 = result.get_stage_record(PipelineStage.INSPECT_SOURCE_CODE)
        assert stage5.status == StageStatus.FAILED
        assert "AST parser crashed" in stage5.error

        # Dependency report from Stage 3 was preserved
        assert result.dependency_report is not None
        assert result.dependency_report.total_dependencies > 0

        # Subsequent stages executed safely (applied zero fixes)
        assert result.get_stage_record(PipelineStage.APPLY_IMPROVEMENTS).status == StageStatus.COMPLETED
        assert result.export_deliverables is not None

