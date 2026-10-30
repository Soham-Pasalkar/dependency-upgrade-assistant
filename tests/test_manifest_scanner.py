"""Unit tests for recursive manifest scanner and nested reference resolution."""
from pathlib import Path
import pytest

from src.analysis.manifest_scanner import (
    ManifestScanResult,
    ManifestType,
    is_requirements_filename,
    is_safe_relative_path,
    scan_manifests,
)


def test_is_requirements_filename():
    """Verify filename pattern recognition for requirements files."""
    assert is_requirements_filename("requirements.txt") is True
    assert is_requirements_filename("requirements-dev.txt") is True
    assert is_requirements_filename("requirements_prod.txt") is True
    assert is_requirements_filename("test-requirements.txt") is True
    assert is_requirements_filename("random.txt") is False
    assert is_requirements_filename("pyproject.toml") is False


def test_is_safe_relative_path(tmp_path: Path):
    """Verify safety check against directory traversal."""
    root = tmp_path / "repo"
    root.mkdir()
    inside = root / "requirements.txt"
    outside = tmp_path / "outside.txt"

    assert is_safe_relative_path(root, inside) is True
    assert is_safe_relative_path(root, outside) is False


def test_scan_multiple_manifests(tmp_path: Path):
    """Verify discovery of pyproject.toml and multiple requirements*.txt files."""
    repo = tmp_path / "sample_project"
    repo.mkdir()

    # Create root manifests
    (repo / "pyproject.toml").write_text("[project]\nname = 'test'", encoding="utf-8")
    (repo / "requirements.txt").write_text("requests==2.28.0\n", encoding="utf-8")
    (repo / "requirements-dev.txt").write_text("pytest>=7.0.0\n", encoding="utf-8")

    # Create subfolder manifest
    sub = repo / "deploy"
    sub.mkdir()
    (sub / "requirements-prod.txt").write_text("gunicorn==20.1.0\n", encoding="utf-8")

    # Create excluded directories that should be skipped
    venv_dir = repo / ".venv"
    venv_dir.mkdir()
    (venv_dir / "requirements.txt").write_text("ignored==1.0\n", encoding="utf-8")

    cache_dir = repo / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "requirements.txt").write_text("ignored==2.0\n", encoding="utf-8")

    result = scan_manifests(repo)

    assert result.has_manifests is True
    rel_paths = result.relative_paths
    assert "pyproject.toml" in rel_paths
    assert "requirements.txt" in rel_paths
    assert "requirements-dev.txt" in rel_paths
    assert "deploy/requirements-prod.txt" in rel_paths

    # Excluded directories must not appear
    assert not any(".venv" in p for p in rel_paths)
    assert not any("__pycache__" in p for p in rel_paths)
    assert len(result.manifests) == 4


def test_scan_nested_and_constraint_references(tmp_path: Path):
    """Verify resolution of -r and -c nested requirements."""
    repo = tmp_path / "nested_repo"
    repo.mkdir()

    req_dir = repo / "requirements"
    req_dir.mkdir()

    (req_dir / "base.txt").write_text("urllib3==1.26.15\n", encoding="utf-8")
    (req_dir / "constraints.txt").write_text("certifi>=2024.1.1\n", encoding="utf-8")

    # Main requirements references base.txt and constraints.txt
    (repo / "requirements.txt").write_text(
        "-r requirements/base.txt\n"
        "-c requirements/constraints.txt\n"
        "requests==2.28.0\n",
        encoding="utf-8",
    )

    result = scan_manifests(repo)

    rel_paths = result.relative_paths
    assert "requirements.txt" in rel_paths
    assert "requirements/base.txt" in rel_paths
    assert "requirements/constraints.txt" in rel_paths

    assert "requirements.txt" in result.nested_references
    links = result.nested_references["requirements.txt"]
    assert "requirements/base.txt" in links
    assert "requirements/constraints.txt" in links


def test_circular_reference_detection(tmp_path: Path):
    """Verify detection of circular -r references without infinite loop."""
    repo = tmp_path / "circular_repo"
    repo.mkdir()

    (repo / "requirements-a.txt").write_text("-r requirements-b.txt\nrequests==2.28.0\n", encoding="utf-8")
    (repo / "requirements-b.txt").write_text("-r requirements-a.txt\nurllib3==1.26.15\n", encoding="utf-8")

    result = scan_manifests(repo)

    assert len(result.circular_references) > 0
    assert any("Circular reference detected" in w for w in result.warnings)


def test_unsafe_path_traversal_reference_blocked(tmp_path: Path):
    """Verify that referencing files outside repo root via -r is blocked."""
    repo = tmp_path / "traversal_repo"
    repo.mkdir()

    (repo / "requirements.txt").write_text("-r ../outside.txt\n", encoding="utf-8")

    result = scan_manifests(repo)
    assert any("Blocked unsafe path traversal" in w for w in result.warnings)


def test_empty_repository_scan(tmp_path: Path):
    """Verify scan on empty repository returns empty result without errors."""
    empty_repo = tmp_path / "empty_repo"
    empty_repo.mkdir()

    result = scan_manifests(empty_repo)
    assert result.has_manifests is False
    assert result.manifests == []
    assert result.warnings == []
