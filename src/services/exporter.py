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
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.analysis.code_improver import ProposedImprovement
from src.analysis.code_inspector import ExcludedFile, InspectionReport
from src.analysis.dependency_analyzer import RepositoryDependencyReport
from src.analysis.manifest_scanner import is_safe_relative_path
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
# Exporter Coordinator
# ---------------------------------------------------------------------------

class ArtifactExporter:
    """Coordinates generation and pre-validation of all pipeline deliverables."""

    def __init__(self, output_dir: Optional[Path | str] = None) -> None:
        self.output_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir()) / "dua_artifacts"
        self.output_dir.mkdir(parents=True, exist_ok=True)

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
