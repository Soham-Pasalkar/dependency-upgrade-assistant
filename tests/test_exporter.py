"""Unit tests for ArtifactExporter, patch pre-validation, ZIP archiving, and report generation."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.analysis.code_improver import ImprovementStatus, ProposedImprovement
from src.analysis.code_inspector import CodeFinding, ExcludedFile, InspectionReport, IssueCategory, IssueSeverity, PrioritizedFile
from src.analysis.dependency_analyzer import AnalyzedDependency, ManifestDependencyAnalysis, RepositoryDependencyReport
from src.analysis.manifest_scanner import DiscoveredManifest, ManifestType
from src.services.exporter import (
    ArtifactExporter,
    PatchValidationResult,
    ZipValidationResult,
    create_and_validate_patch,
    create_and_validate_zip,
    generate_markdown_report,
    generate_unified_patch,
    is_binary_file,
    validate_patch_against_reference,
)
from src.validation.test_runner import ValidationCommandResult, ValidationStatus
from src.validation.validator import ValidationReport


def test_is_binary_file(tmp_path: Path):
    """Verify binary detection on null bytes and non-UTF8 data."""
    text_file = tmp_path / "hello.txt"
    text_file.write_text("Hello World\n", encoding="utf-8")
    assert is_binary_file(text_file) is False

    bin_file = tmp_path / "binary.dat"
    bin_file.write_bytes(b"\x00\x01\x02\x03\xff")
    assert is_binary_file(bin_file) is True


def test_generate_unified_patch_modified_added_deleted(tmp_path: Path):
    """Verify patch generation handles modified, added, and deleted files with Git headers."""
    ref_dir = tmp_path / "reference"
    work_dir = tmp_path / "working"
    ref_dir.mkdir()
    work_dir.mkdir()

    # 1. Modified file
    (ref_dir / "app.py").write_text("version = '1.0'\n", encoding="utf-8")
    (work_dir / "app.py").write_text("version = '2.0'\n", encoding="utf-8")

    # 2. Added file
    (work_dir / "new_module.py").write_text("def helper(): pass\n", encoding="utf-8")

    # 3. Deleted file
    (ref_dir / "obsolete.py").write_text("deprecated = True\n", encoding="utf-8")

    patch_text, modified, added, deleted, binary = generate_unified_patch(ref_dir, work_dir)

    assert "app.py" in modified
    assert "new_module.py" in added
    assert "obsolete.py" in deleted
    assert binary == []

    # Check Git patch headers
    assert "diff --git a/app.py b/app.py" in patch_text
    assert "-version = '1.0'" in patch_text
    assert "+version = '2.0'" in patch_text

    assert "diff --git a/new_module.py b/new_module.py" in patch_text
    assert "new file mode 100644" in patch_text
    assert "+def helper(): pass" in patch_text

    assert "diff --git a/obsolete.py b/obsolete.py" in patch_text
    assert "deleted file mode 100644" in patch_text
    assert "-deprecated = True" in patch_text


def test_binary_change_detection_reported_explicitly(tmp_path: Path):
    """Verify binary changes are detected and reported rather than corrupting the text patch."""
    ref_dir = tmp_path / "reference"
    work_dir = tmp_path / "working"
    ref_dir.mkdir()
    work_dir.mkdir()

    (ref_dir / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00olddata")
    (work_dir / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00newdata")

    patch_text, modified, added, deleted, binary = generate_unified_patch(ref_dir, work_dir)

    assert "logo.png" in binary
    assert "logo.png" not in modified
    # Binary content should not be dumped into text patch
    assert "newdata" not in patch_text


def test_validate_patch_against_reference_clean_run(tmp_path: Path):
    """Verify git apply --check succeeds against clean reference repo."""
    ref_dir = tmp_path / "reference"
    work_dir = tmp_path / "working"
    ref_dir.mkdir()
    work_dir.mkdir()

    (ref_dir / "math.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    (work_dir / "math.py").write_text("def add(a, b): return a + b + 0\n", encoding="utf-8")

    result = create_and_validate_patch(reference_dir=ref_dir, working_dir=work_dir)

    assert result.is_valid is True
    assert result.has_changes is True
    assert "1 modified" in result.change_summary
    assert result.error_message is None


def test_validate_patch_rejection_on_corrupt_patch(tmp_path: Path):
    """Verify patch validation fails when patch cannot apply cleanly."""
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    (ref_dir / "sample.txt").write_text("Original content\n", encoding="utf-8")

    corrupt_patch = (
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-Completely mismatched lines that do not exist\n"
        "+Replacement\n"
    )

    is_valid, error = validate_patch_against_reference(corrupt_patch, ref_dir)
    assert is_valid is False
    assert error is not None
    assert "git apply --check failed" in error


def test_create_and_validate_zip_exclusions_and_integrity(tmp_path: Path):
    """Verify ZIP packaging excludes .git, .venv, caches, and secrets, and passes testzip()."""
    work_dir = tmp_path / "working"
    work_dir.mkdir()

    # Valid files
    src = work_dir / "src"
    src.mkdir()
    (src / "app.py").write_text("print('app')", encoding="utf-8")
    (work_dir / "README.md").write_text("# Project", encoding="utf-8")

    # Files that MUST be excluded
    venv_dir = work_dir / ".venv" / "bin"
    venv_dir.mkdir(parents=True)
    (venv_dir / "python").write_text("binary", encoding="utf-8")

    cache_dir = work_dir / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "app.cpython-314.pyc").write_bytes(b"\x00\x01\x02")

    git_dir = work_dir / ".git" / "objects"
    git_dir.mkdir(parents=True)
    (git_dir / "hash").write_text("gitdata", encoding="utf-8")

    # Secret files that MUST be excluded
    (work_dir / ".env").write_text("SECRET_KEY=12345", encoding="utf-8")
    (work_dir / "private.key").write_text("PRIVATE_KEY_DATA", encoding="utf-8")

    out_zip = tmp_path / "output.zip"
    zip_res = create_and_validate_zip(work_dir, out_zip)

    assert zip_res.is_valid is True
    assert out_zip.exists()
    assert zip_res.file_count == 2  # Only src/app.py and README.md

    # Check exclusions reported
    excluded_names = " ".join(zip_res.excluded_files)
    assert ".venv" in excluded_names
    assert "__pycache__" in excluded_names
    assert ".env" in excluded_names
    assert "private.key" in excluded_names


def test_generate_markdown_report_comprehensive(tmp_path: Path):
    """Verify synthesis of structured Markdown analysis report."""
    manifest = DiscoveredManifest(Path("req.txt"), "requirements.txt", ManifestType.REQUIREMENTS_TXT)
    dep = AnalyzedDependency(
        package_name="requests",
        source_file="requirements.txt",
        raw_declaration="requests==2.28.0",
        current_specifier="==",
        current_version="2.28.0",
        candidate_version="2.32.3",
        has_candidate_upgrade=True,
        satisfies_current_constraint=False,
        requires_python_compatibility=True,
        status_message="Upgrade available",
        provenance="PyPI JSON API",
        is_exact_pin=True,
    )
    m_analysis = ManifestDependencyAnalysis(manifest, [dep], outdated_count=1)
    dep_report = RepositoryDependencyReport([m_analysis], total_dependencies=1, total_outdated=1)

    finding = CodeFinding("BUG-01", "app.py", "10", IssueCategory.BUG, IssueSeverity.HIGH, "Bug desc", "re", "sug", "HIGH")
    insp_report = InspectionReport(
        inspected_files=[PrioritizedFile(Path("app.py"), "app.py", 100, 50, 10, ["main"])],
        excluded_files=[],
        findings=[finding],
        total_files_discovered=1,
    )

    imp = ProposedImprovement("BUG-01", "app.py", "old", "new", "fixed bug", ImprovementStatus.APPLIED, "diff")

    val_cmd = ValidationCommandResult("pytest", ["pytest", "-v"], ValidationStatus.PASSED, 0, "ok", "", 1.5, "Tests")
    val_report = ValidationReport(
        overall_status=ValidationStatus.PASSED,
        summary_message="All passed",
        command_results=[val_cmd],
        total_checks=1,
        passed_count=1,
    )

    patch_res = PatchValidationResult(
        is_valid=True,
        patch_content="diff",
        modified_files=["app.py"],
        added_files=[],
        deleted_files=[],
        binary_files_detected=[],
    )

    zip_res = ZipValidationResult(
        is_valid=True,
        zip_path=Path("proj.zip"),
        file_count=5,
        archive_size_bytes=1024,
        excluded_files=[],
    )

    report_md = generate_markdown_report(
        repo_url="https://github.com/owner/repo",
        commit_sha="abcdef123456",
        manifest_report=dep_report,
        inspection_report=insp_report,
        improvements=[imp],
        validation_report=val_report,
        patch_result=patch_res,
        zip_result=zip_res,
        ollama_model="llama3.1:8b",
    )

    assert "# Repository Analysis & Improvement Report" in report_md
    assert "https://github.com/owner/repo" in report_md
    assert "requests" in report_md
    assert "2.32.3" in report_md
    assert "BUG-01" in report_md
    assert "PASSED" in report_md
    assert "llama3.1:8b" in report_md
    assert "Unified Patch" in report_md
    assert "Verified valid via `git apply --check`" in report_md


def test_artifact_exporter_coordinator(tmp_path: Path):
    """Verify ArtifactExporter creates patch, zip, and report deliverables."""
    ref_dir = tmp_path / "reference"
    work_dir = tmp_path / "working"
    out_dir = tmp_path / "artifacts"
    ref_dir.mkdir()
    work_dir.mkdir()

    (ref_dir / "main.py").write_text("x = 1\n", encoding="utf-8")
    (work_dir / "main.py").write_text("x = 2\n", encoding="utf-8")

    exporter = ArtifactExporter(output_dir=out_dir)
    res = exporter.export_all(
        reference_dir=ref_dir,
        working_dir=work_dir,
        repo_url="https://github.com/org/repo",
        repo_name="my_test_app",
    )

    assert res.patch_result.is_valid is True
    assert res.zip_result.is_valid is True
    assert (out_dir / "my_test_app-improved.zip").exists()
    assert len(res.report_markdown) > 0
    assert "my_test_app" in str(res.zip_result.zip_path)
