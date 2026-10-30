"""Integration tests for the eight-stage PipelineOrchestrator."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.analysis.code_improver import ImprovementStatus, ProposedImprovement
from src.analysis.code_inspector import CodeFinding, IssueCategory, IssueSeverity
from src.core.comparator import compare_requirements
from src.core.parser import parse_requirements_content
from src.data.catalog_loader import Catalog, CatalogMetadata, PackageInfo, normalize_package_name
from src.pipeline.orchestrator import (
    ApprovalCallback,
    PipelineOrchestrator,
    PipelineResult,
    PipelineStage,
    ProgressCallback,
    StageStatus,
)
from src.services.git_service import GitError, GitService
from src.services.ollama_client import OllamaClient, OllamaHealthStatus
from src.services.workspace_manager import WorkspaceManager
from src.validation.sandbox_manager import DockerAvailability, DockerSandboxManager, SandboxExecutionResult
from src.validation.test_runner import SandboxedValidationRunner, ValidationCommandResult, ValidationStatus
from src.validation.validator import RepositoryValidator, ValidationReport


@pytest.fixture
def test_catalog() -> Catalog:
    """Fixture providing a mock catalog for dependency comparisons."""
    meta = CatalogMetadata("Test Catalog", "1.0", "2026-10-30", "Test provenance")
    entries = {
        "requests": PackageInfo("2.32.3", ["2.28.0", "2.32.3"], "HTTP library"),
        "pytest": PackageInfo("8.2.0", ["7.0.0", "8.2.0"], "Testing framework"),
    }
    return Catalog(metadata=meta, packages=entries)


@pytest.fixture
def mock_git_service():
    """GitService fixture that populates target_dir with sample repository files."""
    service = MagicMock(spec=GitService)
    service.validate_github_url.return_value = True
    service.get_head_commit.return_value = "c0ffee1234567890abcdef"

    def fake_clone(repo_url: str, target_dir: Path | str, pat: Optional[str] = None):
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "requirements.txt").write_text("requests==2.28.0\npytest==7.0.0\n", encoding="utf-8")
        (target / "app.py").write_text(
            "import requests\n\n"
            "def fetch():\n"
            "    try:\n"
            "        return 1 / 0\n"
            "    except:\n"
            "        pass\n",
            encoding="utf-8",
        )
        git_dir = target / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    service.clone_repository.side_effect = fake_clone
    return service


@pytest.fixture
def mock_ollama_client():
    """OllamaClient fixture providing mock responses for risk and improvements."""
    client = MagicMock(spec=OllamaClient)
    client.check_health.return_value = OllamaHealthStatus(is_available=True, base_url="http://localhost:11434")

    def fake_chat_json(model: str, messages: list, schema: dict, options=None, timeout=None):
        prompt_content = messages[-1]["content"] if messages else ""
        if "risk" in prompt_content.lower() or "compatibility" in prompt_content.lower():
            return {
                "risk_level": "LOW",
                "breaking_changes_summary": "Minimal breaking changes between 2.28 and 2.32.",
                "deprecated_features": [],
                "migration_notes": "Drop-in compatible for standard GET/POST.",
                "confidence": "HIGH",
            }, 0.05
        # Code improvement fix
        return {
            "original_snippet": "    except:\n        pass",
            "replacement_snippet": "    except ZeroDivisionError:\n        pass",
            "explanation": "Replace bare except with specific ZeroDivisionError.",
        }, 0.05

    client.chat_json.side_effect = fake_chat_json
    return client


def test_full_pipeline_success_with_approval_callback(tmp_path: Path, mock_git_service, mock_ollama_client, test_catalog):
    """Verify complete 8-stage pipeline execution when human approves improvements."""
    progress_events: List[tuple] = []

    def on_progress(stage: PipelineStage, status: StageStatus, message: str, details=None):
        progress_events.append((stage, status, message))

    # Human approval: approve all proposed fixes
    def approve_all(proposed: List[ProposedImprovement]) -> List[ProposedImprovement]:
        return proposed

    orchestrator = PipelineOrchestrator(
        git_service=mock_git_service,
        ollama_client=mock_ollama_client,
        catalog=test_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(
        repo_url="https://github.com/example/demo-repo",
        ollama_model="llama3.1:8b",
        approval_callback=approve_all,
        approved_upgrades={"requests": "2.32.3"},
        progress_callback=on_progress,
    )

    # 1. Assert all 8 stages are COMPLETED
    for stage in PipelineStage:
        rec = result.get_stage_record(stage)
        assert rec.status == StageStatus.COMPLETED, f"Stage {stage} expected COMPLETED, got {rec.status} ({rec.error})"

    # 2. Stage ordering verification
    stage_order = [
        PipelineStage.ACQUIRE_REPO,
        PipelineStage.DISCOVER_MANIFESTS,
        PipelineStage.EVALUATE_DEPENDENCIES,
        PipelineStage.AI_RISK_ANALYSIS,
        PipelineStage.INSPECT_SOURCE_CODE,
        PipelineStage.APPLY_IMPROVEMENTS,
        PipelineStage.VALIDATE_CHANGES,
        PipelineStage.GENERATE_ARTIFACTS,
    ]
    seen_running = [e[0] for e in progress_events if e[1] == StageStatus.RUNNING]
    assert seen_running == stage_order

    # 3. Assert artifacts were created
    assert result.export_deliverables is not None
    assert result.export_deliverables.patch_result.is_valid is True
    assert result.export_deliverables.zip_result.is_valid is True
    assert "# Repository Analysis & Improvement Report" in result.export_deliverables.report_markdown


def test_human_approval_rejection_applies_zero_improvements(tmp_path: Path, mock_git_service, mock_ollama_client, test_catalog):
    """Verify that if the human approval callback rejects all improvements, zero code changes are applied."""
    def reject_all(proposed: List[ProposedImprovement]) -> List[ProposedImprovement]:
        return []  # Reject all

    orchestrator = PipelineOrchestrator(
        git_service=mock_git_service,
        ollama_client=mock_ollama_client,
        catalog=test_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(
        repo_url="https://github.com/example/demo-repo",
        ollama_model="llama3.1:8b",
        approval_callback=reject_all,
        approved_upgrades={},  # No manifest upgrades approved either
    )

    assert result.get_stage_record(PipelineStage.APPLY_IMPROVEMENTS).status == StageStatus.COMPLETED
    assert len(result.approved_improvements) == 0
    assert len(result.applied_improvements) == 0
    assert len(result.applied_manifest_upgrades) == 0

    # Since zero changes were approved, the unified patch is empty (0 modified files)
    assert result.export_deliverables is not None
    assert len(result.export_deliverables.patch_result.modified_files) == 0


def test_human_approval_default_is_safe_zero_without_approval(tmp_path: Path, mock_git_service, mock_ollama_client, test_catalog):
    """Verify that if no approval callback and no approved improvements are provided, zero changes are applied."""
    orchestrator = PipelineOrchestrator(
        git_service=mock_git_service,
        ollama_client=mock_ollama_client,
        catalog=test_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    # Calling run with no approval params
    result = orchestrator.run(
        repo_url="https://github.com/example/demo-repo",
        ollama_model="llama3.1:8b",
    )

    assert len(result.approved_improvements) == 0
    assert len(result.applied_improvements) == 0


def test_ollama_unavailable_skips_ai_stages_without_failing_pipeline(tmp_path: Path, mock_git_service, test_catalog):
    """Verify that when Ollama is unavailable, dependency evaluation succeeds and AI stages report failure/skip gracefully."""
    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.check_health.return_value = OllamaHealthStatus(
        is_available=False,
        base_url="http://localhost:11434",
        error_message="Connection refused: Ollama daemon not running.",
    )

    orchestrator = PipelineOrchestrator(
        git_service=mock_git_service,
        ollama_client=mock_ollama,
        catalog=test_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(
        repo_url="https://github.com/example/demo-repo",
        ollama_model="llama3.1:8b",
    )

    # Dependency evaluation completed deterministically
    assert result.get_stage_record(PipelineStage.EVALUATE_DEPENDENCIES).status == StageStatus.COMPLETED
    assert result.dependency_report is not None
    assert result.dependency_report.total_dependencies > 0

    # AI risk analysis marked FAILED or SKIPPED with diagnostic message
    stage4 = result.get_stage_record(PipelineStage.AI_RISK_ANALYSIS)
    assert stage4.status == StageStatus.FAILED
    assert "Connection refused" in stage4.message

    # Artifact generation still produces valid deliverables
    assert result.export_deliverables is not None
    assert result.export_deliverables.report_markdown != ""


def test_docker_unavailable_retains_not_runnable_status(tmp_path: Path, mock_git_service, test_catalog):
    """Verify that Docker unavailability marks validation as NOT_RUNNABLE and never executes on host."""
    sandbox_mgr = MagicMock(spec=DockerSandboxManager)
    sandbox_mgr.check_availability.return_value = DockerAvailability(
        is_available=False,
        error_message="Docker daemon not reachable.",
    )
    sandbox_mgr.run_command.return_value = SandboxExecutionResult(
        exit_code=-1,
        stdout="",
        stderr="",
        duration_seconds=0.0,
        is_runnable=False,
        error_message="Docker daemon not reachable.",
    )

    orchestrator = PipelineOrchestrator(
        git_service=mock_git_service,
        catalog=test_catalog,
        sandbox_manager=sandbox_mgr,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(
        repo_url="https://github.com/example/demo-repo",
    )

    val_rec = result.get_stage_record(PipelineStage.VALIDATE_CHANGES)
    assert val_rec.status == StageStatus.COMPLETED
    assert result.validation_report is not None
    assert result.validation_report.overall_status == ValidationStatus.NOT_RUNNABLE
    assert "NOT_RUNNABLE" in val_rec.message


def test_git_acquisition_failure_stops_pipeline_and_skips_remaining(tmp_path: Path, test_catalog):
    """Verify that an invalid Git URL fails Stage 1 and marks all remaining stages as SKIPPED."""
    bad_git = MagicMock(spec=GitService)
    bad_git.clone_repository.side_effect = GitError("Repository not found or network error.")

    orchestrator = PipelineOrchestrator(
        git_service=bad_git,
        catalog=test_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(
        repo_url="https://github.com/invalid/nonexistent-repo",
    )

    assert result.get_stage_record(PipelineStage.ACQUIRE_REPO).status == StageStatus.FAILED
    assert result.is_partial is True

    # Remaining stages must be SKIPPED
    for stage in [
        PipelineStage.DISCOVER_MANIFESTS,
        PipelineStage.EVALUATE_DEPENDENCIES,
        PipelineStage.AI_RISK_ANALYSIS,
        PipelineStage.INSPECT_SOURCE_CODE,
        PipelineStage.APPLY_IMPROVEMENTS,
        PipelineStage.VALIDATE_CHANGES,
        PipelineStage.GENERATE_ARTIFACTS,
    ]:
        assert result.get_stage_record(stage).status == StageStatus.SKIPPED


def test_cancellation_halts_pipeline_immediately(tmp_path: Path, mock_git_service, test_catalog):
    """Verify that cancellation flag halts execution cleanly."""
    cancel_count = 0

    def cancel_after_stage_1() -> bool:
        nonlocal cancel_count
        cancel_count += 1
        return cancel_count >= 2  # cancel before Stage 2

    orchestrator = PipelineOrchestrator(
        git_service=mock_git_service,
        catalog=test_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(
        repo_url="https://github.com/example/demo-repo",
        cancel_token=cancel_after_stage_1,
    )

    assert result.is_partial is True
    assert result.get_stage_record(PipelineStage.ACQUIRE_REPO).status == StageStatus.COMPLETED
    assert result.get_stage_record(PipelineStage.DISCOVER_MANIFESTS).status == StageStatus.SKIPPED


def test_workspace_cleanup_occurs_after_run(tmp_path: Path, mock_git_service, test_catalog):
    """Verify that workspace directories are completely cleaned up after pipeline execution."""
    recorded_workspace_dir: Optional[Path] = None

    class TrackingWorkspaceManager(WorkspaceManager):
        def create_workspace(self, base_dir=None):
            ctx = super().create_workspace(base_dir=base_dir)
            # Capture the workspace dir
            nonlocal recorded_workspace_dir
            return ctx

    ws_mgr = TrackingWorkspaceManager(base_dir=tmp_path / "workspaces")

    orchestrator = PipelineOrchestrator(
        git_service=mock_git_service,
        workspace_manager=ws_mgr,
        catalog=test_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    result = orchestrator.run(repo_url="https://github.com/example/demo-repo")
    assert result.get_stage_record(PipelineStage.ACQUIRE_REPO).status == StageStatus.COMPLETED

    # Check that any temporary directory created inside workspaces was removed
    active_workspaces = list((tmp_path / "workspaces").glob("ws_*"))
    assert len(active_workspaces) == 0


def test_phase_1_functionality_preserved(test_catalog):
    """Assert that existing Phase 1 Single Manifest Review functions operate identically."""
    sample_manifest = "requests==2.28.0\nurllib3==1.26.15\n# comment\n"
    lines = parse_requirements_content(sample_manifest)
    evaluations = compare_requirements(lines, test_catalog)

    # requests is in test_catalog with candidate 2.32.3
    requests_eval = next(e for e in evaluations if e.line.package_name == "requests")
    assert requests_eval.is_upgrade_candidate is True
    assert requests_eval.candidate_version == "2.32.3"

    # urllib3 is unknown in test_catalog
    urllib3_eval = next(e for e in evaluations if e.line.package_name == "urllib3")
    assert urllib3_eval.is_upgrade_candidate is False


def test_two_stage_execution_with_human_selection(tmp_path: Path, mock_git_service, mock_ollama_client, test_catalog):
    """Verify that calling run_analysis_stages followed by run_improvement_and_export_stages works cleanly."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    orchestrator = PipelineOrchestrator(
        git_service=mock_git_service,
        workspace_manager=ws_mgr,
        ollama_client=mock_ollama_client,
        catalog=test_catalog,
        artifacts_output_dir=tmp_path / "artifacts",
    )

    with ws_mgr.create_workspace() as ws:
        # Step 1: Run analysis
        analysis_res = orchestrator.run_analysis_stages(
            ws=ws,
            repo_url="https://github.com/example/demo-repo",
            ollama_model="llama3.1:8b",
        )
        assert analysis_res.get_stage_record(PipelineStage.INSPECT_SOURCE_CODE).status == StageStatus.COMPLETED
        assert len(analysis_res.proposed_improvements) > 0

        # Human review: select the first proposed improvement
        selected = [analysis_res.proposed_improvements[0]]

        # Step 2: Apply approved and export
        final_res = orchestrator.run_improvement_and_export_stages(
            ws=ws,
            result=analysis_res,
            approved_improvements=selected,
            approved_upgrades={"requests": "2.32.3"},
            ollama_model="llama3.1:8b",
        )

        assert final_res.get_stage_record(PipelineStage.APPLY_IMPROVEMENTS).status == StageStatus.COMPLETED
        assert final_res.get_stage_record(PipelineStage.GENERATE_ARTIFACTS).status == StageStatus.COMPLETED
        assert len(final_res.applied_improvements) == 1
        assert final_res.export_deliverables is not None
        assert final_res.export_deliverables.patch_result.is_valid is True


def test_app_module_syntax_and_importability():
    """Verify that src/app.py compiles cleanly and all symbols are importable."""
    import py_compile
    app_path = Path(__file__).resolve().parent.parent / "src" / "app.py"
    py_compile.compile(str(app_path), doraise=True)

    from src.app import (
        get_catalog,
        get_ollama_client,
        load_sample_file,
        render_phase1_single_manifest,
        render_phase2_repository_tab,
    )
    assert callable(get_catalog)
    assert callable(get_ollama_client)
    assert callable(load_sample_file)
    assert callable(render_phase1_single_manifest)
    assert callable(render_phase2_repository_tab)
