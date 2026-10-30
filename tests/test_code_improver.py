"""Unit tests for CodeImprover, AST pre-validation, and working-copy containment."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.analysis.code_improver import (
    CodeImprover,
    ImprovementStatus,
    compute_directory_manifest_hash,
    verify_reference_preservation,
)
from src.analysis.code_inspector import CodeFinding, IssueCategory, IssueSeverity
from src.services.ollama_client import OllamaClient, OllamaConnectionError


def test_reference_preservation_verification(tmp_path: Path):
    """Verify hash check detects untouched vs modified reference trees."""
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    (ref_dir / "app.py").write_text("x = 1\n", encoding="utf-8")

    baseline = compute_directory_manifest_hash(ref_dir)
    ok, discrepancies = verify_reference_preservation(ref_dir, baseline)
    assert ok is True
    assert len(discrepancies) == 0

    # Simulate unexpected mutation
    (ref_dir / "app.py").write_text("x = 2\n", encoding="utf-8")
    ok2, discrepancies2 = verify_reference_preservation(ref_dir, baseline)
    assert ok2 is False
    assert any("Reference file modified" in d for d in discrepancies2)


def test_apply_improvement_successful(tmp_path: Path):
    """Verify successful targeted replacement with valid AST and unified diff."""
    work_dir = tmp_path / "working"
    work_dir.mkdir()
    target_file = work_dir / "calc.py"
    target_file.write_text(
        "def compute():\n"
        "    try:\n"
        "        return 1 / 0\n"
        "    except:\n"
        "        pass\n",
        encoding="utf-8",
    )

    finding = CodeFinding(
        finding_id="SMELL-01",
        file_path="calc.py",
        line_range="4-5",
        issue_category=IssueCategory.CODE_SMELL,
        severity=IssueSeverity.MEDIUM,
        description="Bare except clause",
        reasoning="Catch specific exception",
        suggested_improvement="Catch ZeroDivisionError explicitly",
        confidence="HIGH",
    )

    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.chat_json.return_value = (
        {
            "original_snippet": "    except:\n        pass",
            "replacement_snippet": "    except ZeroDivisionError:\n        return 0",
            "explanation": "Catches ZeroDivisionError explicitly and returns fallback value.",
        },
        MagicMock(),
    )

    improver = CodeImprover(ollama_client=mock_ollama, model="llama3.1:8b")
    result = improver.apply_improvement(finding, working_dir=work_dir)

    assert result.status == ImprovementStatus.APPLIED
    assert result.is_applied is True
    assert "-    except:" in result.diff
    assert "+    except ZeroDivisionError:" in result.diff

    # Verify file content on disk
    updated_content = target_file.read_text(encoding="utf-8")
    assert "except ZeroDivisionError:" in updated_content
    assert "return 0" in updated_content


def test_apply_improvement_rejects_invalid_syntax_via_ast(tmp_path: Path):
    """Verify AST pre-validation catches invalid Python syntax and preserves file untouched."""
    work_dir = tmp_path / "working"
    work_dir.mkdir()
    target_file = work_dir / "syntax_check.py"
    original_code = "def valid():\n    return True\n"
    target_file.write_text(original_code, encoding="utf-8")

    finding = CodeFinding(
        finding_id="BUG-02",
        file_path="syntax_check.py",
        line_range="1-2",
        issue_category=IssueCategory.BUG,
        severity=IssueSeverity.HIGH,
        description="Refactor function",
        reasoning="",
        suggested_improvement="",
        confidence="HIGH",
    )

    mock_ollama = MagicMock(spec=OllamaClient)
    # Model returns broken syntax missing closing paren
    mock_ollama.chat_json.return_value = (
        {
            "original_snippet": "def valid():\n    return True",
            "replacement_snippet": "def valid(\n    return False",
            "explanation": "Introduced broken syntax.",
        },
        MagicMock(),
    )

    improver = CodeImprover(ollama_client=mock_ollama, model="llama3.1:8b")
    result = improver.apply_improvement(finding, working_dir=work_dir)

    assert result.status == ImprovementStatus.REJECTED_SYNTAX_ERROR
    assert result.is_applied is False
    assert "AST SyntaxError" in str(result.error_message)

    # CRITICAL: Verify file on disk was preserved completely untouched
    assert target_file.read_text(encoding="utf-8") == original_code


def test_apply_improvement_rejects_snippet_not_found(tmp_path: Path):
    """Verify rejection when the model's original snippet does not match file text."""
    work_dir = tmp_path / "working"
    work_dir.mkdir()
    target_file = work_dir / "module.py"
    target_file.write_text("x = 100\n", encoding="utf-8")

    finding = CodeFinding(
        finding_id="SMELL-03",
        file_path="module.py",
        line_range="1",
        issue_category=IssueCategory.CODE_SMELL,
        severity=IssueSeverity.LOW,
        description="Change variable",
        reasoning="",
        suggested_improvement="",
        confidence="HIGH",
    )

    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.chat_json.return_value = (
        {
            "original_snippet": "non_existent_code_xyz = 999",
            "replacement_snippet": "x = 200",
            "explanation": "Fix",
        },
        MagicMock(),
    )

    improver = CodeImprover(ollama_client=mock_ollama, model="llama3.1:8b")
    result = improver.apply_improvement(finding, working_dir=work_dir)

    assert result.status == ImprovementStatus.REJECTED_SNIPPET_NOT_FOUND
    assert result.is_applied is False
    assert target_file.read_text(encoding="utf-8") == "x = 100\n"


