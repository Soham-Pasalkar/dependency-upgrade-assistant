"""Eight-stage pipeline orchestrator for repository analysis, improvement, validation, and export.

Orchestrates the entire repository lifecycle:
1. ACQUIRE_REPO: Secure clone into isolated reference and working copies.
2. DISCOVER_MANIFESTS: Recursive multi-manifest discovery (requirements, pyproject).
3. EVALUATE_DEPENDENCIES: Deterministic PyPI metadata query with local catalog fallback.
4. AI_RISK_ANALYSIS: Structured qualitative risk assessment from Ollama (advisory).
5. INSPECT_SOURCE_CODE: Priority source code discovery, AST scan, and proposed improvements.
6. APPLY_IMPROVEMENTS: Human-approval-gated application to mutable working copy.
7. VALIDATE_CHANGES: Isolated Docker sandbox validation (fail-closed if unavailable).
8. GENERATE_ARTIFACTS: Pre-validated Git patch, verified ZIP, and Markdown report.

Enforces strict safety boundaries:
- Human approval gate: Zero AI-generated improvements applied without explicit selection.
- Independent of Streamlit: Pure Python orchestration with structured callbacks.
- Never executes untrusted code on host.
- Never exposes credentials in URLs, logs, diffs, or prompts.
- Preserves pristine reference directory intact.
- Workspace cleanup on success, failure, and cancellation.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src.analysis.code_improver import (
    CodeImprover,
    ImprovementBatchResult,
    ImprovementStatus,
    ProposedImprovement,
    compute_directory_manifest_hash,
    verify_reference_preservation,
)
from src.analysis.code_inspector import CodeFinding, CodeInspector, InspectionReport
from src.analysis.dependency_analyzer import (
    AnalyzedDependency,
    DependencyAnalyzer,
    PyPIMetadataClient,
    RepositoryDependencyReport,
    assess_upgrade_risk_with_ollama,
)
from src.analysis.manifest_scanner import DiscoveredManifest, ManifestScanResult, ManifestType, scan_manifests
from src.core.parser import parse_requirements_content
from src.data.catalog_loader import Catalog, load_catalog, normalize_package_name
from src.services.exporter import (
    ArtifactExporter,
    ExportArtifactsResult,
    PatchValidationResult,
    ZipValidationResult,
    generate_markdown_report,
)
from src.services.git_service import GitError, GitService
from src.services.ollama_client import OllamaClient, OllamaError
from src.services.workspace_manager import WorkspaceManager, WorkspaceSession
from src.validation.sandbox_manager import DockerSandboxManager
from src.validation.test_runner import SandboxedValidationRunner, ValidationStatus
from src.validation.validator import RepositoryValidator, ValidationReport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline Enums & Stage State Tracking
# ---------------------------------------------------------------------------

class PipelineStage(str, Enum):
    ACQUIRE_REPO = "ACQUIRE_REPO"
    DISCOVER_MANIFESTS = "DISCOVER_MANIFESTS"
    EVALUATE_DEPENDENCIES = "EVALUATE_DEPENDENCIES"
    AI_RISK_ANALYSIS = "AI_RISK_ANALYSIS"
    INSPECT_SOURCE_CODE = "INSPECT_SOURCE_CODE"
    APPLY_IMPROVEMENTS = "APPLY_IMPROVEMENTS"
    VALIDATE_CHANGES = "VALIDATE_CHANGES"
    GENERATE_ARTIFACTS = "GENERATE_ARTIFACTS"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class StageRecord:
    """Execution metrics and diagnostic outcome for a specific pipeline stage."""
    stage: PipelineStage
    status: StageStatus = StageStatus.PENDING
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration_seconds: float = 0.0
    message: str = ""
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


# Progress callback signature: (stage, status, message, optional_details)
ProgressCallback = Callable[[PipelineStage, StageStatus, str, Optional[Dict[str, Any]]], None]

# Human approval callback: (list_of_proposed_improvements) -> list_of_approved_improvements
ApprovalCallback = Callable[[List[ProposedImprovement]], List[ProposedImprovement]]


# ---------------------------------------------------------------------------
# Pipeline Result Deliverable
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """Comprehensive aggregated result of an orchestrated pipeline execution."""
    repo_url: str
    commit_sha: Optional[str] = None
    stage_records: Dict[PipelineStage, StageRecord] = field(default_factory=dict)
    overall_success: bool = False
    is_partial: bool = False
    error: Optional[str] = None

    # Intermediate artifacts & findings
    manifests: List[DiscoveredManifest] = field(default_factory=list)
    dependency_report: Optional[RepositoryDependencyReport] = None
    inspection_report: Optional[InspectionReport] = None
    proposed_improvements: List[ProposedImprovement] = field(default_factory=list)
    approved_improvements: List[ProposedImprovement] = field(default_factory=list)
    applied_improvements: List[ProposedImprovement] = field(default_factory=list)
    applied_manifest_upgrades: List[Tuple[str, str, str]] = field(default_factory=list)  # (manifest, pkg, new_ver)
    validation_report: Optional[ValidationReport] = None
    export_deliverables: Optional[ExportArtifactsResult] = None
    warnings: List[str] = field(default_factory=list)
    baseline_hashes: Dict[str, str] = field(default_factory=dict)

    def get_stage_record(self, stage: PipelineStage) -> StageRecord:
        if stage not in self.stage_records:
            self.stage_records[stage] = StageRecord(stage=stage)
        return self.stage_records[stage]


# ---------------------------------------------------------------------------
# Manifest Upgrade Helper
# ---------------------------------------------------------------------------

def apply_approved_manifest_upgrades(
    working_dir: Path | str,
    manifests: List[DiscoveredManifest],
    approved_upgrades: Dict[str, str],  # normalized_pkg_name -> candidate_version
) -> List[Tuple[str, str, str]]:
    """Update pinned versions in working-copy requirements manifests for approved package upgrades."""
    applied: List[Tuple[str, str, str]] = []
    if not approved_upgrades:
        return applied

    work_root = Path(working_dir).resolve()

    for m in manifests:
        if m.manifest_type not in (ManifestType.REQUIREMENTS_TXT, ManifestType.CONSTRAINTS_TXT):
            continue

        target_file = work_root / m.relative_path
        if not target_file.is_file():
            continue

        try:
            content = target_file.read_text(encoding="utf-8", errors="replace")
            req_lines = parse_requirements_content(content)
            modified = False
            new_lines: List[str] = []

            for rline in req_lines:
                if rline.package_name:
                    norm = normalize_package_name(rline.package_name)
                    if norm in approved_upgrades:
                        cand_ver = approved_upgrades[norm]
                        new_line_str = rline.reconstruct(new_version=cand_ver)
                        new_lines.append(new_line_str)
                        applied.append((m.relative_path, rline.package_name, cand_ver))
                        modified = True
                        continue
                new_lines.append(rline.raw_text)

            if modified:
                target_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        except Exception as exc:
            logger.warning(f"Failed to apply manifest upgrade to {m.relative_path}: {exc}")

    return applied


# ---------------------------------------------------------------------------
# Pipeline Orchestrator Core
# ---------------------------------------------------------------------------

class PipelineOrchestrator:
    """Coordinates the eight-stage repository analysis, improvement, and export pipeline."""

    def __init__(
        self,
        git_service: Optional[GitService] = None,
        workspace_manager: Optional[WorkspaceManager] = None,
        ollama_client: Optional[OllamaClient] = None,
        catalog: Optional[Catalog] = None,
        sandbox_manager: Optional[DockerSandboxManager] = None,
        artifacts_output_dir: Optional[Path | str] = None,
    ) -> None:
        self.git_service = git_service or GitService()
        self.workspace_manager = workspace_manager
        self.ollama_client = ollama_client
        self.catalog = catalog or load_catalog()
        self.sandbox_manager = sandbox_manager
        self.artifacts_output_dir = artifacts_output_dir

    def _notify(
        self,
        callback: Optional[ProgressCallback],
        record: StageRecord,
    ) -> None:
        """Deliver stage progress update to the structured callback."""
        if callback:
            try:
                callback(record.stage, record.status, record.message, record.details)
            except Exception as exc:
                logger.warning(f"Error in pipeline progress callback: {exc}")

    def run_analysis_stages(
        self,
        ws: WorkspaceSession,
        repo_url: str,
        pat_token: Optional[str] = None,
        ollama_model: Optional[str] = None,
        inspection_limit: int = 10,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_token: Optional[Callable[[], bool]] = None,
    ) -> PipelineResult:
        """Run Stages 1 through 5: repository acquisition, manifest discovery, dependency evaluation, risk analysis, and source inspection."""
        result = PipelineResult(repo_url=repo_url)
        for stage in PipelineStage:
            result.stage_records[stage] = StageRecord(stage=stage, status=StageStatus.PENDING)

        def is_cancelled() -> bool:
            return cancel_token is not None and cancel_token()

        reference_dir = ws.reference_dir
        working_dir = ws.working_dir

        # -----------------------------------------------------------
        # STAGE 1: ACQUIRE_REPO
        # -----------------------------------------------------------
        stage1 = result.get_stage_record(PipelineStage.ACQUIRE_REPO)
        stage1.status = StageStatus.RUNNING
        stage1.started_at = time.time()
        stage1.message = "Cloning repository into isolated workspace..."
        self._notify(progress_callback, stage1)

        if is_cancelled():
            stage1.status = StageStatus.SKIPPED
            stage1.message = "Pipeline cancelled before repository acquisition."
            self._notify(progress_callback, stage1)
            result.is_partial = True
            return result

        try:
            self.git_service.clone_repository(
                repo_url=repo_url,
                target_dir=reference_dir,
                pat=pat_token,
            )
            ws.copy_reference_to_working()
            commit_sha = self.git_service.get_head_commit(reference_dir)
            result.commit_sha = commit_sha

            stage1.status = StageStatus.COMPLETED
            stage1.completed_at = time.time()
            stage1.duration_seconds = stage1.completed_at - stage1.started_at
            stage1.message = f"Cloned successfully (Commit: {commit_sha[:8] if commit_sha else 'unknown'})."
            stage1.details = {"commit_sha": commit_sha}
            self._notify(progress_callback, stage1)

        except GitError as git_err:
            stage1.status = StageStatus.FAILED
            stage1.completed_at = time.time()
            stage1.duration_seconds = stage1.completed_at - stage1.started_at
            stage1.error = str(git_err)
            stage1.message = f"Git clone failed: {git_err}"
            self._notify(progress_callback, stage1)
            result.error = str(git_err)
            result.is_partial = True
            self._skip_remaining_stages(result, PipelineStage.DISCOVER_MANIFESTS, progress_callback)
            return result
        except Exception as exc:
            stage1.status = StageStatus.FAILED
            stage1.completed_at = time.time()
            stage1.error = str(exc)
            stage1.message = f"Unexpected acquisition failure: {exc}"
            self._notify(progress_callback, stage1)
            result.error = str(exc)
            result.is_partial = True
            self._skip_remaining_stages(result, PipelineStage.DISCOVER_MANIFESTS, progress_callback)
            return result

        # Baseline reference hash
        result.baseline_hashes = compute_directory_manifest_hash(reference_dir)

        # -----------------------------------------------------------
        # STAGE 2: DISCOVER_MANIFESTS
        # -----------------------------------------------------------
        stage2 = result.get_stage_record(PipelineStage.DISCOVER_MANIFESTS)
        stage2.status = StageStatus.RUNNING
        stage2.started_at = time.time()
        stage2.message = "Scanning repository for dependency manifests..."
        self._notify(progress_callback, stage2)

        if is_cancelled():
            stage2.status = StageStatus.SKIPPED
            stage2.message = "Pipeline cancelled."
            self._notify(progress_callback, stage2)
            result.is_partial = True
            return result

        try:
            scan_res = scan_manifests(working_dir)
            manifests = scan_res.manifests
            result.manifests = manifests

            stage2.status = StageStatus.COMPLETED
            stage2.completed_at = time.time()
            stage2.duration_seconds = stage2.completed_at - stage2.started_at
            stage2.message = f"Discovered {len(manifests)} dependency manifest(s)."
            stage2.details = {"manifest_count": len(manifests), "files": [m.relative_path for m in manifests]}
            self._notify(progress_callback, stage2)

        except Exception as exc:
            stage2.status = StageStatus.FAILED
            stage2.completed_at = time.time()
            stage2.error = str(exc)
            stage2.message = f"Manifest discovery failed: {exc}"
            self._notify(progress_callback, stage2)
            result.warnings.append(f"Manifest scanner error: {exc}")

        # -----------------------------------------------------------
        # STAGE 3: EVALUATE_DEPENDENCIES
        # -----------------------------------------------------------
        stage3 = result.get_stage_record(PipelineStage.EVALUATE_DEPENDENCIES)
        stage3.status = StageStatus.RUNNING
        stage3.started_at = time.time()
        stage3.message = "Querying PyPI and catalog metadata for candidate upgrades..."
        self._notify(progress_callback, stage3)

        if is_cancelled():
            stage3.status = StageStatus.SKIPPED
            stage3.message = "Pipeline cancelled."
            self._notify(progress_callback, stage3)
            result.is_partial = True
            return result

        try:
            meta_client = PyPIMetadataClient(catalog=self.catalog)
            dep_analyzer = DependencyAnalyzer(metadata_client=meta_client)
            dep_report = dep_analyzer.analyze_repository(result.manifests, enable_ai_risk=False)
            result.dependency_report = dep_report

            stage3.status = StageStatus.COMPLETED
            stage3.completed_at = time.time()
            stage3.duration_seconds = stage3.completed_at - stage3.started_at
            stage3.message = (
                f"Analyzed {dep_report.total_dependencies} dependencies: "
                f"{dep_report.total_outdated} upgrade candidate(s), "
                f"{dep_report.total_conflicts} conflict(s)."
            )
            stage3.details = {
                "total_dependencies": dep_report.total_dependencies,
                "total_outdated": dep_report.total_outdated,
                "total_conflicts": dep_report.total_conflicts,
            }
            self._notify(progress_callback, stage3)

        except Exception as exc:
            stage3.status = StageStatus.FAILED
            stage3.completed_at = time.time()
            stage3.error = str(exc)
            stage3.message = f"Dependency evaluation failed: {exc}"
            self._notify(progress_callback, stage3)
            result.warnings.append(f"Dependency evaluation error: {exc}")

        # -----------------------------------------------------------
        # STAGE 4: AI_RISK_ANALYSIS
        # -----------------------------------------------------------
        stage4 = result.get_stage_record(PipelineStage.AI_RISK_ANALYSIS)
        stage4.status = StageStatus.RUNNING
        stage4.started_at = time.time()

        if is_cancelled():
            stage4.status = StageStatus.SKIPPED
            stage4.message = "Pipeline cancelled."
            self._notify(progress_callback, stage4)
            result.is_partial = True
            return result

        if not self.ollama_client or not ollama_model:
            stage4.status = StageStatus.SKIPPED
            stage4.completed_at = time.time()
            stage4.duration_seconds = stage4.completed_at - stage4.started_at
            stage4.message = "Ollama model not specified; advisory AI risk analysis skipped."
            self._notify(progress_callback, stage4)
        else:
            stage4.message = f"Evaluating compatibility risks with Ollama ({ollama_model})..."
            self._notify(progress_callback, stage4)

            try:
                health = self.ollama_client.check_health()
                if not health.is_available:
                    health_msg = health.error_message or "Ollama service unavailable."
                    stage4.status = StageStatus.FAILED
                    stage4.completed_at = time.time()
                    stage4.duration_seconds = stage4.completed_at - stage4.started_at
                    stage4.message = f"Ollama service unavailable: {health_msg}. Advisory analysis skipped."
                    stage4.error = health_msg
                    self._notify(progress_callback, stage4)
                    result.warnings.append(f"Ollama unavailable for risk analysis: {health_msg}")
                else:
                    risk_count = 0
                    if result.dependency_report:
                        for m_analysis in result.dependency_report.manifest_analyses:
                            for dep in m_analysis.dependencies:
                                if dep.has_candidate_upgrade and dep.current_version and dep.candidate_version:
                                    dep.ai_risk_assessment = assess_upgrade_risk_with_ollama(
                                        client=self.ollama_client,
                                        model=ollama_model,
                                        package_name=dep.package_name,
                                        current_version=dep.current_version,
                                        candidate_version=dep.candidate_version,
                                    )
                                    risk_count += 1

                    stage4.status = StageStatus.COMPLETED
                    stage4.completed_at = time.time()
                    stage4.duration_seconds = stage4.completed_at - stage4.started_at
                    stage4.message = f"Completed AI risk assessment for {risk_count} candidate package(s)."
                    stage4.details = {"assessed_packages": risk_count}
                    self._notify(progress_callback, stage4)

            except Exception as exc:
                stage4.status = StageStatus.FAILED
                stage4.completed_at = time.time()
                stage4.error = str(exc)
                stage4.message = f"AI risk analysis encountered an error: {exc}"
                self._notify(progress_callback, stage4)
                result.warnings.append(f"AI risk analysis error: {exc}")

        # -----------------------------------------------------------
        # STAGE 5: INSPECT_SOURCE_CODE
        # -----------------------------------------------------------
        stage5 = result.get_stage_record(PipelineStage.INSPECT_SOURCE_CODE)
        stage5.status = StageStatus.RUNNING
        stage5.started_at = time.time()
        stage5.message = f"Inspecting priority Python files (limit: {inspection_limit})..."
        self._notify(progress_callback, stage5)

        if is_cancelled():
            stage5.status = StageStatus.SKIPPED
            stage5.message = "Pipeline cancelled."
            self._notify(progress_callback, stage5)
            result.is_partial = True
            return result

        upgraded_pkgs: Set[str] = set()
        if result.dependency_report:
            for ma in result.dependency_report.manifest_analyses:
                for d in ma.dependencies:
                    if d.has_candidate_upgrade:
                        upgraded_pkgs.add(d.package_name)

        try:
            inspector = CodeInspector(
                ollama_client=self.ollama_client,
                ollama_model=ollama_model,
                inspection_limit=inspection_limit,
            )
            enable_ai_inspect = bool(self.ollama_client and ollama_model)
            insp_report = inspector.inspect_repository(
                repo_dir=working_dir,
                upgraded_packages=upgraded_pkgs,
                enable_ai=enable_ai_inspect,
            )
            result.inspection_report = insp_report

            # Synthesize proposed improvements with AST pre-validation
            proposed_list: List[ProposedImprovement] = []
            if self.ollama_client and ollama_model:
                improver = CodeImprover(ollama_client=self.ollama_client, model=ollama_model)
                for finding in insp_report.findings:
                    if is_cancelled():
                        break
                    prop = improver.propose_improvement(finding, working_dir)
                    proposed_list.append(prop)

            result.proposed_improvements = proposed_list

            stage5.status = StageStatus.COMPLETED
            stage5.completed_at = time.time()
            stage5.duration_seconds = stage5.completed_at - stage5.started_at
            stage5.message = (
                f"Inspected {len(insp_report.inspected_files)} file(s), "
                f"discovered {len(insp_report.findings)} finding(s), "
                f"synthesized {len(proposed_list)} proposed fix(es)."
            )
            stage5.details = {
                "inspected_files": [f.relative_path for f in insp_report.inspected_files],
                "findings_count": len(insp_report.findings),
                "proposed_fixes_count": len(proposed_list),
            }
            self._notify(progress_callback, stage5)

        except Exception as exc:
            stage5.status = StageStatus.FAILED
            stage5.completed_at = time.time()
            stage5.error = str(exc)
            stage5.message = f"Code inspection failed: {exc}"
            self._notify(progress_callback, stage5)
            result.warnings.append(f"Code inspection error: {exc}")

        return result

    def run_improvement_and_export_stages(
        self,
        ws: WorkspaceSession,
        result: PipelineResult,
        approved_improvements: Optional[List[ProposedImprovement]] = None,
        approval_callback: Optional[ApprovalCallback] = None,
        approved_upgrades: Optional[Dict[str, str]] = None,
        ollama_model: Optional[str] = None,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_token: Optional[Callable[[], bool]] = None,
        run_sandbox_tests: bool = True,
    ) -> PipelineResult:
        """Run Stages 6 through 8: apply improvements, sandbox validation, and artifact export."""
        def is_cancelled() -> bool:
            return cancel_token is not None and cancel_token()

        reference_dir = ws.reference_dir
        working_dir = ws.working_dir

        # -----------------------------------------------------------
        # STAGE 6: APPLY_IMPROVEMENTS (HUMAN APPROVAL GATE)
        # -----------------------------------------------------------
        stage6 = result.get_stage_record(PipelineStage.APPLY_IMPROVEMENTS)
        stage6.status = StageStatus.RUNNING
        stage6.started_at = time.time()

        if is_cancelled():
            stage6.status = StageStatus.SKIPPED
            stage6.message = "Pipeline cancelled."
            self._notify(progress_callback, stage6)
            result.is_partial = True
            return result

        # Human Approval Resolution
        selected_improvements: List[ProposedImprovement] = []
        if approval_callback is not None:
            stage6.message = "Waiting for human review of proposed improvements..."
            self._notify(progress_callback, stage6)
            try:
                selected_improvements = approval_callback(result.proposed_improvements)
            except Exception as exc:
                stage6.status = StageStatus.FAILED
                stage6.error = f"Approval callback error: {exc}"
                stage6.message = f"Human approval callback failed: {exc}"
                self._notify(progress_callback, stage6)
                result.warnings.append(f"Approval callback failed: {exc}")
                selected_improvements = []
        elif approved_improvements is not None:
            selected_improvements = approved_improvements
        else:
            selected_improvements = []

        result.approved_improvements = selected_improvements

        # Apply approved improvements strictly to working directory
        applied_list: List[ProposedImprovement] = []
        if self.ollama_client and ollama_model:
            improver = CodeImprover(ollama_client=self.ollama_client, model=ollama_model)
            for prop in selected_improvements:
                comm = improver.commit_improvement(prop, working_dir)
                applied_list.append(comm)

        result.applied_improvements = applied_list

        # Apply approved dependency manifest upgrades if specified
        if approved_upgrades:
            applied_manifests = apply_approved_manifest_upgrades(
                working_dir=working_dir,
                manifests=result.manifests,
                approved_upgrades=approved_upgrades,
            )
            result.applied_manifest_upgrades = applied_manifests

        # Verify reference copy was never mutated
        if result.baseline_hashes:
            ref_ok, discrepancies = verify_reference_preservation(reference_dir, result.baseline_hashes)
            if not ref_ok:
                stage6.status = StageStatus.FAILED
                stage6.error = f"Reference directory integrity violation: {', '.join(discrepancies)}"
                stage6.message = "Security fault: pristine reference repository was modified!"
                self._notify(progress_callback, stage6)
                result.error = stage6.error
                result.is_partial = True
                self._skip_remaining_stages(result, PipelineStage.VALIDATE_CHANGES, progress_callback)
                return result

        stage6.status = StageStatus.COMPLETED
        stage6.completed_at = time.time()
        stage6.duration_seconds = stage6.completed_at - stage6.started_at
        successful_applied = sum(1 for a in applied_list if a.is_applied)
        stage6.message = (
            f"Applied {successful_applied}/{len(selected_improvements)} approved code improvement(s), "
            f"{len(result.applied_manifest_upgrades)} manifest upgrade(s)."
        )
        stage6.details = {
            "approved_count": len(selected_improvements),
            "applied_count": successful_applied,
            "manifest_upgrades_count": len(result.applied_manifest_upgrades),
        }
        self._notify(progress_callback, stage6)

        # -----------------------------------------------------------
        # STAGE 7: VALIDATE_CHANGES
        # -----------------------------------------------------------
        stage7 = result.get_stage_record(PipelineStage.VALIDATE_CHANGES)
        stage7.status = StageStatus.RUNNING
        stage7.started_at = time.time()
        stage7.message = "Running isolated repository validation..."
        self._notify(progress_callback, stage7)

        if is_cancelled():
            stage7.status = StageStatus.SKIPPED
            stage7.message = "Pipeline cancelled."
            self._notify(progress_callback, stage7)
            result.is_partial = True
            return result

        try:
            runner = SandboxedValidationRunner(sandbox_manager=self.sandbox_manager)
            validator = RepositoryValidator(runner=runner)
            val_report = validator.validate_repository(
                working_dir=working_dir,
                run_tests=run_sandbox_tests,
                run_linter=True,
            )
            result.validation_report = val_report

            stage7.status = StageStatus.COMPLETED
            stage7.completed_at = time.time()
            stage7.duration_seconds = stage7.completed_at - stage7.started_at
            stage7.message = f"Validation completed: status is {val_report.overall_status.value}."
            stage7.details = {
                "overall_status": val_report.overall_status.value,
                "passed_checks": val_report.passed_count,
                "failed_checks": val_report.failed_count,
                "not_runnable": val_report.not_runnable_count,
            }
            self._notify(progress_callback, stage7)

        except Exception as exc:
            stage7.status = StageStatus.FAILED
            stage7.completed_at = time.time()
            stage7.error = str(exc)
            stage7.message = f"Validation runner encountered an exception: {exc}"
            self._notify(progress_callback, stage7)
            result.warnings.append(f"Validation runner error: {exc}")

        # -----------------------------------------------------------
        # STAGE 8: GENERATE_ARTIFACTS
        # -----------------------------------------------------------
        stage8 = result.get_stage_record(PipelineStage.GENERATE_ARTIFACTS)
        stage8.status = StageStatus.RUNNING
        stage8.started_at = time.time()
        stage8.message = "Synthesizing and validating output artifacts (.patch, .zip, report)..."
        self._notify(progress_callback, stage8)

        if is_cancelled():
            stage8.status = StageStatus.SKIPPED
            stage8.message = "Pipeline cancelled."
            self._notify(progress_callback, stage8)
            result.is_partial = True
            return result

        try:
            exporter = ArtifactExporter(output_dir=self.artifacts_output_dir)
            deliverables = exporter.export_all(
                reference_dir=reference_dir,
                working_dir=working_dir,
                repo_url=result.repo_url,
                commit_sha=result.commit_sha,
                manifest_report=result.dependency_report,
                inspection_report=result.inspection_report,
                improvements=result.applied_improvements or result.proposed_improvements,
                validation_report=result.validation_report,
                ollama_model=ollama_model,
            )
            result.export_deliverables = deliverables

            stage8.status = StageStatus.COMPLETED
            stage8.completed_at = time.time()
            stage8.duration_seconds = stage8.completed_at - stage8.started_at
            stage8.message = (
                f"Generated deliverables: patch valid={deliverables.patch_result.is_valid}, "
                f"zip valid={deliverables.zip_result.is_valid} ({deliverables.zip_result.file_count} files)."
            )
            stage8.details = {
                "patch_valid": deliverables.patch_result.is_valid,
                "zip_valid": deliverables.zip_result.is_valid,
                "modified_files": deliverables.patch_result.modified_files,
            }
            self._notify(progress_callback, stage8)

        except Exception as exc:
            stage8.status = StageStatus.FAILED
            stage8.completed_at = time.time()
            stage8.error = str(exc)
            stage8.message = f"Artifact generation failed: {exc}"
            self._notify(progress_callback, stage8)
            result.error = f"Artifact export failure: {exc}"
            result.is_partial = True

        stage1 = result.get_stage_record(PipelineStage.ACQUIRE_REPO)
        result.overall_success = (
            stage1.status == StageStatus.COMPLETED
            and (result.validation_report is None or not result.validation_report.is_failed)
            and stage8.status == StageStatus.COMPLETED
        )

        return result

    def run(
        self,
        repo_url: str,
        pat_token: Optional[str] = None,
        ollama_model: Optional[str] = None,
        inspection_limit: int = 10,
        approved_improvements: Optional[List[ProposedImprovement]] = None,
        approval_callback: Optional[ApprovalCallback] = None,
        approved_upgrades: Optional[Dict[str, str]] = None,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_token: Optional[Callable[[], bool]] = None,
        run_sandbox_tests: bool = True,
    ) -> PipelineResult:
        """Execute the full eight-stage pipeline with strict safety, isolation, and approval controls."""
        ws_mgr = self.workspace_manager or WorkspaceManager()

        with ws_mgr.create_workspace() as ws:
            analysis_res = self.run_analysis_stages(
                ws=ws,
                repo_url=repo_url,
                pat_token=pat_token,
                ollama_model=ollama_model,
                inspection_limit=inspection_limit,
                progress_callback=progress_callback,
                cancel_token=cancel_token,
            )

            # If Stage 1 or analysis failed/cancelled, stop and return
            if analysis_res.is_partial or analysis_res.error:
                return analysis_res

            # Stages 6 through 8
            final_res = self.run_improvement_and_export_stages(
                ws=ws,
                result=analysis_res,
                approved_improvements=approved_improvements,
                approval_callback=approval_callback,
                approved_upgrades=approved_upgrades,
                ollama_model=ollama_model,
                progress_callback=progress_callback,
                cancel_token=cancel_token,
                run_sandbox_tests=run_sandbox_tests,
            )
            return final_res

    def _skip_remaining_stages(
        self,
        result: PipelineResult,
        starting_stage: PipelineStage,
        progress_callback: Optional[ProgressCallback],
    ) -> None:
        """Mark remaining stages as SKIPPED when an unrecoverable failure occurs."""
        skip = False
        for stage in PipelineStage:
            if stage == starting_stage:
                skip = True
            if skip:
                rec = result.get_stage_record(stage)
                if rec.status == StageStatus.PENDING:
                    rec.status = StageStatus.SKIPPED
                    rec.message = "Skipped due to earlier pipeline failure."
                    self._notify(progress_callback, rec)
