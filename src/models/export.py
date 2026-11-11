"""Pydantic v2 schemas and enums for artifact export, deliverables, and checksum verification."""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ArtifactType(str, Enum):
    """Deliverable artifact types available for download."""
    PATCH = "patch"
    SUMMARY_JSON = "summary_json"
    EXECUTION_LOG = "execution_log"
    CHECKSUMS = "checksums"
    BUNDLE_ZIP = "bundle_zip"


class ExportItemMetadata(BaseModel):
    """Metadata for an individual exported deliverable file."""
    artifact_type: ArtifactType = Field(..., description="Type of deliverable artifact")
    filename: str = Field(..., description="Safe filename for download")
    mime_type: str = Field(..., description="MIME content type")
    size_bytes: int = Field(..., description="File size in bytes")
    sha256: str = Field(..., description="Server-computed SHA-256 cryptographic digest")
    download_url: str = Field(..., description="Relative or absolute URL to download artifact")


class ExportRequest(BaseModel):
    """Request payload to generate verified deliverables for a proposal or run."""
    proposal_id: Optional[str] = Field(
        default=None,
        description="Unique identifier of server-stored upgrade proposal to export",
    )
    execution_id: Optional[str] = Field(
        default=None,
        description="Optional validation execution ID to attach execution logs and outcomes",
    )
    run_id: Optional[str] = Field(
        default=None,
        description="Optional repository analysis run ID",
    )
    include_logs: bool = Field(
        default=True,
        description="Whether to include validation execution logs in the export bundle",
    )
    custom_notes: Optional[str] = Field(
        default=None,
        description="Optional human reviewer notes to embed in summary",
    )


class ExportMetadataResponse(BaseModel):
    """Complete response payload detailing generated export bundle and deliverable checksums."""
    export_id: str = Field(..., description="Unique server-assigned export identifier")
    proposal_id: Optional[str] = Field(default=None, description="Source proposal identifier")
    execution_id: Optional[str] = Field(default=None, description="Source validation execution identifier")
    run_id: Optional[str] = Field(default=None, description="Source analysis run identifier")
    created_at: float = Field(..., description="Epoch timestamp of export bundle creation")
    sanitized: bool = Field(default=True, description="Zero-credential sanitization confirmation")
    secret_scan_passed: bool = Field(default=True, description="Confirmation that all artifacts passed secret scanning")
    validation_status: Optional[str] = Field(
        default=None,
        description="Truthful validation outcome status (e.g. PASSED, FAILED, NOT_RUNNABLE, NOT_PERFORMED)",
    )
    items: List[ExportItemMetadata] = Field(
        default_factory=list,
        description="List of individual deliverable files with server-calculated checksums",
    )
    summary: Dict[str, Any] = Field(
        default_factory=dict,
        description="Executive summary and statistics of the exported deliverables",
    )

