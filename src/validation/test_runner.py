"""Isolated test runner executing repository validation commands strictly inside the sandbox.

Enforces:
- Command whitelisting (python -m py_compile, pytest, ruff check only).
- Strict execution-time limits.
- Subprocess containment via DockerSandboxManager.
- Rejection of model-generated shell scripts or arbitrary commands.
- Accurate status categorization (PASSED, FAILED, NOT_RUNNABLE, ERROR).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

from src.validation.sandbox_manager import DockerSandboxManager, SandboxExecutionResult

logger = logging.getLogger(__name__)


class ValidationStatus(str, Enum):
    """Categorization of validation command execution outcome."""
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_RUNNABLE = "NOT_RUNNABLE"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class ValidationCommandResult:
    """Outcome of an individual validation command."""
    command_name: str
    command_args: List[str]
    status: ValidationStatus
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    description: str
    error_message: Optional[str] = None

    @property
    def is_passed(self) -> bool:
        return self.status == ValidationStatus.PASSED


class SandboxedValidationRunner:
    """Coordinates isolated execution of syntax compilation, test discovery, and linting."""

    def __init__(self, sandbox_manager: Optional[DockerSandboxManager] = None) -> None:
        self.sandbox = sandbox_manager or DockerSandboxManager()

    def run_syntax_compilation(
        self,
        working_dir: Path | str,
        files: Optional[List[str]] = None,
        timeout: int = 30,
    ) -> ValidationCommandResult:
        """Run python -m py_compile inside the sandbox across modified Python files."""
        # Whitelisted command shape
        if files:
            cmd = ["python", "-m", "py_compile", *files]
        else:
            cmd = ["python", "-m", "compileall", "-q", "."]

        exec_res: SandboxExecutionResult = self.sandbox.run_command(
            command_args=cmd,
            working_dir=working_dir,
            timeout=timeout,
        )

        if not exec_res.is_runnable:
            return ValidationCommandResult(
                command_name="py_compile",
                command_args=cmd,
                status=ValidationStatus.NOT_RUNNABLE,
                exit_code=exec_res.exit_code,
                stdout=exec_res.stdout,
                stderr=exec_res.stderr,
                duration_seconds=exec_res.duration_seconds,
                description="Python bytecode syntax compilation.",
                error_message=exec_res.error_message,
            )

        if exec_res.timed_out:
            return ValidationCommandResult(
                command_name="py_compile",
                command_args=cmd,
                status=ValidationStatus.ERROR,
                exit_code=124,
                stdout=exec_res.stdout,
                stderr=exec_res.stderr,
                duration_seconds=exec_res.duration_seconds,
                description="Python bytecode syntax compilation.",
                error_message=f"Syntax compilation timed out after {timeout}s.",
            )

        status = ValidationStatus.PASSED if exec_res.exit_code == 0 else ValidationStatus.FAILED
        return ValidationCommandResult(
            command_name="py_compile",
            command_args=cmd,
            status=status,
            exit_code=exec_res.exit_code,
            stdout=exec_res.stdout,
            stderr=exec_res.stderr,
            duration_seconds=exec_res.duration_seconds,
            description="Python bytecode syntax compilation.",
        )

    def run_tests(
        self,
        working_dir: Path | str,
        test_framework: str = "pytest",
        timeout: int = 60,
    ) -> ValidationCommandResult:
        """Run repository test suite strictly inside the isolated sandbox."""
        # Whitelisted commands only
        if test_framework == "pytest":
            cmd = ["pytest", "-v"]
        elif test_framework == "unittest":
            cmd = ["python", "-m", "unittest", "discover", "-v"]
        else:
            raise ValueError(f"Unsupported test framework: '{test_framework}'. Whitelisted: pytest, unittest.")

        exec_res = self.sandbox.run_command(
            command_args=cmd,
            working_dir=working_dir,
            timeout=timeout,
        )

        if not exec_res.is_runnable:
            return ValidationCommandResult(
                command_name=test_framework,
                command_args=cmd,
                status=ValidationStatus.NOT_RUNNABLE,
                exit_code=exec_res.exit_code,
                stdout=exec_res.stdout,
                stderr=exec_res.stderr,
                duration_seconds=exec_res.duration_seconds,
                description=f"Isolated repository test suite execution via {test_framework}.",
                error_message=exec_res.error_message,
            )

        if exec_res.timed_out:
            return ValidationCommandResult(
                command_name=test_framework,
                command_args=cmd,
                status=ValidationStatus.ERROR,
                exit_code=124,
                stdout=exec_res.stdout,
                stderr=exec_res.stderr,
                duration_seconds=exec_res.duration_seconds,
                description=f"Isolated repository test suite execution via {test_framework}.",
                error_message=f"Test execution timed out after {timeout}s in sandbox.",
            )

        # Distinguish exit codes
        # 0 = All tests passed
        # 1 = Tests ran, some failed
        # 5 = No tests found (pytest specific)
        if exec_res.exit_code == 0:
            status = ValidationStatus.PASSED
        elif exec_res.exit_code == 1:
            status = ValidationStatus.FAILED
        elif exec_res.exit_code == 5:
            # Pytest exit code 5: No tests collected
            status = ValidationStatus.SKIPPED
        else:
            # Missing dependency, error, or unrunnable environment inside container
            status = ValidationStatus.NOT_RUNNABLE

        return ValidationCommandResult(
            command_name=test_framework,
            command_args=cmd,
            status=status,
            exit_code=exec_res.exit_code,
            stdout=exec_res.stdout,
            stderr=exec_res.stderr,
            duration_seconds=exec_res.duration_seconds,
            description=f"Isolated repository test suite execution via {test_framework}.",
        )

    def run_linter(
        self,
        working_dir: Path | str,
        timeout: int = 30,
    ) -> ValidationCommandResult:
        """Run ruff check strictly inside the isolated sandbox."""
        cmd = ["ruff", "check", "."]

        exec_res = self.sandbox.run_command(
            command_args=cmd,
            working_dir=working_dir,
            timeout=timeout,
        )

        if not exec_res.is_runnable:
            return ValidationCommandResult(
                command_name="ruff",
                command_args=cmd,
                status=ValidationStatus.NOT_RUNNABLE,
                exit_code=exec_res.exit_code,
                stdout=exec_res.stdout,
                stderr=exec_res.stderr,
                duration_seconds=exec_res.duration_seconds,
                description="Code quality and linting check via Ruff.",
                error_message=exec_res.error_message,
            )

        status = ValidationStatus.PASSED if exec_res.exit_code == 0 else ValidationStatus.FAILED
        return ValidationCommandResult(
            command_name="ruff",
            command_args=cmd,
            status=status,
            exit_code=exec_res.exit_code,
            stdout=exec_res.stdout,
            stderr=exec_res.stderr,
            duration_seconds=exec_res.duration_seconds,
            description="Code quality and linting check via Ruff.",
        )

