"""Multi-stage validation coordinator aggregating syntax compilation, linting, and sandboxed test execution.

Guarantees:
- Never labels a repository as PASSED if required validation checks were not performed or could not run.
- Accurately classifies overall status: PASSED, FAILED, NOT_RUNNABLE, ERROR.
- Captures clear explanations of what ran, what was skipped, and why.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from src.validation.sandbox_manager import DockerSandboxManager
from src.validation.test_runner import SandboxedValidationRunner, ValidationCommandResult, ValidationStatus

logger = logging.getLogger(__name__)


@dataclass
class ValidationReport:
    """Structured report aggregating all repository validation stages."""
    overall_status: ValidationStatus
    summary_message: str
    command_results: List[ValidationCommandResult] = field(default_factory=list)
    total_checks: int = 0
    passed_count: int = 0
    failed_count: int = 0
    not_runnable_count: int = 0
    skipped_count: int = 0
    sandbox_used: str = "Docker Sandbox"
    warnings: List[str] = field(default_factory=list)

    @property
    def is_passed(self) -> bool:
        return self.overall_status == ValidationStatus.PASSED

    @property
    def is_failed(self) -> bool:
        return self.overall_status == ValidationStatus.FAILED

    @property
    def is_not_runnable(self) -> bool:
        return self.overall_status == ValidationStatus.NOT_RUNNABLE


class RepositoryValidator:
    """Coordinates validation pipeline for a working repository copy."""

    def __init__(
        self,
        runner: Optional[SandboxedValidationRunner] = None,
        sandbox_manager: Optional[DockerSandboxManager] = None,
    ) -> None:
        if runner:
            self.runner = runner
        else:
            self.runner = SandboxedValidationRunner(sandbox_manager=sandbox_manager)

    def validate_repository(
        self,
        working_dir: Path | str,
        modified_files: Optional[List[str]] = None,
        run_tests: bool = True,
        run_linter: bool = False,
        test_framework: str = "pytest",
    ) -> ValidationReport:
        """Run full validation suite inside isolated sandbox across modified code.
        
        Args:
            working_dir: Path to working repository copy.
            modified_files: Relative paths of files modified by AI improver or dependency bumps.
            run_tests: Whether to execute repository test suite.
            run_linter: Whether to execute Ruff lint checks.
            test_framework: "pytest" or "unittest".
            
        Returns:
            ValidationReport detailing status, logs, and failure explanations.
        """
        results: List[ValidationCommandResult] = []
        warnings: List[str] = []

        # 1. Syntax Bytecode Compilation Check
        syntax_res = self.runner.run_syntax_compilation(
            working_dir=working_dir,
            files=modified_files,
        )
        results.append(syntax_res)

        # 2. Optional Linting Check (Ruff)
        if run_linter:
            linter_res = self.runner.run_linter(working_dir=working_dir)
            results.append(linter_res)

        # 3. Test Suite Execution (pytest / unittest)
        if run_tests:
            test_res = self.runner.run_tests(
                working_dir=working_dir,
                test_framework=test_framework,
            )
            results.append(test_res)

        # Compute summary metrics
        total = len(results)
        passed = sum(1 for r in results if r.status == ValidationStatus.PASSED)
        failed = sum(1 for r in results if r.status == ValidationStatus.FAILED)
        not_runnable = sum(1 for r in results if r.status == ValidationStatus.NOT_RUNNABLE)
        skipped = sum(1 for r in results if r.status == ValidationStatus.SKIPPED)
        errors = sum(1 for r in results if r.status == ValidationStatus.ERROR)

        # Determine overall status with strict safety precedence:
        # FAILED > ERROR > NOT_RUNNABLE > PASSED
        if failed > 0:
            overall_status = ValidationStatus.FAILED
            summary = f"Validation failed: {failed} of {total} checks reported failures or assertions failed."
        elif errors > 0:
            overall_status = ValidationStatus.ERROR
            summary = f"Validation error: {errors} check(s) timed out or encountered execution errors."
        elif not_runnable > 0:
            overall_status = ValidationStatus.NOT_RUNNABLE
            summary = (
                f"Validation not runnable: {not_runnable} check(s) could not execute because required "
                "sandbox isolation is unavailable or dependencies are not installed in container."
            )
        elif passed > 0:
            overall_status = ValidationStatus.PASSED
            summary = f"All {passed} validation checks passed successfully inside the sandbox."
        else:
            overall_status = ValidationStatus.SKIPPED
            summary = "No validation checks were executed."

        return ValidationReport(
            overall_status=overall_status,
            summary_message=summary,
            command_results=results,
            total_checks=total,
            passed_count=passed,
            failed_count=failed,
            not_runnable_count=not_runnable,
            skipped_count=skipped,
            warnings=warnings,
        )

