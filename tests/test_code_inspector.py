"""Unit tests for CodeInspector, file prioritization, and AST/AI inspection."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.analysis.code_inspector import (
    CodeFinding,
    CodeInspector,
    IssueCategory,
    IssueSeverity,
    calculate_file_priority,
    contains_likely_secrets,
    discover_and_prioritize_files,
    scan_file_statically,
    scan_file_with_ollama,
)
from src.services.ollama_client import OllamaClient, OllamaConnectionError


def test_contains_likely_secrets():
    """Verify detection of private keys and hardcoded API tokens."""
    clean_code = "def add(a, b):\n    return a + b\n"
    assert contains_likely_secrets(clean_code) is False

    key_code = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...\n-----END RSA PRIVATE KEY-----"
    assert contains_likely_secrets(key_code) is True

    secret_code = "api_key = 'abcdef1234567890abcdef1234567890'"
    assert contains_likely_secrets(secret_code) is True

    ghp_code = "token = 'ghp_123456789012345678901234567890123456'"
    assert contains_likely_secrets(ghp_code) is True


def test_calculate_file_priority():
    """Verify priority score calculations for entrypoints and candidate imports."""
    # Entry point
    score_app, reasons_app = calculate_file_priority("src/app.py", "import os\n")
    assert score_app > 50
    assert "Application entrypoint" in reasons_app

    # Dependency boosted file
    score_dep, reasons_dep = calculate_file_priority(
        "src/client.py",
        "import requests\nresponse = requests.get('url')\n",
        upgraded_packages={"requests"},
    )
    assert any("Imports candidate dependency" in r for r in reasons_dep)
    assert score_dep >= 50

    # Test file should receive lower score
    score_test, reasons_test = calculate_file_priority("tests/test_app.py", "def test_ok(): pass\n")
    assert any("Test suite file" in r for r in reasons_test)
    assert score_test < score_app


def test_discover_and_prioritize_files_limit_and_exclusions(tmp_path: Path):
    """Verify exclusion of .git, .venv, secrets, large files, and configurable limit."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Create 3 regular files
    (repo / "app.py").write_text("print('main')", encoding="utf-8")
    (repo / "helper1.py").write_text("def h1(): pass", encoding="utf-8")
    (repo / "helper2.py").write_text("def h2(): pass", encoding="utf-8")

    # Create excluded files
    venv_dir = repo / ".venv"
    venv_dir.mkdir()
    (venv_dir / "lib.py").write_text("ignored", encoding="utf-8")

    secret_file = repo / "config.py"
    secret_file.write_text("api_key = 'secret12345678901234567890'", encoding="utf-8")

    large_file = repo / "large_data.py"
    large_file.write_text("x = 1\n" * 60000, encoding="utf-8")  # ~360 KB

    # Test with inspection limit = 2
    inspected, excluded, total = discover_and_prioritize_files(
        repo_root=repo,
        limit=2,
        max_file_bytes=100 * 1024,
    )

    assert len(inspected) == 2
    inspected_names = [f.relative_path for f in inspected]
    assert "app.py" in inspected_names  # High priority entrypoint

    excluded_reasons = {e.relative_path: e.reason for e in excluded}
    assert any("config.py" in p and "secrets" in r for p, r in excluded_reasons.items())
    assert any("large_data.py" in p and "exceeds maximum" in r for p, r in excluded_reasons.items())
    assert any("Exceeded inspection limit" in r for r in excluded_reasons.values())


def test_scan_file_statically_detects_security_and_smells(tmp_path: Path):
    """Verify static AST detector catches eval, shell=True, and bare except."""
    code = (
        "import subprocess\n"
        "def bad_func(user_input):\n"
        "    eval(user_input)\n"
        "    subprocess.call(user_input, shell=True)\n"
        "    try:\n"
        "        pass\n"
        "    except:\n"
        "        pass\n"
    )
    test_file = tmp_path / "vulnerable.py"
    test_file.write_text(code, encoding="utf-8")

    findings = scan_file_statically(test_file, "vulnerable.py")

    assert len(findings) >= 3
    categories = [f.issue_category for f in findings]
    assert IssueCategory.SECURITY in categories
    assert IssueCategory.CODE_SMELL in categories

    severities = [f.severity for f in findings]
    assert IssueSeverity.CRITICAL in severities  # eval
    assert IssueSeverity.HIGH in severities      # shell=True


def test_scan_file_statically_syntax_error(tmp_path: Path):
    """Verify static scanner reports SyntaxError gracefully."""
    bad_code = "def broken(\n"
    test_file = tmp_path / "syntax_err.py"
    test_file.write_text(bad_code, encoding="utf-8")

    findings = scan_file_statically(test_file, "syntax_err.py")
    assert len(findings) == 1
    assert findings[0].issue_category == IssueCategory.BUG
    assert "SyntaxError" in findings[0].description


def test_scan_file_with_ollama_mocked_success(tmp_path: Path):
    """Verify structured AI code finding parsing from Ollama."""
    test_file = tmp_path / "sample.py"
    test_file.write_text("def calculate():\n    return 42\n", encoding="utf-8")

    mock_client = MagicMock(spec=OllamaClient)
    expected_response = {
        "findings": [
            {
                "finding_id": "BUG-01",
                "line_range": "1-2",
                "issue_category": "MAINTAINABILITY",
                "severity": "LOW",
                "description": "Magic number returned without named constant.",
                "reasoning": "Returning raw 42 reduces readability.",
                "suggested_improvement": "Define DEFAULT_MAGIC_CONSTANT = 42.",
                "confidence": "HIGH",
            }
        ]
    }
    mock_client.chat_json.return_value = (expected_response, MagicMock())

    findings = scan_file_with_ollama(
        client=mock_client,
        model="llama3.1:8b",
        file_path=test_file,
        relative_path="sample.py",
    )

    assert len(findings) == 1
    f = findings[0]
    assert f.issue_category == IssueCategory.MAINTAINABILITY
    assert f.severity == IssueSeverity.LOW
    assert f.is_ai_generated is True
    assert f.requires_human_review is True
    assert "Magic number" in f.description


def test_scan_file_with_ollama_error_handling(tmp_path: Path):
    """Verify that Ollama connection failures return empty list without crashing."""
    test_file = tmp_path / "sample.py"
    test_file.write_text("print('hi')", encoding="utf-8")

    mock_client = MagicMock(spec=OllamaClient)
    mock_client.chat_json.side_effect = OllamaConnectionError("Offline")

    findings = scan_file_with_ollama(
        client=mock_client,
        model="llama3.1:8b",
        file_path=test_file,
        relative_path="sample.py",
    )
    assert findings == []


def test_code_inspector_empty_repo(tmp_path: Path):
    """Verify CodeInspector handles empty repository cleanly."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    inspector = CodeInspector()
    report = inspector.inspect_repository(empty_dir)

    assert report.total_files_discovered == 0
    assert report.inspected_files == []
    assert report.findings == []

