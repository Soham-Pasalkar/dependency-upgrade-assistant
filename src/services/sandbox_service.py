"""Sandbox validation service orchestrating snapshot isolation, container execution, and reporting."""
from __future__ import annotations

import logging
import subprocess
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
from src.sandbox.container_config import (
    SandboxSecurityConfig,
    sanitize_resource_limits,
)
from src.sandbox.dependency_staging import DependencyStagingManager, StagingResult
from src.sandbox.image_catalog import (
    get_default_image_for_ecosystem,
    get_image_digest,
    validate_image_or_raise,
)
from src.sandbox.runner import (
    DockerAvailability,
    DockerSandboxRunner,
    SandboxExecutionResult,
    SandboxRunnerInterface,
)
from src.sandbox.snapshot_manager import IsolatedSnapshot, SnapshotManager
from src.sandbox.test_discovery import (
    TestDiscoveryManager,
    TestDiscoveryResult,
    TestDiscoveryStatus,
)
from src.sandbox.workspace_boundary import validate_trusted_workspace_path
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
        self._execution_states: Dict[str, ExecutionLifecycleState] = {}
        self._execution_containers: Dict[str, str] = {}
        self._execution_procs: Dict[str, subprocess.Popen] = {}
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
                lifecycle_state=report.lifecycle_state,
                logs=report.logs,
                truncated=report.is_truncated or ("[Output truncated after reaching" in report.logs),
            )

    def cancel_execution(self, execution_id: str) -> bool:
        """Atomically terminate an active container execution and mark status CANCELLED."""
        with self._lock:
            report = self._executions.get(execution_id)
            if not report:
                return False

            current_state = self._execution_states.get(execution_id, ExecutionLifecycleState.COMPLETED)
            if current_state in (
                ExecutionLifecycleState.COMPLETED,
                ExecutionLifecycleState.FAILED,
                ExecutionLifecycleState.CANCELLED,
            ):
                # Already terminal, do not overwrite
                return True

            # Mark state as CANCELLED immediately
            self._execution_states[execution_id] = ExecutionLifecycleState.CANCELLED
            container_id = self._execution_containers.get(execution_id)
            proc = self._execution_procs.get(execution_id)

            # Terminate container via runner
            if container_id:
                self.runner.terminate(container_id)

            # Terminate host subprocess if active
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.kill()
                except Exception as e:
                    logger.debug(f"Process termination note: {e}")

            # Update report
            updated_report = report.model_copy(
                update={
                    "status": ValidationStatus.CANCELLED,
                    "lifecycle_state": ExecutionLifecycleState.CANCELLED,
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
        """Execute sandboxed validation with strict isolation, explicit test discovery, and cleanup.

        Enforces:
        1. Authentic proposal resolution from ProposalRegistry.
        2. Strict workspace path validation against server-controlled root.
        3. Ephemeral isolated snapshot creation with secret scrubbing.
        4. Strict allowlisted container image selection with pinned SHA-256 digest.
        5. Mandatory non-root user, `--network none`, capability dropping, resource quotas.
        6. Explicit test suite discovery preventing false-positive PASSED outcomes.
        7. Real-time cancellation registration and atomic lifecycle tracking.
        8. Truthful NOT_RUNNABLE reporting when Docker is unavailable.
        9. Fail-closed cleanup of containers, temporary directories, and staging caches.
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

        # Validate source workspace if provided (must reside strictly within server root)
        validated_source_ws: Optional[Path] = None
        if source_workspace:
            validated_source_ws = validate_trusted_workspace_path(source_workspace)

        # Determine ecosystem and manifest type
        manifest_type = (
            request.manifest_type
            or (proposal.manifest_type if proposal else None)
            or "requirements.txt"
        )
        ecosystem = (
            request.ecosystem
            or (proposal.ecosystem.value if proposal and hasattr(proposal.ecosystem, "value") else "python")
        )

        # Select & validate container image and resolve pinned digest
        if request.image_override:
            container_image = validate_image_or_raise(request.image_override)
        else:
            container_image = get_default_image_for_ecosystem(ecosystem or manifest_type)
        image_digest = get_image_digest(container_image)

        # Sanitize and clamp resource limits
        mem_mb, cpus, pids, timeout_sec = sanitize_resource_limits(
            memory_mb=request.memory_limit_mb,
            cpu_limit=request.cpu_limit,
            pids_limit=request.pids_limit,
            timeout_seconds=request.timeout_seconds,
        )

        # Initialize Execution Record in QUEUED state
        initial_report = ValidationReportResponse(
            execution_id=execution_id,
            proposal_id=request.proposal_id,
            status=ValidationStatus.NOT_RUNNABLE,
            lifecycle_state=ExecutionLifecycleState.QUEUED,
            is_runnable=False,
            summary="Validation execution queued.",
            commands=[],
            total_duration_seconds=0.0,
            image_used=container_image,
            image_digest=image_digest,
            network_enabled=False,
            resource_limits={
                "memory_limit_mb": mem_mb,
                "cpu_limit": cpus,
                "pids_limit": pids,
                "timeout_seconds": timeout_sec,
            },
            logs="",
            created_at=created_at,
            completed_at=None,
            diagnostics=[],
        )

        with self._lock:
            self._executions[execution_id] = initial_report
            self._execution_states[execution_id] = ExecutionLifecycleState.QUEUED

        # 2. Check Docker Availability (Fail-Closed Policy)
        availability = self.get_availability()
        if not availability.is_available:
            summary = (
                f"Docker sandbox unavailable: {availability.error_message}. "
                "In accordance with security policy, untrusted repository code will not be executed on the host."
            )
            report = initial_report.model_copy(
                update={
                    "status": ValidationStatus.NOT_RUNNABLE,
                    "lifecycle_state": ExecutionLifecycleState.COMPLETED,
                    "is_runnable": False,
                    "summary": summary,
                    "logs": f"[DIAGNOSTIC] {summary}\n",
                    "completed_at": time.time(),
                    "diagnostics": [summary],
                }
            )
            with self._lock:
                self._executions[execution_id] = report
                self._execution_states[execution_id] = ExecutionLifecycleState.COMPLETED
            return report

        # Transition to INITIALIZING
        with self._lock:
            self._execution_states[execution_id] = ExecutionLifecycleState.INITIALIZING
            self._executions[execution_id] = initial_report.model_copy(
                update={"lifecycle_state": ExecutionLifecycleState.INITIALIZING}
            )

        # 3. Controlled Dependency Staging (Stage A)
        staging_result: Optional[StagingResult] = None
        staging_status = "NOT_REQUIRED"
        try:
            staging_result = DependencyStagingManager.prepare_staging_cache(
                ecosystem=ecosystem,
            )
            staging_status = staging_result.status
        except Exception as stg_exc:
            logger.warning(f"Dependency staging note: {stg_exc}")
            staging_status = "SKIPPED"

        # 4. Create Ephemeral Isolated Workspace Snapshot (WP3)
        snapshot: Optional[IsolatedSnapshot] = None
        command_results: List[ValidationCommandResult] = []
        combined_logs: List[str] = []
        diagnostics: List[str] = []
        is_output_truncated = False

        security_config = SandboxSecurityConfig(
            image=container_image,
            network_enabled=False,  # Enforced --network=none
            memory_mb=mem_mb,
            cpu_limit=cpus,
            pids_limit=pids,
            timeout_seconds=timeout_sec,
            read_only_root=True,
            cap_drop_all=True,
            no_new_privileges=True,
        )

        try:
            snapshot = SnapshotManager.create_snapshot(
                source_workspace=validated_source_ws,
                proposal=proposal,
                manifest_type=manifest_type,
                container_user=security_config.user,
            )

            # 5. Explicit Test Discovery (WP7)
            discovery: TestDiscoveryResult = TestDiscoveryManager.discover_tests(
                snapshot_dir=snapshot.path,
                ecosystem=ecosystem,
                custom_cmd=request.custom_test_cmd,
            )
            test_discovery_status = discovery.status.value
            diagnostics.append(f"Test Discovery: {discovery.description}")

            # Determine commands to execute in container
            commands_to_run: List[tuple[str, List[str], str]] = []

            if "node" in ecosystem or "npm" in ecosystem or manifest_type == "package.json":
                # Node.js validation flow: syntax check first
                commands_to_run.append((
                    "syntax_check",
                    ["node", "-e", "JSON.parse(require('fs').readFileSync('package.json', 'utf8'))"],
                    "Package.json syntax validation.",
                ))
                if discovery.has_runnable_tests:
                    commands_to_run.append((
                        "test_execution",
                        discovery.test_command,
                        discovery.description,
                    ))
            else:
                # Python validation flow: bytecode compilation first
                commands_to_run.append((
                    "py_compile",
                    ["python", "-m", "compileall", "-q", "."],
                    "Python bytecode syntax compilation.",
                ))
                if discovery.has_runnable_tests:
                    commands_to_run.append((
                        "pytest",
                        discovery.test_command,
                        discovery.description,
                    ))

            # Callback for registering container identity before process execution
            def on_started(c_name: str, p_handle: subprocess.Popen) -> None:
                with self._lock:
                    self._execution_containers[execution_id] = c_name
                    self._execution_procs[execution_id] = p_handle
                    if self._execution_states.get(execution_id) != ExecutionLifecycleState.CANCELLED:
                        self._execution_states[execution_id] = ExecutionLifecycleState.RUNNING

            # Execute commands sequentially inside container
            for cmd_name, cmd_args, desc in commands_to_run:
                # Check for cancellation before starting command
                with self._lock:
                    if self._execution_states.get(execution_id) == ExecutionLifecycleState.CANCELLED:
                        break

                combined_logs.append(f">>> Running {cmd_name}: {' '.join(cmd_args)}")

                res: SandboxExecutionResult = self.runner.run_command(
                    command_args=cmd_args,
                    working_dir=snapshot.path,
                    staging_dir=staging_result.cache_dir if staging_result else None,
                    timeout=timeout_sec,
                    config=security_config,
                    on_process_started=on_started,
                )

                if res.is_truncated:
                    is_output_truncated = True

                cmd_status = ValidationStatus.PASSED
                if not res.is_runnable:
                    cmd_status = ValidationStatus.NOT_RUNNABLE
                elif res.timed_out:
                    cmd_status = ValidationStatus.TIMED_OUT
                elif res.exit_code != 0:
                    # In pytest, exit code 5 means zero tests collected
                    if cmd_name == "pytest" and res.exit_code == 5:
                        cmd_status = ValidationStatus.NO_TESTS_FOUND
                    else:
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

                # Stop execution if check failed or timed out
                if not cmd_res.is_passed:
                    break

        except Exception as exc:
            logger.exception(f"Unexpected error during sandboxed validation: {exc}")
            err_msg = f"Infrastructure error during sandbox execution: {exc}"
            diagnostics.append(err_msg)
            combined_logs.append(f"[INFRASTRUCTURE ERROR] {err_msg}")
        finally:
            if snapshot:
                try:
                    snapshot.cleanup()
                except Exception as clean_exc:
                    logger.error(f"Snapshot cleanup failed: {clean_exc}")
                    clean_msg = f"Snapshot cleanup failure: {clean_exc}"
                    diagnostics.append(clean_msg)
                    combined_logs.append(f"[CLEANUP ERROR] {clean_msg}")
            if staging_result:
                try:
                    staging_result.cleanup()
                except Exception as stg_clean_exc:
                    logger.error(f"Staging cleanup failed: {stg_clean_exc}")

        # Check if cancelled during execution
        with self._lock:
            if self._execution_states.get(execution_id) == ExecutionLifecycleState.CANCELLED:
                final_rep = self._executions[execution_id]
                return final_rep

        total_duration = round(time.monotonic() - start_time, 2)
        overall_status = ValidationStatus.PASSED

        # Derive overall status strictly from execution evidence (WP7)
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
        elif any(c.status == ValidationStatus.NO_TESTS_FOUND for c in command_results) or not discovery.has_runnable_tests:
            overall_status = ValidationStatus.NO_TESTS_FOUND
            summary = "Syntax compilation check passed; zero runnable tests were discovered in repository."
        elif command_results and all(c.status == ValidationStatus.PASSED for c in command_results):
            cleanup_errors = [d for d in diagnostics if "Snapshot cleanup failure" in d]
            if cleanup_errors:
                overall_status = ValidationStatus.INFRASTRUCTURE_ERROR
                summary = f"Validation checks passed, but snapshot cleanup failed: {cleanup_errors[0]}"
            else:
                overall_status = ValidationStatus.PASSED
                summary = f"All {len(command_results)} validation checks passed successfully inside isolated sandbox."
        else:
            overall_status = ValidationStatus.FAILED
            summary = "Validation finished with errors."

        final_lifecycle = (
            ExecutionLifecycleState.FAILED
            if overall_status in (ValidationStatus.FAILED, ValidationStatus.INFRASTRUCTURE_ERROR)
            else ExecutionLifecycleState.COMPLETED
        )

        report = ValidationReportResponse(
            execution_id=execution_id,
            proposal_id=request.proposal_id,
            status=overall_status,
            lifecycle_state=final_lifecycle,
            is_runnable=overall_status != ValidationStatus.NOT_RUNNABLE,
            summary=summary,
            commands=command_results,
            total_duration_seconds=total_duration,
            image_used=container_image,
            image_digest=image_digest,
            network_enabled=False,
            resource_limits={
                "memory_limit_mb": mem_mb,
                "cpu_limit": cpus,
                "pids_limit": pids,
                "timeout_seconds": timeout_sec,
            },
            logs="\n".join(combined_logs),
            is_truncated=is_output_truncated,
            test_discovery_status=discovery.status.value,
            staging_status=staging_status,
            snapshot_files_count=snapshot.manifest.included_files_count if snapshot else 0,
            snapshot_excluded_count=snapshot.manifest.excluded_files_count if snapshot else 0,
            created_at=created_at,
            completed_at=time.time(),
            diagnostics=diagnostics,
        )

        with self._lock:
            # Prevent overwriting CANCELLED if user cancelled during report assembly
            if self._execution_states.get(execution_id) == ExecutionLifecycleState.CANCELLED:
                self._execution_containers.pop(execution_id, None)
                self._execution_procs.pop(execution_id, None)
                return self._executions[execution_id]

            self._executions[execution_id] = report
            self._execution_states[execution_id] = final_lifecycle
            self._execution_containers.pop(execution_id, None)
            self._execution_procs.pop(execution_id, None)

        return report


# Global singleton instance provider
_sandbox_service_instance: Optional[SandboxService] = None


def get_sandbox_service() -> SandboxService:
    """Retrieve the application singleton SandboxService."""
    global _sandbox_service_instance
    if _sandbox_service_instance is None:
        _sandbox_service_instance = SandboxService()
    return _sandbox_service_instance
