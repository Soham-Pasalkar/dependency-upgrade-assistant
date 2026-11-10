"""Pydantic v2 schemas for manifest ingestion and dependency analysis."""
from __future__ import annotations

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class Ecosystem(str, Enum):
    """Supported dependency ecosystems."""
    PYTHON = "python"
    NPM = "npm"


class DependencyType(str, Enum):
    """Type/scope of the dependency."""
    RUNTIME = "runtime"
    DEVELOPMENT = "development"


class DependencyStatus(str, Enum):
    """Evaluation classification of a dependency."""
    UP_TO_DATE = "UP_TO_DATE"
    UPGRADE_AVAILABLE = "UPGRADE_AVAILABLE"
    PRESERVED = "PRESERVED"


class RegistryLookupStatus(str, Enum):
    """Status of the external package registry query."""
    SUCCESS = "SUCCESS"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    SKIPPED = "SKIPPED"


class ParsedDependency(BaseModel):
    """A raw dependency extracted from a manifest."""
    name: str = Field(..., description="Normalized package name")
    original_name: str = Field(..., description="Package name as declared in manifest")
    ecosystem: Ecosystem = Field(..., description="Target ecosystem")
    dependency_type: DependencyType = Field(default=DependencyType.RUNTIME, description="Runtime or dev dependency")
    declared_specifier: str = Field(default="", description="Version constraint or specifier declared")
    current_version: Optional[str] = Field(default=None, description="Current pinned or resolved version if discernable")
    line_number: Optional[int] = Field(default=None, description="1-based line number in manifest file")
    raw_line: Optional[str] = Field(default=None, description="Original raw line from manifest")
    is_supported: bool = Field(default=True, description="Whether this dependency specification is eligible for automated analysis")
    diagnostic_message: Optional[str] = Field(default=None, description="Warning or explanation if line is unsupported or non-standard")
    extras: Optional[str] = Field(default=None, description="Package extras, e.g. [security]")
    environment_marker: Optional[str] = Field(default=None, description="Environment marker, e.g. python_version < '3.11'")


class AnalyzedDependency(BaseModel):
    """Enriched dependency with registry metadata and upgrade classification."""
    name: str = Field(..., description="Normalized package name")
    original_name: str = Field(..., description="Package name as declared in manifest")
    ecosystem: Ecosystem = Field(..., description="Target ecosystem")
    dependency_type: DependencyType = Field(default=DependencyType.RUNTIME)
    declared_specifier: str = Field(default="")
    current_version: Optional[str] = Field(default=None)
    latest_version: Optional[str] = Field(default=None, description="Latest available release in registry")
    latest_compatible_version: Optional[str] = Field(default=None, description="Latest release satisfying declared constraint")
    status: DependencyStatus = Field(..., description="UP_TO_DATE, UPGRADE_AVAILABLE, or PRESERVED")
    status_reason: str = Field(default="", description="Human-readable justification for the classification")
    registry_status: RegistryLookupStatus = Field(default=RegistryLookupStatus.SKIPPED)
    available_versions: List[str] = Field(default_factory=list, description="Sample of recent available versions")
    yanked_or_deprecated: bool = Field(default=False, description="Whether package/version is yanked on PyPI or deprecated on npm")
    deprecation_reason: Optional[str] = Field(default=None, description="Deprecation message from registry")
    line_number: Optional[int] = Field(default=None)
    raw_line: Optional[str] = Field(default=None)
    diagnostic_message: Optional[str] = Field(default=None)


class ManifestAnalysisRequest(BaseModel):
    """Request payload for analyzing raw manifest text."""
    manifest_type: str = Field(..., description="Manifest format: 'requirements.txt' or 'package.json'")
    content: str = Field(..., description="Raw manifest file contents")


class ManifestAnalysisResponse(BaseModel):
    """Complete analysis response for an ingested manifest."""
    manifest_type: str = Field(..., description="Manifest format analyzed")
    ecosystem: Ecosystem = Field(..., description="Target ecosystem")
    total_dependencies: int = Field(default=0)
    up_to_date_count: int = Field(default=0)
    upgrade_available_count: int = Field(default=0)
    preserved_count: int = Field(default=0)
    dependencies: List[AnalyzedDependency] = Field(default_factory=list)
    diagnostics: List[str] = Field(default_factory=list, description="Global or per-line diagnostic notices")

