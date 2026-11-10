"""Pydantic schemas and enums for sandboxed validation, container execution, and reports."""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ValidationStatus(str, Enum):
    """Execution and verification outcome status."""
    PASSED = "PASSED"
    FAILED = "FAILED"
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
    workspace_path: Optional[str] = Field(
        default=None,
        description="Optional relative workspace path to snapshot and test",
    )
    manifest_type: Optional[str] = Field(
        default=None,
        description="Manifest type ('requirements.txt' or 'package.json')",
    )
    custom_test_cmd: Optional[List[str]] = Field(
        default=None,
        description="Optional whitelisted test command override (e.g. ['pytest', '-v', 'tests/'])",
    )
    timeout_seconds: Optional[int] = Field(
        default=60,
        ge=5,
        le=300,
        description="Maximum execution timeout in seconds",
    )
    memory_limit_mb: Optional[int] = Field(
        default=512,
        ge=64,
        le=4096,
        description="Memory limit in megabytes",
    )
    cpu_limit: Optional[str] = Field(
        default="1.0",
        description="CPU quota limit (e.g. '1.0' or '2.0')",
    )
    pids_limit: Optional[int] = Field(
        default=64,
        ge=16,
        le=512,
        description="Maximum process table limit",
    )
    network_enabled: bool = Field(
        default=False,
        description="Whether network access is permitted (default: False for --network none)",
    )
    image_override: Optional[str] = Field(
        default=None,
        description="Optional allowlisted container image override",
    )
    ecosystem: Optional[str] = Field(
        default=None,
        description="Target ecosystem ('python' or 'npm')",
    )


class ValidationReportResponse(BaseModel):
    """Complete structured validation report aggregating all checks, logs, and metadata."""
    execution_id: str = Field(..., description="Unique server execution identifier")
    proposal_id: Optional[str] = Field(default=None, description="Associated proposal ID if validated")
    status: ValidationStatus = Field(..., description="Aggregated overall validation status")
    is_runnable: bool = Field(..., description="True if container execution was operational")
    summary: str = Field(..., description="Human-readable executive summary of results")
    commands: List[ValidationCommandResult] = Field(default_factory=list, description="Results of individual commands")
    total_duration_seconds: float = Field(default=0.0, description="Total execution duration")
    image_used: str = Field(default="", description="Container image used for validation")
    network_enabled: bool = Field(default=False, description="Whether network was enabled")
    resource_limits: Dict[str, Any] = Field(default_factory=dict, description="Resource limits applied")
    logs: str = Field(default="", description="Aggregated execution stdout/stderr logs")
    created_at: float = Field(..., description="Epoch timestamp of execution creation")
    completed_at: Optional[float] = Field(default=None, description="Epoch timestamp of execution completion")
    diagnostics: List[str] = Field(default_factory=list, description="Diagnostic notices or warnings")


class SandboxExecutionLogResponse(BaseModel):
    """Log stream response for an active or completed execution."""
    execution_id: str = Field(..., description="Execution identifier")
    status: ValidationStatus = Field(..., description="Current execution status")
    logs: str = Field(..., description="Captured stdout/stderr log text")
    truncated: bool = Field(default=False, description="True if output exceeded buffer limit")
