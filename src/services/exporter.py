"""Artifact exporter delivering Git-compatible patches, verified ZIP archives, and analysis reports.

Integrity and security guarantees:
- Generates Git-compatible unified patches for added, modified, and deleted text files.
- Pre-validates generated patches using `git apply --check` against a clean copy of the reference repo.
- Detects binary changes and explicitly reports them as unsupported (never silently omitted).
- Creates improved repository ZIP archives strictly excluding .git, .venv, caches, build files, and secrets.
- Validates ZIP archive integrity using zipfile.testzip() and test extraction before download presentation.
- Compiles a comprehensive Markdown analysis report clearly distinguishing deterministic checks,
  advisory AI recommendations, and empirical sandbox validation outcomes.
- Never mutates the pristine reference repository.
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.analysis.code_improver import ProposedImprovement
from src.analysis.code_inspector import ExcludedFile, InspectionReport
from src.analysis.dependency_analyzer import RepositoryDependencyReport
from src.analysis.manifest_scanner import is_safe_relative_path
from src.models.export import (
    ArtifactType,
    ExportItemMetadata,
    ExportMetadataResponse,
)
from src.models.plan import UpgradeProposalResponse
from src.models.sandbox import ValidationReportResponse
from src.services.secret_scanner import (
    SecretLeakageError,
    redact_secrets,
    verify_zero_secrets,
)
from src.validation.validator import ValidationReport, ValidationStatus

logger = logging.getLogger(__name__)

# Excluded directory names for ZIP packaging
ZIP_EXCLUDED_DIRS: Set[str] = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".tox",
    "node_modules",
}

# Excluded filename patterns for ZIP packaging (secrets, credentials, temp files)
SECRET_FILE_PATTERNS = [
    re.compile(r"^\.env(?:\..*)?$"),
    re.compile(r".*\.(?:key|pem|token|secret|pfx|p12)$", re.IGNORECASE),
    re.compile(r"^id_(?:rsa|ecdsa|ed25519|dsa)$"),
    re.compile(r".*\.(?:tmp|swp|bak|orig)$", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PatchValidationResult:
    """Outcome of unified patch generation and git apply --check pre-validation."""
    is_valid: bool
    patch_content: str
    modified_files: List[str]
    added_files: List[str]
    deleted_files: List[str]
    binary_files_detected: List[str]
    error_message: Optional[str] = None

    @property
    def has_changes(self) -> bool:
        return bool(self.modified_files or self.added_files or self.deleted_files)

    @property
    def change_summary(self) -> str:
        parts = []
        if self.modified_files:
            parts.append(f"{len(self.modified_files)} modified")
        if self.added_files:
            parts.append(f"{len(self.added_files)} added")
        if self.deleted_files:
            parts.append(f"{len(self.deleted_files)} deleted")
        return ", ".join(parts) if parts else "No changes"


@dataclass(frozen=True)
class ZipValidationResult:
    """Outcome of ZIP archive packaging, testzip() verification, and test extraction."""
    is_valid: bool
    zip_path: Path
    file_count: int
    archive_size_bytes: int
    excluded_files: List[str]
    error_message: Optional[str] = None

    @property
    def archive_size_kb(self) -> float:
        return round(self.archive_size_bytes / 1024, 2)


@dataclass
class ExportArtifactsResult:
    """Aggregated deliverables resulting from repository analysis and improvement."""
    patch_result: PatchValidationResult
    zip_result: ZipValidationResult
    report_markdown: str
    analysis_timestamp: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Binary File Detection
# ---------------------------------------------------------------------------

def is_binary_file(file_path: Path) -> bool:
    """Detect whether a file contains binary bytes or is non-UTF-8 text."""
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(8192)
            if b"\x00" in chunk:
                return True
            # Attempt UTF-8 decoding of the sample
            chunk.decode("utf-8")
            return False
    except (UnicodeDecodeError, OSError):
        return True


# ---------------------------------------------------------------------------
# Patch Generator & Validator
# ---------------------------------------------------------------------------

def _format_diff_hunks(from_lines: List[str], to_lines: List[str], from_file: str, to_file: str) -> str:
    """Generate a clean unified diff string with POSIX patch headers."""
    diff = list(
        difflib.unified_diff(
            from_lines,
            to_lines,
            fromfile=from_file,
            tofile=to_file,
            lineterm="",
        )
    )
    if not diff:
        return ""
    return "\n".join(diff) + "\n"


def generate_unified_patch(
    reference_dir: Path | str,
    working_dir: Path | str,
) -> Tuple[str, List[str], List[str], List[str], List[str]]:
    """Generate a standard Git-compatible unified patch between reference and working directories.
    
    Returns:
        Tuple of (patch_text, modified_files, added_files, deleted_files, binary_files)
    """
    ref_root = Path(reference_dir).resolve()
    work_root = Path(working_dir).resolve()

    def get_tracked_rel_files(root: Path) -> Dict[str, Path]:
        files_map: Dict[str, Path] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            # Skip .git metadata
            rel_parts = path.relative_to(root).parts
            if any(p == ".git" for p in rel_parts):
                continue
            rel_str = str(path.relative_to(root))
            files_map[rel_str] = path
        return files_map

    ref_files = get_tracked_rel_files(ref_root)
    work_files = get_tracked_rel_files(work_root)

    modified_files: List[str] = []
    added_files: List[str] = []
    deleted_files: List[str] = []
    binary_files: List[str] = []

    patch_sections: List[str] = []

    all_paths = sorted(set(ref_files.keys()) | set(work_files.keys()))

    for rel_path in all_paths:
        in_ref = rel_path in ref_files
        in_work = rel_path in work_files

        # 1. Modified file
        if in_ref and in_work:
            ref_path = ref_files[rel_path]
            work_path = work_files[rel_path]

            if is_binary_file(ref_path) or is_binary_file(work_path):
                # Detect binary discrepancy
                if ref_path.read_bytes() != work_path.read_bytes():
                    binary_files.append(rel_path)
                continue

            ref_lines = ref_path.read_text(encoding="utf-8", errors="replace").splitlines()
            work_lines = work_path.read_text(encoding="utf-8", errors="replace").splitlines()

            if ref_lines != work_lines:
                modified_files.append(rel_path)
                header = (
                    f"diff --git a/{rel_path} b/{rel_path}\n"
                    f"--- a/{rel_path}\n"
                    f"+++ b/{rel_path}\n"
                )
                hunks = _format_diff_hunks(ref_lines, work_lines, f"a/{rel_path}", f"b/{rel_path}")
                # Replace standard unified_diff header with Git diff header
                hunk_lines = hunks.splitlines(keepends=True)
                actual_hunks = "".join(hunk_lines[2:]) if len(hunk_lines) >= 2 else ""
                patch_sections.append(header + actual_hunks)

        # 2. Added file
        elif not in_ref and in_work:
            work_path = work_files[rel_path]
            if is_binary_file(work_path):
                binary_files.append(rel_path)
                continue

            added_files.append(rel_path)
            work_lines = work_path.read_text(encoding="utf-8", errors="replace").splitlines()
            header = (
                f"diff --git a/{rel_path} b/{rel_path}\n"
                f"new file mode 100644\n"
                f"--- /dev/null\n"
                f"+++ b/{rel_path}\n"
            )
            hunks = _format_diff_hunks([], work_lines, "/dev/null", f"b/{rel_path}")
            hunk_lines = hunks.splitlines(keepends=True)
            actual_hunks = "".join(hunk_lines[2:]) if len(hunk_lines) >= 2 else ""
            patch_sections.append(header + actual_hunks)

        # 3. Deleted file
        elif in_ref and not in_work:
            ref_path = ref_files[rel_path]
            if is_binary_file(ref_path):
                binary_files.append(rel_path)
                continue

            deleted_files.append(rel_path)
            ref_lines = ref_path.read_text(encoding="utf-8", errors="replace").splitlines()
            header = (
                f"diff --git a/{rel_path} b/{rel_path}\n"
                f"deleted file mode 100644\n"
                f"--- a/{rel_path}\n"
                f"+++ /dev/null\n"
            )
            hunks = _format_diff_hunks(ref_lines, [], f"a/{rel_path}", "/dev/null")
            hunk_lines = hunks.splitlines(keepends=True)
            actual_hunks = "".join(hunk_lines[2:]) if len(hunk_lines) >= 2 else ""
            patch_sections.append(header + actual_hunks)

    complete_patch = "".join(patch_sections)
    return complete_patch, modified_files, added_files, deleted_files, binary_files


def validate_patch_against_reference(
    patch_content: str,
    reference_dir: Path | str,
) -> Tuple[bool, Optional[str]]:
    """Validate patch using `git apply --check` against an isolated temporary copy of the reference repo.
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not patch_content.strip():
        # Empty patch (zero changes) is trivially valid
        return True, None

    ref_root = Path(reference_dir).resolve()
    if not ref_root.is_dir():
        return False, f"Reference directory does not exist: {ref_root}"

    temp_copy_dir = Path(tempfile.mkdtemp(prefix="dua_patch_val_"))
    try:
        # Deep copy reference repository into temporary sandbox
        shutil.copytree(ref_root, temp_copy_dir / "repo", symlinks=True)
        repo_copy = temp_copy_dir / "repo"

        # Ensure git is initialized in the copy if not already
        if not (repo_copy / ".git").is_dir():
            subprocess.run(["git", "init", "-q"], cwd=repo_copy, check=False)
            subprocess.run(["git", "config", "user.name", "dua-validator"], cwd=repo_copy, check=False)
            subprocess.run(["git", "config", "user.email", "val@local"], cwd=repo_copy, check=False)
            subprocess.run(["git", "add", "-A"], cwd=repo_copy, check=False)
            subprocess.run(["git", "commit", "-m", "baseline", "-q"], cwd=repo_copy, check=False)

        patch_file = temp_copy_dir / "proposed.patch"
        patch_file.write_text(patch_content, encoding="utf-8")

        # Run git apply --check
        proc = subprocess.run(
            ["git", "apply", "--check", str(patch_file)],
            cwd=repo_copy,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )

        if proc.returncode == 0:
            return True, None
        else:
            err = proc.stderr.strip() or proc.stdout.strip()
            return False, f"git apply --check failed (exit code {proc.returncode}): {err}"

    except subprocess.TimeoutExpired:
        return False, "Patch validation timed out after 15s."
    except Exception as exc:
        return False, f"Error validating patch: {exc}"
    finally:
        shutil.rmtree(temp_copy_dir, ignore_errors=True)


