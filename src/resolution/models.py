"""Resolution evidence, job and input-classification models (FR-23, FR-27, FR-29, FR-32)."""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ResolutionStatus(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    CONFLICT = "CONFLICT"
    UNSUPPORTED = "UNSUPPORTED"
    RESOLUTION_FAILED = "RESOLUTION_FAILED"
    RESOLVER_UNAVAILABLE = "RESOLVER_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class ResolutionRole(str, Enum):
    ORIGINAL = "original"  # informational baseline (FR-32b)
    PROPOSED = "proposed"  # authoritative for enforcement (FR-28)


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    INTERRUPTED = "INTERRUPTED"


ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)
TERMINAL_JOB_STATUSES = (
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.TIMED_OUT,
    JobStatus.INTERRUPTED,
)


class UnsupportedReason(str, Enum):
    NON_PYTHON = "non_python_manifest"
    PYPROJECT_ONLY = "pyproject_not_supported"
    NESTED_REQUIREMENTS = "nested_requirements"
    EDITABLE = "editable_requirement"
    DIRECT_REFERENCE = "url_vcs_or_path_requirement"
    PIP_OPTION = "pip_option_not_supported"
    ENV_VARIABLE = "environment_variable"
    UNPARSEABLE = "unparseable_requirement"
    CONSTRAINTS_OUTSIDE_WORKSPACE = "constraints_not_repository_local"
    CONSTRAINTS_INVALID = "constraints_file_invalid"
    INPUT_TOO_LARGE = "input_too_large"
    SOURCE_BUILD_REQUIRED = "source_build_required"
    SOURCE_ONLY_CONFLICT = "conflict_involves_source_only_releases"


class UnsupportedItem(BaseModel):
    reason: UnsupportedReason
    message: str
    line_number: Optional[int] = Field(default=None, description="1-based line in the manifest or constraints file")
    source: str = Field(default="manifest", description="'manifest' or the constraints file path")
    text: Optional[str] = Field(default=None, description="Offending line, truncated to 200 characters")


class ConflictDiagnostic(BaseModel):
    summary: str
    causes: List[str] = Field(default_factory=list)


class UploadDate(BaseModel):
    """FR-32c: upload time of the exact selected file, or an explicit 'date unavailable'."""

    filename: Optional[str] = Field(default=None, description="Exact distribution file the date belongs to")
    value: Optional[str] = Field(default=None, description="Upload time normalized to UTC (RFC 3339, 'Z')")
    available: bool = False
    source: Optional[str] = Field(default=None, description="'pep700-simple-json' or 'pypi-json-api'")
    endpoint: Optional[str] = None
    retrieved_at: Optional[str] = None
    reason: Optional[str] = Field(default=None, description="Why the date is unavailable")

    @property
    def display(self) -> str:
        return self.value if self.available and self.value else "date unavailable"


class ResolvedPackage(BaseModel):
    name: str
    version: str
    direct: bool
    requested_extras: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(
        default_factory=list, description="Specifiers that applied to this package (input and Requires-Dist)"
    )
    filename: Optional[str] = None
    sha256: Optional[str] = None
    upload: UploadDate = Field(default_factory=UploadDate)
    introduced: Optional[bool] = Field(default=None, description="FR-32b; None when the comparison is unavailable")


class Comparison(BaseModel):
    available: bool
    reason: Optional[str] = Field(default=None, description="Why the comparison is unavailable")
    introduced: List[str] = Field(default_factory=list)


class ResolutionReport(BaseModel):
    """Supervisor-produced evidence for one role. Never built from client input."""

    policy_version: str
    role: ResolutionRole
    status: ResolutionStatus
    ecosystem: str = "python"
    fingerprint: str
    resolver: Optional[str] = Field(default=None, description="pip version that produced the solution")
    image: Optional[str] = None
    target: dict = Field(default_factory=dict)
    allowlist_version: Optional[str] = None
    inputs: dict = Field(default_factory=dict, description="SHA-256 digests of the resolution inputs")
    packages: List[ResolvedPackage] = Field(default_factory=list)
    conflicts: List[ConflictDiagnostic] = Field(default_factory=list)
    unsupported: List[UnsupportedItem] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    comparison: Optional[Comparison] = None
    disclosure: Optional[str] = None
    failure: Optional[str] = Field(default=None, description="Reason code and detail when not COMPATIBLE")
    created_at: float
