"""Unit tests for deterministic dependency analyzer, PyPI metadata client, and AI risk assessment."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.analysis.dependency_analyzer import (
    AIRiskAssessment,
    AnalyzedDependency,
    DependencyAnalyzer,
    ManifestDependencyAnalysis,
    PyPIMetadataClient,
    RepositoryDependencyReport,
    assess_upgrade_risk_with_ollama,
)
from src.analysis.manifest_scanner import DiscoveredManifest, ManifestType
from src.core.models import LineType, RequirementLine
from src.services.ollama_client import OllamaClient, OllamaConnectionError


# ---------------------------------------------------------------------------
# PyPIMetadataClient Tests
# ---------------------------------------------------------------------------

def test_pypi_client_finds_latest_candidate():
    """Verify PyPI metadata parsing, prerelease filtering, and candidate selection."""
    mock_session = MagicMock()
    sample_pypi_data = {
        "info": {"version": "2.32.3", "summary": "Python HTTP for Humans."},
        "releases": {
            "2.28.0": [{"yanked": False, "requires_python": ">=3.7"}],
            "2.31.0": [{"yanked": False, "requires_python": ">=3.8"}],
            "2.32.3": [{"yanked": False, "requires_python": ">=3.8"}],
            "3.0.0b1": [{"yanked": False, "requires_python": ">=3.10"}],  # Prerelease: should be ignored
            "2.99.0": [{"yanked": True, "requires_python": ">=3.8"}],     # Yanked: should be ignored
            "4.0.0": [{"yanked": False, "requires_python": ">=3.16"}],    # Incompatible Python: ignored
        },
    }
    mock_session.get.return_value = MagicMock(status_code=200, json=lambda: sample_pypi_data)

    client = PyPIMetadataClient(session=mock_session)
    candidate, prov, py_compat = client.find_candidate_version(
        package_name="requests",
        current_version_str="2.28.0",
        target_python="3.14.3",
    )

    assert candidate == "2.32.3"
    assert prov == "PyPI JSON API"
    assert py_compat is True

    # Test in-memory cache hit (should not call session.get a second time)
    client.find_candidate_version("requests", "2.28.0")
    assert mock_session.get.call_count == 1


def test_pypi_client_fallback_to_local_catalog_on_404():
    """Verify fallback to local catalog when package is missing from PyPI (e.g. private or test package)."""
    mock_session = MagicMock()
    mock_session.get.return_value = MagicMock(status_code=404)

    client = PyPIMetadataClient(session=mock_session)
    candidate, prov, py_compat = client.find_candidate_version(
        package_name="urllib3",  # Present in local catalog
        current_version_str="1.26.15",
    )

    assert candidate == "2.2.3"
    assert "Local Catalog" in prov
    assert py_compat is True


def test_pypi_client_fallback_on_network_error():
    """Verify graceful fallback to local catalog on network failure or timeout."""
    mock_session = MagicMock()
    mock_session.get.side_effect = requests.exceptions.ConnectionError("Offline")

    client = PyPIMetadataClient(session=mock_session)
    candidate, prov, _ = client.find_candidate_version(
        package_name="pytest",
        current_version_str="7.4.0",
    )

    # Local catalog has pytest candidate 8.3.3
    assert candidate == "8.3.3"
    assert "Local Catalog" in prov


# ---------------------------------------------------------------------------
# AI Risk Assessment Tests
# ---------------------------------------------------------------------------

def test_assess_upgrade_risk_with_ollama_success():
    """Verify structured advisory risk assessment parsing from Ollama."""
    mock_ollama = MagicMock(spec=OllamaClient)
    expected_risk = {
        "risk_level": "LOW",
        "breaking_changes_summary": "No breaking API changes; bugfixes and performance improvements only.",
        "deprecated_features": ["Legacy adapter flag deprecated in favor of new session parameters"],
        "migration_notes": "Run existing integration tests; no code changes expected.",
        "confidence": "HIGH",
    }
    mock_ollama.chat_json.return_value = (expected_risk, MagicMock())

    assessment = assess_upgrade_risk_with_ollama(
        client=mock_ollama,
        model="llama3.1:8b",
        package_name="requests",
        current_version="2.28.0",
        candidate_version="2.32.3",
        package_summary="HTTP library",
    )

    assert assessment.risk_level == "LOW"
    assert assessment.has_error is False
    assert assessment.is_advisory is True
    assert "No breaking API changes" in assessment.breaking_changes_summary
    assert len(assessment.deprecated_features) == 1
    assert assessment.model_used == "llama3.1:8b"


def test_assess_upgrade_risk_with_ollama_error_handling():
    """Verify graceful degradation when Ollama is unreachable or raises error."""
    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.chat_json.side_effect = OllamaConnectionError("Daemon offline")

    assessment = assess_upgrade_risk_with_ollama(
        client=mock_ollama,
        model="llama3.1:8b",
        package_name="requests",
        current_version="2.28.0",
        candidate_version="2.32.3",
    )

    assert assessment.has_error is True
    assert assessment.risk_level == "UNKNOWN"
    assert "Ollama error" in str(assessment.error_message)


# ---------------------------------------------------------------------------
# DependencyAnalyzer Tests
# ---------------------------------------------------------------------------

def test_analyze_requirement_line_exact_pin():
    """Verify tier analysis for an outdated exact pin."""
    mock_meta = MagicMock(spec=PyPIMetadataClient)
    mock_meta.find_candidate_version.return_value = ("2.32.3", "PyPI JSON API", True)

    analyzer = DependencyAnalyzer(metadata_client=mock_meta)
    req_line = RequirementLine(
        line_number=1,
        raw_text="requests==2.28.0\n",
        line_type=LineType.EXACT_PIN,
        package_name="requests",
        operator="==",
        version="2.28.0",
    )

    dep = analyzer.analyze_requirement_line(req_line, "requirements.txt")

    assert dep.package_name == "requests"
    assert dep.current_version == "2.28.0"
    assert dep.candidate_version == "2.32.3"
    assert dep.has_candidate_upgrade is True
    assert dep.satisfies_current_constraint is False  # Candidate 2.32.3 does not satisfy ==2.28.0
    assert dep.requires_python_compatibility is True
    assert dep.is_exact_pin is True


def test_analyze_manifest_requirements_txt(tmp_path: Path):
    """Verify manifest analysis on a requirements.txt file."""
    f = tmp_path / "requirements.txt"
    f.write_text(
        "requests==2.28.0\n"
        "pytest==8.3.3\n"
        "# Comment line\n"
        "flask>=3.0.0\n",
        encoding="utf-8",
    )

    manifest = DiscoveredManifest(
        path=f,
        relative_path="requirements.txt",
        manifest_type=ManifestType.REQUIREMENTS_TXT,
    )

    # Use local catalog fallback metadata client
    analyzer = DependencyAnalyzer()
    res = analyzer.analyze_manifest(manifest)

    assert isinstance(res, ManifestDependencyAnalysis)
    assert len(res.dependencies) == 3  # requests, pytest, flask
    assert res.outdated_count >= 1     # requests is outdated (catalog candidate 2.32.3)


def test_analyze_repository_detects_conflicts(tmp_path: Path):
    """Verify multi-manifest aggregation and cross-manifest conflict detection."""
    req1 = tmp_path / "requirements.txt"
    req1.write_text("urllib3==1.26.15\nrequests==2.28.0\n", encoding="utf-8")

    req2 = tmp_path / "requirements-dev.txt"
    req2.write_text("urllib3>=2.0.0\npytest==8.3.3\n", encoding="utf-8")

    m1 = DiscoveredManifest(path=req1, relative_path="requirements.txt", manifest_type=ManifestType.REQUIREMENTS_TXT)
    m2 = DiscoveredManifest(path=req2, relative_path="requirements-dev.txt", manifest_type=ManifestType.REQUIREMENTS_TXT)

    analyzer = DependencyAnalyzer()
    report = analyzer.analyze_repository([m1, m2])

    assert isinstance(report, RepositoryDependencyReport)
    assert report.total_dependencies == 4
    assert report.has_conflicts is True
    assert "urllib3" in report.conflicts
    conflict_entries = report.conflicts["urllib3"]
    assert len(conflict_entries) == 2
    assert any("requirements.txt" in c[0] and "==" in c[1] for c in conflict_entries)
    assert any("requirements-dev.txt" in c[0] and ">=" in c[1] for c in conflict_entries)