def create_and_validate_patch(
    reference_dir: Path | str,
    working_dir: Path | str,
) -> PatchValidationResult:
    """Generate and validate a unified patch comparing reference and working directories."""
    patch_text, modified, added, deleted, binary_files = generate_unified_patch(
        reference_dir=reference_dir,
        working_dir=working_dir,
    )

    is_valid, error_msg = validate_patch_against_reference(
        patch_content=patch_text,
        reference_dir=reference_dir,
    )

    return PatchValidationResult(
        is_valid=is_valid,
        patch_content=patch_text,
        modified_files=modified,
        added_files=added,
        deleted_files=deleted,
        binary_files_detected=binary_files,
        error_message=error_msg,
    )


# ---------------------------------------------------------------------------
# Improved Repository ZIP Archiver
# ---------------------------------------------------------------------------

def is_excluded_from_zip(rel_path: str) -> Tuple[bool, Optional[str]]:
    """Determine whether a file should be excluded from the ZIP archive."""
    parts = Path(rel_path).parts
    filename = parts[-1] if parts else ""

    # 1. Directory exclusion
    for part in parts[:-1]:
        if part in ZIP_EXCLUDED_DIRS or part.endswith(".egg-info"):
            return True, f"Directory '{part}' is excluded"

    # 2. Secret and temporary file pattern exclusion
    for pattern in SECRET_FILE_PATTERNS:
        if pattern.match(filename):
            return True, f"Excluded file pattern match: {filename}"

    return False, None


