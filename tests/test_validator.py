"""Unit tests for RepositoryValidator, SandboxedValidationRunner, and status aggregation."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.validation.sandbox_manager import DockerSandboxManager, SandboxExecutionResult
from src.validation.test_runner import SandboxedValidationRunner, ValidationStatus
from src.validation.validator import RepositoryValidator, ValidationReport


def test_validator_all_checks_passed(tmp_path: Path):
    """Verify overall PASSED status when syntax and tests succeed in sandbox."""
    mock_sandbox = MagicMock(spec=DockerSandboxManager)
    # Both py_compile and pytest return exit_code 0
    mock_sandbox.run_command.side_effect = [
        SandboxExecutionResult(exit_code=0, stdout="Compiled", stderr="", duration_seconds=0.5),  # py_compile
        SandboxExecutionResult(exit_code=0, stdout="10 passed", stderr="", duration_seconds=2.0), # pytest
    ]

    validator = RepositoryValidator(sandbox_manager=mock_sandbox)
    report = validator.validate_repository(working_dir=tmp_path, run_tests=True, run_linter=False)

    assert report.is_passed is True
    assert report.overall_status == ValidationStatus.PASSED
    assert report.passed_count == 2
    assert report.failed_count == 0
    assert report.not_runnable_count == 0
    assert "All 2 validation checks passed" in report.summary_message


def test_validator_test_failure_reports_failed(tmp_path: Path):
    """Verify overall FAILED status when assertions fail in test suite."""
    mock_sandbox = MagicMock(spec=DockerSandboxManager)
    mock_sandbox.run_command.side_effect = [
        SandboxExecutionResult(exit_code=0, stdout="Compiled", stderr="", duration_seconds=0.5),  # py_compile
        SandboxExecutionResult(exit_code=1, stdout="1 failed", stderr="", duration_seconds=2.0),  # pytest failed
    ]

    validator = RepositoryValidator(sandbox_manager=mock_sandbox)
    report = validator.validate_repository(working_dir=tmp_path, run_tests=True)

    assert report.is_failed is True
    assert report.overall_status == ValidationStatus.FAILED
    assert report.passed_count == 1
    assert report.failed_count == 1
    assert "Validation failed: 1 of 2 checks reported failures" in report.summary_message


def test_validator_sandbox_unavailable_returns_not_runnable(tmp_path: Path):
    """CRITICAL TEST: When sandbox is unavailable, status MUST be NOT_RUNNABLE and never PASSED."""
    mock_sandbox = MagicMock(spec=DockerSandboxManager)
    mock_sandbox.run_command.return_value = SandboxExecutionResult(
        exit_code=-1,
        stdout="",
        stderr="",
        duration_seconds=0.0,
        is_runnable=False,
        error_message="Docker unavailable on host.",
    )

    validator = RepositoryValidator(sandbox_manager=mock_sandbox)
    report = validator.validate_repository(working_dir=tmp_path, run_tests=True)

    assert report.is_not_runnable is True
    assert report.overall_status == ValidationStatus.NOT_RUNNABLE
    assert report.is_passed is False
    assert report.not_runnable_count == 2
    assert "Validation not runnable" in report.summary_message


def test_validator_timeout_returns_error(tmp_path: Path):
    """Verify command execution timeout results in ERROR status."""
    mock_sandbox = MagicMock(spec=DockerSandboxManager)
    mock_sandbox.run_command.side_effect = [
        SandboxExecutionResult(exit_code=0, stdout="Compiled", stderr="", duration_seconds=0.5),
        SandboxExecutionResult(exit_code=124, stdout="", stderr="", duration_seconds=60.0, timed_out=True),
    ]

    validator = RepositoryValidator(sandbox_manager=mock_sandbox)
    report = validator.validate_repository(working_dir=tmp_path, run_tests=True)

    assert report.overall_status == ValidationStatus.ERROR
    assert "timed out or encountered execution errors" in report.summary_message


def test_test_runner_rejects_unwhitelisted_framework():
    """Verify test runner rejects unapproved test framework commands."""
    runner = SandboxedValidationRunner(sandbox_manager=MagicMock())
    with pytest.raises(ValueError, match="Unsupported test framework"):
        runner.run_tests(working_dir=".", test_framework="custom_malicious_runner")


def test_test_runner_pytest_exit_code_5_classified_as_skipped():
    """Verify pytest exit code 5 (no tests collected) is marked as SKIPPED rather than failed."""
    mock_sandbox = MagicMock(spec=DockerSandboxManager)
    mock_sandbox.run_command.return_value = SandboxExecutionResult(
        exit_code=5,
        stdout="collected 0 items",
        stderr="",
        duration_seconds=0.2,
    )

    runner = SandboxedValidationRunner(sandbox_manager=mock_sandbox)
    cmd_res = runner.run_tests(working_dir=".", test_framework="pytest")

    assert cmd_res.status == ValidationStatus.SKIPPED
    assert cmd_res.exit_code == 5

