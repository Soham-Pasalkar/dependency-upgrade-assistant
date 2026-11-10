"""Sandbox validation service orchestrating snapshot isolation, container execution, and reporting."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from src.models.plan import UpgradeProposalResponse
from src.models.sandbox import (
    ExecutionLifecycleState,
    SandboxExecutionLogResponse,
    SandboxExecutionRequest,
    ValidationCommandResult,
    ValidationReportResponse,
    ValidationStatus,
)
from src.sandbox.container_config import SandboxSecurityConfig
from src.sandbox.image_catalog import (
    get_default_image_for_ecosystem,
    validate_image_or_raise,
)
from src.sandbox.runner import (
    DockerAvailability,
    DockerSandboxRunner,
    SandboxExecutionResult,
    SandboxRunnerInterface,
)
from src.sandbox.snapshot_manager import IsolatedSnapshot, SnapshotManager
from src.services.proposal_registry import ProposalRegistry, get_proposal_registry

logger = logging.getLogger(__name__)


class SandboxService:
    """Coordinates isolated sandbox execution for dependency upgrade proposals."""

    def __init__(
        self,
        runner: Optional[SandboxRunnerInterface] = None,
        proposal_registry: Optional[ProposalRegistry] = None,
    ) -> None:
        self.runner = runner or DockerSandboxRunner()
        self.proposal_registry = proposal_registry or get_proposal_registry()
        self._executions: Dict[str, ValidationReportResponse] = {}
        self._execution_containers: Dict[str, str] = {}
        self._lock = threading.Lock()

    def get_availability(self) -> DockerAvailability:
        """Check whether the underlying Docker sandbox runtime is operational."""
        return self.runner.check_availability()

    def get_execution(self, execution_id: str) -> Optional[ValidationReportResponse]:
        """Retrieve the current report and status for an execution."""
        with self._lock:
            return self._executions.get(execution_id)

    def get_execution_logs(self, execution_id: str) -> Optional[SandboxExecutionLogResponse]:
        """Retrieve logs captured for an execution."""
        with self._lock:
            report = self._executions.get(execution_id)
            if not report:
                return None
            return SandboxExecutionLogResponse(
                execution_id=execution_id,
                status=report.status,
                logs=report.logs,
                truncated="[Output truncated after reaching" in report.logs,
            )

    def cancel_execution(self, execution_id: str) -> bool:
        """Terminate an active container execution and mark status CANCELLED."""
        with self._lock:
            report = self._executions.get(execution_id)
            if not report:
                return False
            container_id = self._execution_containers.get(execution_id)
            if container_id:
                self.runner.terminate(container_id)
            # Update status
            updated_report = report.model_copy(
                update={
                    "status": ValidationStatus.CANCELLED,
                    "summary": "Validation execution was cancelled by user request.",
                    "completed_at": time.time(),
                }
            )
            self._executions[execution_id] = updated_report
            return True

    def execute_validation(
        self,
        request: SandboxExecutionRequest,
        source_workspace: Optional[Path | str] = None,
    ) -> ValidationReportResponse:
        """Execute sandboxed validation for a proposal or workspace snapshot.

        Enforces:
        1. Authentic proposal resolution from ProposalRegistry (preventing tampering).
        2. Ephemeral isolated snapshot creation with secret scrubbing.
        3. Strict allowlisted container image selection.
        4. Non-root user, `--network none`, capability dropping, resource quotas.
        5. Truthful NOT_RUNNABLE reporting when Docker is unavailable.
        6. Fail-closed cleanup of containers and temporary directories.
        """
        execution_id = f"val-{uuid.uuid4().hex[:12]}"
        created_at = time.time()
        start_time = time.monotonic()

        # 1. Resolve Proposal if proposal_id provided
        proposal: Optional[UpgradeProposalResponse] = None
        if request.proposal_id:
            stored = self.proposal_registry.get(request.proposal_id)
            if not stored:
                raise ValueError(
                    f"Proposal '{request.proposal_id}' not found in registry. "
                    "Proposal may have expired or was not registered."
                )
            proposal = stored.proposal

        # Determine ecosystem and manifest type
        manifest_type = (
            request.manifest_type
            or (proposal.manifest_type if proposal else None)
            or "requirements.txt"
        )
        ecosystem = request.ecosystem or (proposal.ecosystem.value if proposal and hasattr(proposal.ecosystem, "value") else "python")

        # Select & validate container image
        if request.image_override:
            container_image = validate_image_or_raise(request.image_override)
        else:
            container_image = get_default_image_for_ecosystem(ecosystem or manifest_type)

        # 2. Check Docker Availability (Fail-Closed Policy)
        availability = self.runner.check_availability()
        if not availability.is_available:
            summary = (
                f"Docker sandbox unavailable: {availability.error_message}. "
                "In accordance with security policy, untrusted repository code will not be executed on the host."
            )
            report = ValidationReportResponse(
                execution_id=execution_id,
                proposal_id=request.proposal_id,
                status=ValidationStatus.NOT_RUNNABLE,
                is_runnable=False,
                summary=summary,
                commands=[],
                total_duration_seconds=0.0,
                image_used=container_image,
                network_enabled=request.network_enabled,
                resource_limits={
                    "memory_limit_mb": request.memory_limit_mb,
                    "cpu_limit": request.cpu_limit,
                    "pids_limit": request.pids_limit,
                    "timeout_seconds": request.timeout_seconds,
                },
                logs=f"[DIAGNOSTIC] {summary}\n",
                created_at=created_at,
                completed_at=time.time(),
                diagnostics=[summary],
            )
            with self._lock:
                self._executions[execution_id] = report
            return report

        # 3. Create Ephemeral Isolated Workspace Snapshot
        snapshot: Optional[IsolatedSnapshot] = None
        command_results: List[ValidationCommandResult] = []
        combined_logs: List[str] = []
        diagnostics: List[str] = []

        # Configure container security policy
        security_config = SandboxSecurityConfig(
            image=container_image,
            network_enabled=request.network_enabled,
            memory_limit=f"{request.memory_limit_mb or 512}m",
            cpu_limit=request.cpu_limit or "1.0",
            pids_limit=request.pids_limit or 64,
            timeout_seconds=request.timeout_seconds or 60,
        )

        try:
            snapshot = SnapshotManager.create_snapshot(
                source_workspace=source_workspace or request.workspace_path,
                proposal=proposal,
                manifest_type=manifest_type,
            )

            # Determine commands to run inside sandbox
            commands_to_run: List[tuple[str, List[str], str]] = []

            if request.custom_test_cmd:
                commands_to_run.append((
                    "custom_test",
                    request.custom_test_cmd,
                    "User-specified test execution.",
                ))
            elif "node" in ecosystem or "npm" in ecosystem or manifest_type == "package.json":
                # Node.js validation flow
                commands_to_run.append((
                    "syntax_check",
                    ["node", "-e", "JSON.parse(require('fs').readFileSync('package.json', 'utf8'))"],
                    "Package.json syntax validation.",
                ))
                commands_to_run.append((
                    "test_discovery",
                    ["npm", "test", "--if-present"],
                    "Node.js test suite execution.",
                ))
            else:
                # Python validation flow
                commands_to_run.append((
                    "py_compile",
                    ["python", "-m", "compileall", "-q", "."],
                    "Python bytecode syntax compilation.",
                ))
                commands_to_run.append((
                    "pytest",
                    ["pytest", "-q"],
                    "Python test suite execution.",
                ))

            # Execute commands sequentially inside container
            for cmd_name, cmd_args, desc in commands_to_run:
                combined_logs.append(f">>> Running {cmd_name}: {' '.join(cmd_args)}")

                res: SandboxExecutionResult = self.runner.run_command(
                    command_args=cmd_args,
                    working_dir=snapshot.path,
                    timeout=request.timeout_seconds,
                    config=security_config,
                )

                if res.container_id:
                    with self._lock:
                        self._execution_containers[execution_id] = res.container_id

                cmd_status = ValidationStatus.PASSED
                if not res.is_runnable:
                    cmd_status = ValidationStatus.NOT_RUNNABLE
                elif res.timed_out:
                    cmd_status = ValidationStatus.TIMED_OUT
                elif res.exit_code != 0:
                    cmd_status = ValidationStatus.FAILED

                if res.stdout:
                    combined_logs.append(res.stdout)
                if res.stderr:
                    combined_logs.append(f"[STDERR]\n{res.stderr}")
                if res.error_message:
                    combined_logs.append(f"[ERROR] {res.error_message}")

                cmd_res = ValidationCommandResult(
                    command_name=cmd_name,
                    command_args=cmd_args,
                    status=cmd_status,
                    exit_code=res.exit_code,
                    stdout=res.stdout,
                    stderr=res.stderr,
                    duration_seconds=res.duration_seconds,
                    description=desc,
                    error_message=res.error_message,
                )
                command_results.append(cmd_res)

                # Stop early if a command fails or times out
                if not cmd_res.is_passed:
                    break

        except Exception as exc:
            logger.exception(f"Unexpected error during sandboxed validation: {exc}")
            err_msg = f"Infrastructure error during sandbox execution: {exc}"
            diagnostics.append(err_msg)
            combined_logs.append(f"[INFRASTRUCTURE ERROR] {err_msg}")
        finally:
            if snapshot:
                snapshot.cleanup()

        # Compute overall status
        total_duration = round(time.monotonic() - start_time, 2)
        overall_status = ValidationStatus.PASSED

        if diagnostics and not command_results:
            overall_status = ValidationStatus.INFRASTRUCTURE_ERROR
            summary = f"Validation failed with infrastructure error: {diagnostics[0]}"
        elif any(c.status == ValidationStatus.TIMED_OUT for c in command_results):
            overall_status = ValidationStatus.TIMED_OUT
            summary = "Validation timed out while executing inside isolated container."
        elif any(c.status == ValidationStatus.NOT_RUNNABLE for c in command_results):
            overall_status = ValidationStatus.NOT_RUNNABLE
            summary = "Validation could not run because container runtime is unavailable."
        elif any(c.status == ValidationStatus.FAILED for c in command_results):
            failed_cmd = next(c for c in command_results if c.status == ValidationStatus.FAILED)
            overall_status = ValidationStatus.FAILED
            summary = f"Validation failed during '{failed_cmd.command_name}' (exit code {failed_cmd.exit_code})."
        elif command_results and all(c.status == ValidationStatus.PASSED for c in command_results):
            overall_status = ValidationStatus.PASSED
            summary = f"All {len(command_results)} validation checks passed successfully inside isolated sandbox."
        else:
            overall_status = ValidationStatus.FAILED
            summary = "Validation finished with errors."

        report = ValidationReportResponse(
            execution_id=execution_id,
            proposal_id=request.proposal_id,
            status=overall_status,
            is_runnable=overall_status != ValidationStatus.NOT_RUNNABLE,
            summary=summary,
            commands=command_results,
            total_duration_seconds=total_duration,
            image_used=container_image,
            network_enabled=request.network_enabled,
            resource_limits={
                "memory_limit_mb": request.memory_limit_mb,
                "cpu_limit": request.cpu_limit,
                "pids_limit": request.pids_limit,
                "timeout_seconds": request.timeout_seconds,
            },
            logs="\n".join(combined_logs),
            created_at=created_at,
            completed_at=time.time(),
            diagnostics=diagnostics,
        )

        with self._lock:
            self._executions[execution_id] = report

        return report


# Global singleton instance provider
_sandbox_service_instance: Optional[SandboxService] = None


def get_sandbox_service() -> SandboxService:
    """Retrieve the application singleton SandboxService."""
    global _sandbox_service_instance
    if _sandbox_service_instance is None:
        _sandbox_service_instance = SandboxService()
    return _sandbox_service_instance