def test_apply_improvement_rejects_path_traversal(tmp_path: Path):
    """Verify security rejection when file path resolves outside working directory."""
    work_dir = tmp_path / "working"
    work_dir.mkdir()

    finding = CodeFinding(
        finding_id="SEC-01",
        file_path="../../outside.py",
        line_range="1",
        issue_category=IssueCategory.SECURITY,
        severity=IssueSeverity.CRITICAL,
        description="Path traversal",
        reasoning="",
        suggested_improvement="",
        confidence="HIGH",
    )

    improver = CodeImprover(ollama_client=MagicMock(), model="llama3.1:8b")
    result = improver.apply_improvement(finding, working_dir=work_dir)

    assert result.status == ImprovementStatus.REJECTED_SECURITY
    assert "Path traversal attempt blocked" in str(result.error_message)


def test_apply_batch_improvements(tmp_path: Path):
    """Verify batch execution tracking applied vs rejected modifications."""
    work_dir = tmp_path / "working"
    work_dir.mkdir()
    (work_dir / "f1.py").write_text("a = 1\n", encoding="utf-8")
    (work_dir / "f2.py").write_text("b = 2\n", encoding="utf-8")

    f1 = CodeFinding("F-01", "f1.py", "1", IssueCategory.CODE_SMELL, IssueSeverity.LOW, "desc", "re", "sug", "HIGH")
    f2 = CodeFinding("F-02", "f2.py", "1", IssueCategory.CODE_SMELL, IssueSeverity.LOW, "desc", "re", "sug", "HIGH")

    mock_ollama = MagicMock(spec=OllamaClient)
    # f1 succeeds, f2 fails with syntax error
    mock_ollama.chat_json.side_effect = [
        ({"original_snippet": "a = 1", "replacement_snippet": "a = 10", "explanation": "ok"}, MagicMock()),
        ({"original_snippet": "b = 2", "replacement_snippet": "b = == 20", "explanation": "bad"}, MagicMock()),
    ]

    improver = CodeImprover(ollama_client=mock_ollama, model="llama3.1:8b")
    batch_res = improver.apply_batch([f1, f2], working_dir=work_dir)

    assert batch_res.applied_count == 1
    assert batch_res.rejected_count == 1
    assert len(batch_res.improvements) == 2
    assert (work_dir / "f1.py").read_text(encoding="utf-8") == "a = 10\n"
    assert (work_dir / "f2.py").read_text(encoding="utf-8") == "b = 2\n"  # preserved


def test_apply_improvement_ollama_offline(tmp_path: Path):
    """Verify graceful handling when Ollama daemon is offline."""
    work_dir = tmp_path / "working"
    work_dir.mkdir()
    (work_dir / "code.py").write_text("val = 1\n", encoding="utf-8")

    finding = CodeFinding("F-01", "code.py", "1", IssueCategory.BUG, IssueSeverity.HIGH, "desc", "re", "sug", "HIGH")

    mock_ollama = MagicMock(spec=OllamaClient)
    mock_ollama.chat_json.side_effect = OllamaConnectionError("Daemon unreachable")

    improver = CodeImprover(ollama_client=mock_ollama, model="llama3.1:8b")
    result = improver.apply_improvement(finding, working_dir=work_dir)

    assert result.status == ImprovementStatus.REJECTED_OLLAMA_ERROR
    assert "Ollama error" in str(result.error_message)

