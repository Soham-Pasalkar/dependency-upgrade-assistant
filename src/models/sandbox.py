"""Pydantic schemas and enums for sandboxed validation, container execution, and reports."""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, model_validator


class ValidationStatus(str, Enum):
    """Execution and verification outcome status."""
    PASSED = "PASSED"
    FAILED = "FAILED"
    NO_TESTS_FOUND = "NO_TESTS_FOUND"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    NOT_RUNNABLE = "NOT_RUNNABLE"
    ERROR = "ERROR"        # Backward-compatible alias
    SKIPPED = "SKIPPED"    # Backward-compatible alias


# Alias for explicit clarity against recommendation ValidationStatus
SandboxValidationStatus = ValidationStatus


class ExecutionLifecycleState(str, Enum):
    """Server lifecycle state of an active or completed sandbox execution."""
    QUEUED = "QUEUED"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ValidationCommandResult(BaseModel):
    """Detailed outcome of an individual validation command inside the sandbox."""
    command_name: str = Field(..., description="Logical name of command (e.g. 'py_compile', 'pytest', 'npm_test')")
    command_args: List[str] = Field(..., description="Actual arguments executed")
    status: ValidationStatus = Field(..., description="Command execution status")
    exit_code: int = Field(..., description="Process exit code")
    stdout: str = Field(default="", description="Captured standard output (bounded)")
    stderr: str = Field(default="", description="Captured standard error (bounded)")
    duration_seconds: float = Field(default=0.0, description="Duration in seconds")
    description: str = Field(default="", description="Human-readable description of check")
    error_message: Optional[str] = Field(default=None, description="Diagnostic error message if failed")

    @property
    def is_passed(self) -> bool:
        return self.status == ValidationStatus.PASSED


class SandboxExecutionRequest(BaseModel):
    """Request payload to initiate containerized validation."""
    proposal_id: Optional[str] = Field(
        default=None,
        description="ID of server-stored upgrade proposal to validate",
    )
    manifest_type: Optional[str] = Field(
        default=None,
        description="Manifest type ('requirements.txt' or 'package.json')",
    )
    custom_test_cmd: Optional[List[str]] = Field(
        default=None,
        description="Not accepted. The server determines the test command; any value is rejected (422).",
    )
    timeout_seconds: Optional[int] = Field(
        default=60,
        ge=5,
        le=120,
        description="Maximum execution timeout in seconds (server ceiling: 120s)",
    )
    memory_limit_mb: Optional[int] = Field(
        default=512,
        ge=64,
        le=1024,
        description="Memory limit in megabytes (server ceiling: 1024MB)",
    )
    cpu_limit: Optional[str] = Field(
        default="1.0",
        description="CPU quota limit (server ceiling: 2.0)",
    )
    pids_limit: Optional[int] = Field(
        default=64,
        ge=16,
        le=128,
        description="Maximum process table limit (server ceiling: 128)",
    )
    image_override: Optional[str] = Field(
        default=None,
        description="Not accepted. The server selects the pinned image; any value is rejected (422).",
    )
    ecosystem: Optional[str] = Field(
        default=None,
        description="Target ecosystem ('python' or 'npm')",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_untrusted_parameters(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # 0. The validation operation is server-controlled and bound into approval (F13)
            if data.get("custom_test_cmd") is not None:
                raise ValueError(
                    "custom_test_cmd is not accepted: the server determines the test command for an approved plan."
                )
            if data.get("image_override") is not None:
                raise ValueError(
                    "image_override is not accepted: the server selects the pinned validation image."
                )
            # 1. Strictly forbid attempts to enable network
            if data.get("network_enabled") is True:
                raise ValueError("Enabling network is strictly forbidden in sandbox execution.")
            # 2. Strictly forbid client-controlled arbitrary workspace paths
            if "workspace_path" in data and data["workspace_path"] is not None:
                ws_val = str(data["workspace_path"]).strip()
                if ws_val.startswith(("/", "\\")) or ".." in ws_val or ":" in ws_val:
                    raise ValueError(
                        "Arbitrary workspace paths are forbidden. Provide an authentic proposal_id instead."
                    )
        return data


class ValidationReportResponse(BaseModel):
    """Complete structured validation report aggregating all checks, logs, and metadata."""
    execution_id: str = Field(..., description="Unique server execution identifier")
    proposal_id: Optional[str] = Field(default=None, description="Associated proposal ID if validated")
    status: ValidationStatus = Field(..., description="Aggregated overall validation status")
    lifecycle_state: ExecutionLifecycleState = Field(
        default=ExecutionLifecycleState.COMPLETED,
        description="Server lifecycle state",
    )
    is_runnable: bool = Field(..., description="True if container execution was operational")
    summary: str = Field(..., description="Human-readable executive summary of results")
    commands: List[ValidationCommandResult] = Field(default_factory=list, description="Results of individual commands")
    total_duration_seconds: float = Field(default=0.0, description="Total execution duration")
    image_used: str = Field(default="", description="Container image used for validation")
    image_digest: Optional[str] = Field(default=None, description="Pinned immutable SHA-256 digest of image")
    network_enabled: bool = Field(default=False, description="Strictly False (--network none)")
    resource_limits: Dict[str, Any] = Field(default_factory=dict, description="Resource limits applied")
    logs: str = Field(default="", description="Aggregated execution stdout/stderr logs")
    is_truncated: bool = Field(default=False, description="True if output exceeded buffer limit")
    test_discovery_status: Optional[str] = Field(default=None, description="Test suite discovery result")
    staging_status: Optional[str] = Field(default=None, description="Controlled dependency staging result")
    snapshot_files_count: int = Field(default=0, description="Number of files included in snapshot")
    snapshot_excluded_count: int = Field(default=0, description="Number of sensitive files excluded from snapshot")
    created_at: float = Field(..., description="Epoch timestamp of execution creation")
    completed_at: Optional[float] = Field(default=None, description="Epoch timestamp of execution completion")
    diagnostics: List[str] = Field(default_factory=list, description="Diagnostic notices or warnings")
    validation_fingerprint: Optional[str] = Field(
        default=None, description="SHA-256 of the server-controlled validation policy this run used"
    )
    validation_policy: Optional[Dict[str, Any]] = Field(
        default=None, description="Effective server-controlled validation policy (image, limits, discovery rules)"
    )


class SandboxExecutionLogResponse(BaseModel):
    """Log stream response for an active or completed execution."""
    execution_id: str = Field(..., description="Execution identifier")
    status: ValidationStatus = Field(..., description="Current execution status")
    lifecycle_state: ExecutionLifecycleState = Field(
        default=ExecutionLifecycleState.COMPLETED,
        description="Current server lifecycle state",
    )
    logs: str = Field(..., description="Captured stdout/stderr log text")
    truncated: bool = Field(default=False, description="True if output exceeded buffer limit")