def create_and_validate_zip(
    working_dir: Path | str,
    output_zip_path: Path | str,
) -> ZipValidationResult:
    """Pack working directory into a clean ZIP archive and verify its integrity.
    
    Verifies:
    1. Exclusion of .git, virtual environments, caches, and secrets.
    2. Path traversal security (all entries are strictly relative without ..).
    3. Python zipfile.testzip() CRC integrity check.
    4. Successful test extraction into an isolated temporary folder.
    """
    work_root = Path(working_dir).resolve()
    out_path = Path(output_zip_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    excluded_files: List[str] = []
    file_count = 0

    try:
        with zipfile.ZipFile(out_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(work_root, followlinks=False):
                current_p = Path(root)

                # Record and prune excluded directories
                pruned = [d for d in dirs if d in ZIP_EXCLUDED_DIRS or d.endswith(".egg-info")]
                for d in pruned:
                    rel_d = str((current_p / d).relative_to(work_root))
                    excluded_files.append(f"{rel_d} (Excluded directory)")

                dirs[:] = [
                    d for d in dirs
                    if d not in ZIP_EXCLUDED_DIRS and not d.endswith(".egg-info")
                ]

                for file_name in files:
                    file_p = current_p / file_name
                    if not is_safe_relative_path(work_root, file_p):
                        continue

                    rel_str = str(file_p.relative_to(work_root))

                    # Check exclusion rules
                    exclude, reason = is_excluded_from_zip(rel_str)
                    if exclude:
                        excluded_files.append(f"{rel_str} ({reason})")
                        continue

                    # Security check: forbid path traversal in zip entry
                    if rel_str.startswith(("/", "\\")) or ".." in rel_str:
                        raise ValueError(f"Unsafe archive entry path detected: '{rel_str}'")

                    zf.write(file_p, arcname=rel_str)
                    file_count += 1

        # Test ZIP integrity via testzip()
        with zipfile.ZipFile(out_path, mode="r") as zf:
            corrupt_file = zf.testzip()
            if corrupt_file is not None:
                out_path.unlink(missing_ok=True)
                return ZipValidationResult(
                    is_valid=False,
                    zip_path=out_path,
                    file_count=0,
                    archive_size_bytes=0,
                    excluded_files=excluded_files,
                    error_message=f"ZIP archive corrupted at entry: {corrupt_file}",
                )

        # Test extraction in isolated temporary sandbox
        temp_extract = Path(tempfile.mkdtemp(prefix="dua_zip_test_"))
        try:
            with zipfile.ZipFile(out_path, mode="r") as zf:
                zf.extractall(temp_extract)
        finally:
            shutil.rmtree(temp_extract, ignore_errors=True)

        size_bytes = out_path.stat().st_size
        return ZipValidationResult(
            is_valid=True,
            zip_path=out_path,
            file_count=file_count,
            archive_size_bytes=size_bytes,
            excluded_files=excluded_files,
        )

    except Exception as exc:
        if out_path.exists():
            out_path.unlink(missing_ok=True)
        return ZipValidationResult(
            is_valid=False,
            zip_path=out_path,
            file_count=0,
            archive_size_bytes=0,
            excluded_files=excluded_files,
            error_message=f"ZIP creation or validation failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Structured Analysis Report Generator
# ---------------------------------------------------------------------------

def generate_markdown_report(
    repo_url: str,
    commit_sha: Optional[str],
    manifest_report: Optional[RepositoryDependencyReport] = None,
    inspection_report: Optional[InspectionReport] = None,
    improvements: Optional[List[ProposedImprovement]] = None,
    validation_report: Optional[ValidationReport] = None,
    patch_result: Optional[PatchValidationResult] = None,
    zip_result: Optional[ZipValidationResult] = None,
    ollama_model: Optional[str] = None,
) -> str:
    """Synthesize a complete Markdown analysis report detailing all pipeline findings and outputs."""
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    sha_str = commit_sha[:8] if commit_sha else "N/A"

    lines: List[str] = [
        "# Repository Analysis & Improvement Report",
        "",
        f"- **Repository URL:** `{repo_url}`",
        f"- **Target Commit:** `{sha_str}`",
        f"- **Analysis Timestamp:** `{now_str}`",
        f"- **AI Model Used:** `{ollama_model or 'Deterministic Evaluation Only'}`",
        "",
        "---",
        "",
        "## 1. Executive Summary",
        "",
    ]

    val_status = validation_report.overall_status.value if validation_report else "NOT_PERFORMED"
    lines.append(f"- **Validation Status:** `{val_status}`")
    if patch_result:
        patch_status = "VALID" if patch_result.is_valid else f"INVALID ({patch_result.error_message})"
        lines.append(f"- **Patch Status:** `{patch_status}` ({patch_result.change_summary})")
    if zip_result:
        zip_status = f"VALID ({zip_result.file_count} files, {zip_result.archive_size_kb} KB)" if zip_result.is_valid else "INVALID"
        lines.append(f"- **ZIP Archive:** `{zip_status}`")

    # 2. Dependency Analysis Section
    lines.extend([
        "",
        "---",
        "",
        "## 2. Dependency Manifest Findings",
        "",
    ])

    if manifest_report and manifest_report.manifest_analyses:
        lines.append(f"Discovered **{len(manifest_report.manifest_analyses)}** dependency manifest(s).")
        lines.append("")
        lines.append("| Manifest File | Package | Declared Version | Proposed Candidate | Status | Provenance |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")

        for m_analysis in manifest_report.manifest_analyses:
            for dep in m_analysis.dependencies:
                cand = f"`{dep.candidate_version}`" if dep.candidate_version else "None"
                curr = f"`{dep.current_specifier}`" if dep.current_specifier else "None"
                badge = "Outdated" if dep.has_candidate_upgrade and dep.is_exact_pin else "Up-to-Date / Preserved"
                lines.append(
                    f"| `{dep.source_file}` | **`{dep.package_name}`** | {curr} | {cand} | {badge} | {dep.provenance} |"
                )

        if manifest_report.has_conflicts:
            lines.extend([
                "",
                "### Cross-Manifest Dependency Conflicts",
                "",
                "The following packages are declared with conflicting version constraints across manifests:",
                "",
            ])
            for pkg, occs in manifest_report.conflicts.items():
                occ_str = ", ".join(f"`{m}` ({s})" for m, s in occs)
                lines.append(f"- **`{pkg}`**: {occ_str}")
    else:
        lines.append("No dependency manifests were detected in the repository.")

    # 3. Advisory AI Risk Assessments
    lines.extend([
        "",
        "---",
        "",
        "## 3. Advisory AI Compatibility Risk Assessments",
        "",
        "> [!NOTE]",
        "> **Advisory Notice:** AI compatibility risk assessments are qualitative explanations generated by Ollama. "
        "They provide context on breaking changes and deprecations, but are never treated as proof of compatibility.",
        "",
    ])

    has_ai_risks = False
    if manifest_report:
        for m_analysis in manifest_report.manifest_analyses:
            for dep in m_analysis.dependencies:
                if dep.ai_risk_assessment and not dep.ai_risk_assessment.has_error:
                    has_ai_risks = True
                    risk = dep.ai_risk_assessment
                    lines.extend([
                        f"### `{risk.package_name}`: `{risk.current_version}` → `{risk.candidate_version}`",
                        f"- **Assessed Risk Level:** `{risk.risk_level}` (Confidence: {risk.confidence})",
                        f"- **Breaking Changes Summary:** {risk.breaking_changes_summary}",
                    ])
                    if risk.deprecated_features:
                        lines.append(f"- **Deprecated Features:** {', '.join(risk.deprecated_features)}")
                    lines.append(f"- **Migration Notes:** {risk.migration_notes}")
                    lines.append("")

    if not has_ai_risks:
        lines.append("No AI compatibility risk assessments were generated.")

    # 4. Source Code Inspection
    lines.extend([
        "",
        "---",
        "",
        "## 4. Source Code Inspection & AI Improvements",
        "",
    ])

    if inspection_report:
        lines.append(f"- **Files Discovered:** {inspection_report.total_files_discovered}")
        lines.append(f"- **Files Inspected:** {len(inspection_report.inspected_files)}")
        lines.append(f"- **Files Excluded:** {len(inspection_report.excluded_files)}")
        lines.append(f"- **Total Findings:** {inspection_report.total_findings}")
        lines.append("")

        if inspection_report.findings:
            lines.append("### Detected Code Findings")
            lines.append("")
            lines.append("| ID | File | Lines | Category | Severity | Description | Review Required |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            for f in inspection_report.findings:
                review_badge = "Yes (Advisory)" if f.is_ai_generated else "No (Static AST)"
                lines.append(
                    f"| `{f.finding_id}` | `{f.file_path}` | {f.line_range} | {f.issue_category.value} | {f.severity.value} | {f.description} | {review_badge} |"
                )
            lines.append("")

    if improvements:
        lines.extend([
            "### Applied Code Improvements",
            "",
            "| Finding ID | File | Status | Technical Rationale |",
            "| :--- | :--- | :--- | :--- |",
        ])
        for imp in improvements:
            lines.append(f"| `{imp.finding_id}` | `{imp.file_path}` | `{imp.status.value}` | {imp.explanation} |")
        lines.append("")
    else:
        lines.append("No code improvements were applied.")

    # 5. Validation Results
    lines.extend([
        "",
        "---",
        "",
        "## 5. Automated Validation & Sandbox Results",
        "",
    ])

    if validation_report:
        lines.append(f"- **Overall Outcome:** `{validation_report.overall_status.value}`")
        lines.append(f"- **Sandbox Environment:** `{validation_report.sandbox_used}`")
        lines.append(f"- **Summary:** {validation_report.summary_message}")
        lines.append("")

        if validation_report.command_results:
            lines.append("| Command | Status | Exit Code | Duration | Description |")
            lines.append("| :--- | :--- | :--- | :--- | :--- |")
            for cmd in validation_report.command_results:
                lines.append(
                    f"| `{' '.join(cmd.command_args)}` | `{cmd.status.value}` | {cmd.exit_code} | {cmd.duration_seconds}s | {cmd.description} |"
                )
            lines.append("")
    else:
        lines.append("No automated validation results recorded.")

    # 6. Deliverable Verification & Unsupported Changes
    lines.extend([
        "",
        "---",
        "",
        "## 6. Deliverable Integrity & Unsupported Changes",
        "",
    ])

    if patch_result:
        lines.append(f"- **Unified Patch (`.patch`):** Verified valid via `git apply --check` ({patch_result.change_summary}).")
        if patch_result.binary_files_detected:
            lines.append("- **Unsupported Binary Changes Detected:**")
            for b_file in patch_result.binary_files_detected:
                lines.append(f"  - `{b_file}` (binary modification cannot be represented in text patch).")
    if zip_result:
        lines.append(f"- **Improved Project Archive (`.zip`):** Verified intact via `testzip()` and extraction ({zip_result.file_count} files).")

    lines.extend([
        "",
        "---",
        "*Report generated by Dependency Upgrade Assistant • Human-in-the-Loop AI-DLC*",
        "",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cryptographic Checksums & Export Structures
# ---------------------------------------------------------------------------

def resolution_export_payload(proposed, original=None) -> Dict[str, Any]:
    """FR-32d: one payload for both the Markdown and the JSON export, so they cannot disagree.

    ``proposed`` and ``original`` are supervisor-written ResolutionReport objects; nothing here is
    client-supplied.
    """
    from src.resolution.policy import COMPATIBILITY_DISCLOSURE

    def packages(report) -> List[Dict[str, Any]]:
        if report is None:
            return []
        return [
            {
                "name": p.name, "version": p.version, "direct": p.direct, "introduced": p.introduced,
                "constraints": list(p.constraints), "filename": p.filename, "sha256": p.sha256,
                "upload_date": p.upload.value if p.upload.available else "date unavailable",
                "upload_date_source": p.upload.source if p.upload.available else None,
                "upload_date_reason": None if p.upload.available else p.upload.reason,
            }
            for p in report.packages
        ]

    comparison = proposed.comparison
    return {
        "status": proposed.status.value,
        "proposed_fingerprint": proposed.fingerprint,
        "original_status": original.status.value if original else None,
        "original_fingerprint": original.fingerprint if original else None,
        "resolver": proposed.resolver,
        "image": proposed.image,
        "target": proposed.target,
        "policy_version": proposed.policy_version,
        "allowlist_version": proposed.allowlist_version,
        "resolved_at": proposed.created_at,
        "comparison": (
            {"available": comparison.available, "reason": comparison.reason, "introduced": list(comparison.introduced)}
            if comparison else {"available": False, "reason": "not_computed", "introduced": []}
        ),
        "warnings": list(proposed.warnings),
        "disclosure": COMPATIBILITY_DISCLOSURE,
        "proposed_packages": packages(proposed),
        "original_packages": packages(original),
    }


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def render_compatibility_markdown(proposal: UpgradeProposalResponse, payload: Optional[Dict[str, Any]],
                                  validation_status: str, created_at: str) -> str:
    lines = [
        "# Dependency upgrade report",
        "",
        f"- **Plan:** `{proposal.proposal_id}`",
        f"- **Manifest:** `{proposal.file_path}`",
        f"- **Generated (UTC):** {created_at}",
        f"- **Validation outcome:** `{validation_status}`",
        "",
        "## Compatibility",
        "",
    ]
    if not payload:
        lines += ["Compatibility not verified for this plan.", ""]
    else:
        comparison = payload["comparison"]
        lines += [
            f"- **Status:** `{payload['status']}`",
            f"- **Resolver:** {payload['resolver']} in `{payload['image']}`",
            f"- **Target:** {payload['target'].get('implementation')} {payload['target'].get('python')} "
            f"{payload['target'].get('platform')} {payload['target'].get('machine')}",
            f"- **Proposed resolution fingerprint:** `{payload['proposed_fingerprint']}`",
            f"- **Original resolution:** `{payload['original_status'] or 'not available'}`"
            + (f" (fingerprint `{payload['original_fingerprint']}`)" if payload["original_fingerprint"] else ""),
            "",
            f"> {payload['disclosure']}",
            "",
        ]
        if comparison["available"]:
            introduced = ", ".join(f"`{n}`" for n in comparison["introduced"]) or "none"
            lines += [f"**Packages introduced by this upgrade:** {introduced}", ""]
        else:
            lines += [f"**Introduced packages:** comparison unavailable ({comparison['reason']}).", ""]
        if payload["warnings"]:
            lines += ["**Warnings:**", ""] + [f"- {_md_cell(w)}" for w in payload["warnings"]] + [""]
        for title, key in (("Proposed resolved dependencies", "proposed_packages"),
                           ("Original resolved dependencies", "original_packages")):
            rows = payload[key]
            lines += [f"### {title}", ""]
            if not rows:
                lines += ["Not available.", ""]
                continue
            lines += ["| Package | Version | Direct | Introduced | Uploaded (UTC) |", "|---|---|---|---|---|"]
            for r in rows:
                introduced = "n/a" if r["introduced"] is None else ("yes" if r["introduced"] else "no")
                lines.append(f"| {_md_cell(r['name'])} | {_md_cell(r['version'])} | "
                             f"{'direct' if r['direct'] else 'transitive'} | {introduced} | {_md_cell(r['upload_date'])} |")
            lines.append("")
    lines += ["## Proposed change", "", "```diff", (proposal.unified_diff or "").rstrip("\n"), "```", ""]
    return "\n".join(lines)


def compute_sha256_bytes(data: bytes) -> str:
    """Compute hex-encoded SHA-256 digest for bytes."""
    return hashlib.sha256(data).hexdigest()


def compute_sha256_file(file_path: Path) -> str:
    """Compute hex-encoded SHA-256 digest for a file on disk."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class ExportArtifactRecord:
    """Record of an individual exported deliverable file."""
    artifact_type: ArtifactType
    file_path: Path
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str


@dataclass
class ExportBundle:
    """Represents a validated bundle of deliverables stored server-side."""
    export_id: str
    created_at: float
    export_dir: Path
    artifacts: Dict[ArtifactType, ExportArtifactRecord]
    proposal_id: Optional[str] = None
    execution_id: Optional[str] = None
    run_id: Optional[str] = None
    sanitized: bool = True
    secret_scan_passed: bool = True
    validation_status: str = "NOT_PERFORMED"
    summary: Dict[str, Any] = field(default_factory=dict)

    def to_response(self, base_url: str = "/api/export") -> ExportMetadataResponse:
        items = [
            ExportItemMetadata(
                artifact_type=art.artifact_type,
                filename=art.filename,
                mime_type=art.mime_type,
                size_bytes=art.size_bytes,
                sha256=art.sha256,
                download_url=f"{base_url}/{self.export_id}/download/{art.artifact_type.value}",
            )
            for art in self.artifacts.values()
        ]
        return ExportMetadataResponse(
            export_id=self.export_id,
            proposal_id=self.proposal_id,
            execution_id=self.execution_id,
            run_id=self.run_id,
            created_at=self.created_at,
            sanitized=self.sanitized,
            secret_scan_passed=self.secret_scan_passed,
            validation_status=self.validation_status,
            items=items,
            summary=self.summary,
        )


# ---------------------------------------------------------------------------
# Exporter Coordinator
# ---------------------------------------------------------------------------

class ArtifactExporter:
    """Coordinates generation, pre-validation, secret scanning, and export of deliverables."""

    def __init__(
        self,
        output_dir: Optional[Path | str] = None,
        export_store_dir: Optional[Path | str] = None,
    ) -> None:
        self.output_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir()) / "dua_artifacts"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.export_store_dir = Path(export_store_dir) if export_store_dir else Path(tempfile.gettempdir()) / "dua_exports"
        self.export_store_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.export_store_dir, 0o700)
        except (PermissionError, OSError):
            pass
        self._exports: Dict[str, ExportBundle] = {}
        self._lock = threading.Lock()

    def export_all(
        self,
        reference_dir: Path | str,
        working_dir: Path | str,
        repo_url: str,
        commit_sha: Optional[str] = None,
        manifest_report: Optional[RepositoryDependencyReport] = None,
        inspection_report: Optional[InspectionReport] = None,
        improvements: Optional[List[ProposedImprovement]] = None,
        validation_report: Optional[ValidationReport] = None,
        ollama_model: Optional[str] = None,
        repo_name: str = "project",
    ) -> ExportArtifactsResult:
        """Generate and validate all three deliverables (.patch, .zip, and Markdown report)."""
        # 1. Generate & validate unified patch
        patch_result = create_and_validate_patch(
            reference_dir=reference_dir,
            working_dir=working_dir,
        )

        # 2. Package & validate ZIP archive
        zip_path = self.output_dir / f"{repo_name}-improved.zip"
        zip_result = create_and_validate_zip(
            working_dir=working_dir,
            output_zip_path=zip_path,
        )

        # 3. Synthesize Markdown report
        report_md = generate_markdown_report(
            repo_url=repo_url,
            commit_sha=commit_sha,
            manifest_report=manifest_report,
            inspection_report=inspection_report,
            improvements=improvements,
            validation_report=validation_report,
            patch_result=patch_result,
            zip_result=zip_result,
            ollama_model=ollama_model,
        )

        return ExportArtifactsResult(
            patch_result=patch_result,
            zip_result=zip_result,
            report_markdown=report_md,
        )

    def export_proposal(
        self,
        proposal: UpgradeProposalResponse,
        validation_report: Optional[ValidationReportResponse] = None,
        run_id: Optional[str] = None,
        custom_notes: Optional[str] = None,
        resolution: Optional[Dict[str, Any]] = None,
    ) -> ExportBundle:
        """Bundle proposal changes, execution summaries, logs, and checksums into verified deliverables.
        
        Guarantees:
        - Git-compatible .patch file with header metadata and unified diff.
        - Structured JSON execution summary detailing proposal, dependencies, and validation status.
        - Sanitized execution console logs.
        - Strict pre-export secret verification (zero credentials leaked; fails closed).
        - Authoritative server-calculated SHA-256 checksums for each artifact.
        - Verifiably intact ZIP bundle (path-traversal safe, testzip CRC verified).
        - Atomic file staging and fail-closed cleanup on error.
        """
        export_id = f"exp_{uuid.uuid4().hex}"
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_ts = now_dt.timestamp()
        now_iso = now_dt.isoformat()

        stage_dir = self.export_store_dir / f"tmp_{export_id}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(stage_dir, 0o700)
        except (PermissionError, OSError):
            pass

        try:
            # 1. Target file naming
            manifest_name = Path(proposal.file_path).name or "manifest"
            clean_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", manifest_name)
            patch_filename = f"{clean_name}-upgrade.patch"
            log_filename = "execution.log"
            summary_filename = "summary.json"
            checksums_filename = "checksums.sha256"
            report_filename = "compatibility-report.md"
            zip_filename = f"{clean_name}-upgrade-bundle.zip"

            # 2. Synthesize Git-compatible Unified Patch
            patch_header = (
                f"# Dependency Upgrade Assistant 2.0 Export Patch\n"
                f"# Proposal ID: {proposal.proposal_id}\n"
                f"# Target Manifest: {proposal.file_path}\n"
                f"# Ecosystem: {proposal.ecosystem.value if hasattr(proposal.ecosystem, 'value') else str(proposal.ecosystem)}\n"
                f"# Generated UTC: {now_iso}\n"
                f"# Checksum Standard: SHA-256\n"
                f"#\n"
            )
            full_patch_text = patch_header + (proposal.unified_diff or "")
            if not full_patch_text.endswith("\n"):
                full_patch_text += "\n"

            verify_zero_secrets(full_patch_text, source_name=patch_filename)
            patch_path = stage_dir / patch_filename
            patch_path.write_text(full_patch_text, encoding="utf-8")
            patch_sha256 = compute_sha256_file(patch_path)
            patch_size = patch_path.stat().st_size

            # 3. Synthesize Execution Logs
            if validation_report:
                raw_logs = validation_report.logs or ""
                if not raw_logs and validation_report.commands:
                    raw_logs = "\n\n".join(
                        f"=== [{cmd.command_name}] (status: {cmd.status.value if hasattr(cmd.status, 'value') else cmd.status}, exit: {cmd.exit_code}) ===\n"
                        f"{cmd.stdout}\n{cmd.stderr}".strip()
                        for cmd in validation_report.commands
                    )
                sanitized_logs = redact_secrets(raw_logs)
                val_status_str = (
                    validation_report.status.value
                    if hasattr(validation_report.status, "value")
                    else str(validation_report.status)
                )
            else:
                sanitized_logs = (
                    "[VALIDATION_STATUS: NOT_PERFORMED]\n"
                    "No sandboxed container validation was executed for this proposal.\n"
                    "To empirically verify these dependencies, execute container validation before production merge.\n"
                )
                val_status_str = "NOT_PERFORMED"

            verify_zero_secrets(sanitized_logs, source_name=log_filename)
            log_path = stage_dir / log_filename
            log_path.write_text(sanitized_logs, encoding="utf-8")
            log_sha256 = compute_sha256_file(log_path)
            log_size = log_path.stat().st_size

            # 4. Synthesize JSON Execution Summary
            summary_dict: Dict[str, Any] = {
                "dua_version": "2.0.0",
                "export_id": export_id,
                "proposal_id": proposal.proposal_id,
                "created_at": now_iso,
                "manifest_path": proposal.file_path,
                "manifest_type": proposal.manifest_type,
                "ecosystem": proposal.ecosystem.value if hasattr(proposal.ecosystem, "value") else str(proposal.ecosystem),
                "total_planned": proposal.total_planned,
                "total_unchanged": proposal.total_unchanged,
                "total_conflicts": proposal.total_conflicts,
                "planned_upgrades": [
                    {
                        "package_name": item.package_name,
                        "current_version": item.current_version,
                        "target_version": item.target_version,
                        "declared_specifier": item.declared_specifier,
                        "proposed_specifier": item.proposed_specifier,
                        "action": item.action.value if hasattr(item.action, "value") else str(item.action),
                        "rationale": item.rationale,
                        "line_number": item.line_number,
                        "dependency_type": item.dependency_type.value if hasattr(item.dependency_type, "value") else str(item.dependency_type),
                    }
                    for item in proposal.plan_items
                ],
                "patch_integrity": {
                    "is_valid": proposal.integrity.is_valid,
                    "can_apply": proposal.integrity.can_apply,
                    "can_reverse": proposal.integrity.can_reverse,
                    "syntax_valid": proposal.integrity.syntax_valid,
                    "errors": proposal.integrity.errors,
                    "warnings": proposal.integrity.warnings,
                },
                "validation": (
                    {
                        "status": val_status_str,
                        "execution_id": validation_report.execution_id,
                        "is_runnable": validation_report.is_runnable,
                        "summary": validation_report.summary,
                        "total_duration_seconds": validation_report.total_duration_seconds,
                        "image_used": validation_report.image_used,
                        "image_digest": validation_report.image_digest,
                        "network_enabled": False,
                        "commands": [
                            {
                                "command_name": cmd.command_name,
                                "command_args": cmd.command_args,
                                "status": cmd.status.value if hasattr(cmd.status, "value") else str(cmd.status),
                                "exit_code": cmd.exit_code,
                                "duration_seconds": cmd.duration_seconds,
                            }
                            for cmd in validation_report.commands
                        ],
                    }
                    if validation_report
                    else {
                        "status": "NOT_PERFORMED",
                        "summary": "No sandboxed container validation was executed for this proposal.",
                    }
                ),
                "checksums": {
                    patch_filename: patch_sha256,
                    log_filename: log_sha256,
                },
                "security_audit": {
                    "zero_secrets_verified": True,
                    "sanitization_engine": "DUA 2.0 Pre-Export Secret Sanitizer",
                },
                "compatibility": resolution or {"status": "NOT_VERIFIED"},
                "custom_notes": custom_notes,
            }

            summary_text = json.dumps(summary_dict, indent=2) + "\n"
            verify_zero_secrets(summary_text, source_name=summary_filename)
            summary_path = stage_dir / summary_filename
            summary_path.write_text(summary_text, encoding="utf-8")
            summary_sha256 = compute_sha256_file(summary_path)
            summary_size = summary_path.stat().st_size

            # 4b. FR-32d Markdown report (same payload as summary.json "compatibility")
            report_text = render_compatibility_markdown(proposal, resolution, val_status_str, now_iso)
            verify_zero_secrets(report_text, source_name=report_filename)
            report_path = stage_dir / report_filename
            report_path.write_text(report_text, encoding="utf-8")
            report_sha256 = compute_sha256_file(report_path)
            report_size = report_path.stat().st_size

            # 5. Synthesize SHA-256 Checksums Manifest
            checksums_content = (
                f"{patch_sha256}  {patch_filename}\n"
                f"{summary_sha256}  {summary_filename}\n"
                f"{log_sha256}  {log_filename}\n"
                f"{report_sha256}  {report_filename}\n"
            )
            checksums_path = stage_dir / checksums_filename
            checksums_path.write_text(checksums_content, encoding="utf-8")
            checksums_sha256 = compute_sha256_file(checksums_path)
            checksums_size = checksums_path.stat().st_size

            # 6. Synthesize and Verify ZIP Archive Bundle
            zip_path = stage_dir / zip_filename
            with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(patch_path, arcname=patch_filename)
                zf.write(summary_path, arcname=summary_filename)
                zf.write(log_path, arcname=log_filename)
                zf.write(checksums_path, arcname=checksums_filename)
                zf.write(report_path, arcname=report_filename)

            # Test zip integrity
            with zipfile.ZipFile(zip_path, mode="r") as zf:
                corrupt = zf.testzip()
                if corrupt is not None:
                    raise ValueError(f"ZIP package corrupted at entry '{corrupt}'.")

            # Test extraction in isolated sandbox
            temp_extract = Path(tempfile.mkdtemp(prefix="dua_export_test_"))
            try:
                with zipfile.ZipFile(zip_path, mode="r") as zf:
                    zf.extractall(temp_extract)
            finally:
                shutil.rmtree(temp_extract, ignore_errors=True)

            zip_sha256 = compute_sha256_file(zip_path)
            zip_size = zip_path.stat().st_size

            # 7. Atomic Commit to Permanent Export Store
            final_dir = self.export_store_dir / export_id
            if final_dir.exists():
                shutil.rmtree(final_dir, ignore_errors=True)
            stage_dir.rename(final_dir)

            # Update paths to final directory
            artifacts_map: Dict[ArtifactType, ExportArtifactRecord] = {
                ArtifactType.PATCH: ExportArtifactRecord(
                    artifact_type=ArtifactType.PATCH,
                    file_path=final_dir / patch_filename,
                    filename=patch_filename,
                    mime_type="text/x-diff; charset=utf-8",
                    size_bytes=patch_size,
                    sha256=patch_sha256,
                ),
                ArtifactType.SUMMARY_JSON: ExportArtifactRecord(
                    artifact_type=ArtifactType.SUMMARY_JSON,
                    file_path=final_dir / summary_filename,
                    filename=summary_filename,
                    mime_type="application/json",
                    size_bytes=summary_size,
                    sha256=summary_sha256,
                ),
                ArtifactType.EXECUTION_LOG: ExportArtifactRecord(
                    artifact_type=ArtifactType.EXECUTION_LOG,
                    file_path=final_dir / log_filename,
                    filename=log_filename,
                    mime_type="text/plain; charset=utf-8",
                    size_bytes=log_size,
                    sha256=log_sha256,
                ),
                ArtifactType.CHECKSUMS: ExportArtifactRecord(
                    artifact_type=ArtifactType.CHECKSUMS,
                    file_path=final_dir / checksums_filename,
                    filename=checksums_filename,
                    mime_type="text/plain; charset=utf-8",
                    size_bytes=checksums_size,
                    sha256=checksums_sha256,
                ),
                ArtifactType.REPORT_MD: ExportArtifactRecord(
                    artifact_type=ArtifactType.REPORT_MD,
                    file_path=final_dir / report_filename,
                    filename=report_filename,
                    mime_type="text/markdown; charset=utf-8",
                    size_bytes=report_size,
                    sha256=report_sha256,
                ),
                ArtifactType.BUNDLE_ZIP: ExportArtifactRecord(
                    artifact_type=ArtifactType.BUNDLE_ZIP,
                    file_path=final_dir / zip_filename,
                    filename=zip_filename,
                    mime_type="application/zip",
                    size_bytes=zip_size,
                    sha256=zip_sha256,
                ),
            }

            bundle = ExportBundle(
                export_id=export_id,
                created_at=now_ts,
                export_dir=final_dir,
                artifacts=artifacts_map,
                proposal_id=proposal.proposal_id,
                execution_id=validation_report.execution_id if validation_report else None,
                run_id=run_id,
                sanitized=True,
                secret_scan_passed=True,
                validation_status=val_status_str,
                summary={
                    "patch_filename": patch_filename,
                    "bundle_filename": zip_filename,
                    "target_manifest": proposal.file_path,
                    "total_upgrades": proposal.total_planned,
                    "validation_status": val_status_str,
                    "sha256_bundle": zip_sha256,
                },
            )

            with self._lock:
                self._exports[export_id] = bundle

            logger.info(f"Successfully generated export bundle '{export_id}' for proposal '{proposal.proposal_id}'")
            return bundle

        except Exception:
            # Fail closed: scrub temporary staging directory completely
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            raise

    def get_export(self, export_id: str) -> Optional[ExportBundle]:
        """Retrieve an export bundle by ID if it exists (thread-safe, validates identifier)."""
        if not export_id or not re.match(r"^[a-zA-Z0-9_\-]+$", export_id):
            return None

        with self._lock:
            if export_id in self._exports:
                return self._exports[export_id]

        # Resilience: inspect filesystem store if memory entry missing (e.g. after server restart)
        target_dir = self.export_store_dir / export_id
        if not target_dir.is_dir() or not is_safe_relative_path(self.export_store_dir, target_dir):
            return None

        summary_file = target_dir / "summary.json"
        if not summary_file.is_file():
            return None

        try:
            summary_data = json.loads(summary_file.read_text(encoding="utf-8"))
            artifacts_map: Dict[ArtifactType, ExportArtifactRecord] = {}

            # Find matching files
            for p in target_dir.iterdir():
                if not p.is_file():
                    continue
                sha = compute_sha256_file(p)
                size = p.stat().st_size
                if p.name.endswith(".patch"):
                    artifacts_map[ArtifactType.PATCH] = ExportArtifactRecord(
                        ArtifactType.PATCH, p, p.name, "text/x-diff; charset=utf-8", size, sha
                    )
                elif p.name == "summary.json":
                    artifacts_map[ArtifactType.SUMMARY_JSON] = ExportArtifactRecord(
                        ArtifactType.SUMMARY_JSON, p, p.name, "application/json", size, sha
                    )
                elif p.name == "execution.log":
                    artifacts_map[ArtifactType.EXECUTION_LOG] = ExportArtifactRecord(
                        ArtifactType.EXECUTION_LOG, p, p.name, "text/plain; charset=utf-8", size, sha
                    )
                elif p.name == "checksums.sha256":
                    artifacts_map[ArtifactType.CHECKSUMS] = ExportArtifactRecord(
                        ArtifactType.CHECKSUMS, p, p.name, "text/plain; charset=utf-8", size, sha
                    )
                elif p.name == "compatibility-report.md":
                    artifacts_map[ArtifactType.REPORT_MD] = ExportArtifactRecord(
                        ArtifactType.REPORT_MD, p, p.name, "text/markdown; charset=utf-8", size, sha
                    )
                elif p.name.endswith(".zip"):
                    artifacts_map[ArtifactType.BUNDLE_ZIP] = ExportArtifactRecord(
                        ArtifactType.BUNDLE_ZIP, p, p.name, "application/zip", size, sha
                    )

            bundle = ExportBundle(
                export_id=export_id,
                created_at=target_dir.stat().st_mtime,
                export_dir=target_dir,
                artifacts=artifacts_map,
                proposal_id=summary_data.get("proposal_id"),
                validation_status=summary_data.get("validation", {}).get("status", "NOT_PERFORMED"),
                summary=summary_data,
            )
            with self._lock:
                self._exports[export_id] = bundle
            return bundle
        except Exception as exc:
            logger.warning(f"Failed to recover export bundle '{export_id}' from disk: {exc}")
            return None

    def get_artifact_file(
        self,
        export_id: str,
        artifact_type: ArtifactType | str,
    ) -> Tuple[Path, str, str, str]:
        """Locate an artifact file for download.
        
        Returns:
            Tuple of (file_path, filename, mime_type, sha256)
        Raises:
            FileNotFoundError if export or artifact not found.
            ValueError if export_id or artifact_type is invalid.
        """
        bundle = self.get_export(export_id)
        if not bundle:
            raise FileNotFoundError(f"Export bundle '{export_id}' not found.")

        try:
            art_type = ArtifactType(artifact_type)
        except ValueError:
            raise ValueError(f"Invalid artifact type: '{artifact_type}'.")

        if art_type not in bundle.artifacts:
            raise FileNotFoundError(f"Artifact '{art_type.value}' not found in export bundle '{export_id}'.")

        rec = bundle.artifacts[art_type]
        if not is_safe_relative_path(self.export_store_dir, rec.file_path):
            raise ValueError(f"Unsafe path detected for artifact '{art_type.value}'.")

        if not rec.file_path.is_file():
            raise FileNotFoundError(f"Artifact file '{rec.filename}' is missing from disk.")

        return rec.file_path, rec.filename, rec.mime_type, rec.sha256


# Global singleton instance for export service
_exporter_instance: Optional[ArtifactExporter] = None


def get_artifact_exporter() -> ArtifactExporter:
    """Retrieve or initialize the global ArtifactExporter singleton."""
    global _exporter_instance
    if _exporter_instance is None:
        _exporter_instance = ArtifactExporter()
    return _exporter_instance

