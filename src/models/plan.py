"""Pydantic v2 schemas for upgrade planning, patch generation, and human approval gates."""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from src.models.manifest import DependencyType, Ecosystem
from src.models.recommendation import PackageUpgradeRecommendation


class UpgradeAction(str, Enum):
    """Action determined for a package in the upgrade plan."""
    UPGRADE = "UPGRADE"
    RETAIN = "RETAIN"
    REJECT_CONFLICT = "REJECT_CONFLICT"
    REJECT_UNSUPPORTED = "REJECT_UNSUPPORTED"
    REJECT_STALE = "REJECT_STALE"


class ProposalStatus(str, Enum):
    """Lifecycle status of the generated upgrade proposal."""
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INVALID_PATCH = "INVALID_PATCH"
    CONFLICT_DETECTED = "CONFLICT_DETECTED"
    NO_OP = "NO_OP"


class PlannedUpgradeItem(BaseModel):
    """A single dependency item within the upgrade plan."""
    package_name: str = Field(..., description="Normalized package name")
    ecosystem: Ecosystem = Field(..., description="Target ecosystem (python or npm)")
    current_version: Optional[str] = Field(default=None, description="Current version in manifest")
    target_version: Optional[str] = Field(default=None, description="Proposed upgrade target version")
    declared_specifier: str = Field(default="", description="Original constraint specifier from manifest")
    proposed_specifier: str = Field(default="", description="Proposed constraint specifier for manifest")
    action: UpgradeAction = Field(..., description="Planned action (UPGRADE, RETAIN, etc.)")
    rationale: str = Field(..., description="Reason for planned action or rejection")
    recommendation_ref: Optional[str] = Field(default=None, description="Reference ID or trace to M3 recommendation")
    line_number: Optional[int] = Field(default=None, description="1-based line number in original manifest")
    dependency_type: DependencyType = Field(default=DependencyType.RUNTIME, description="Runtime or dev dependency")
    is_conflict: bool = Field(default=False, description="True if this item has conflicting constraints")
    conflict_details: Optional[str] = Field(default=None, description="Details of any detected conflict")


class SelectedPackageUpgrade(BaseModel):
    """Explicit user selection of a package upgrade."""
    package_name: str = Field(..., description="Name of the package")
    candidate_version: Optional[str] = Field(default=None, description="Optional target version override")
    selected: bool = Field(default=True, description="Whether to include in upgrade plan")


class UpgradePlanRequest(BaseModel):
    """Request payload for constructing an upgrade plan and generating patch."""
    manifest_type: str = Field(..., description="Manifest format: 'requirements.txt' or 'package.json'")
    original_content: str = Field(..., description="Raw original manifest content")
    recommendations: List[PackageUpgradeRecommendation] = Field(..., description="Validated M3 recommendations")
    selected_packages: Optional[List[str]] = Field(
        default=None,
        description="List of package names selected for upgrade. If omitted, all packages with valid upgrade recommendations are planned.",
    )
    custom_selections: Optional[List[SelectedPackageUpgrade]] = Field(
        default=None,
        description="Fine-grained package selections with target versions",
    )
    file_path: Optional[str] = Field(
        default=None,
        description="Relative file path within repository (e.g. 'requirements.txt' or 'package.json')",
    )


class PatchIntegrityResult(BaseModel):
    """Integrity and validity report for a synthesized patch."""
    is_valid: bool = Field(..., description="Whether the patch passed all integrity and safety checks")
    can_apply: bool = Field(..., description="Whether the diff applies cleanly to the original manifest")
    can_reverse: bool = Field(..., description="Whether reversing the diff cleanly restores original manifest")
    syntax_valid: bool = Field(..., description="Whether proposed content parses as valid manifest syntax")
    target_versions_match: bool = Field(..., description="Whether all changed versions match validated recommendations")
    unrelated_dependencies_unchanged: bool = Field(..., description="Whether unselected/unrelated lines are intact")
    original_unchanged: bool = Field(..., description="Confirms original manifest was not mutated")
    checks_passed: List[str] = Field(default_factory=list, description="List of passed validation checks")
    checks_failed: List[str] = Field(default_factory=list, description="List of failed validation checks")
    errors: List[str] = Field(default_factory=list, description="Actionable error descriptions")
    warnings: List[str] = Field(default_factory=list, description="Non-fatal warnings or notices")


class UpgradeProposalResponse(BaseModel):
    """Complete, structured upgrade proposal containing plan, mutated content, diff, and integrity checks."""
    proposal_id: str = Field(default="", description="Server-generated unique identifier for proposal lifecycle tracking")
    manifest_type: str = Field(..., description="Source manifest type analyzed")
    ecosystem: Ecosystem = Field(..., description="Target ecosystem")
    file_path: str = Field(..., description="Target manifest path")
    plan_items: List[PlannedUpgradeItem] = Field(default_factory=list, description="Ordered dependency plan items")
    total_planned: int = Field(default=0, description="Count of dependencies to be upgraded")
    total_unchanged: int = Field(default=0, description="Count of dependencies retained unchanged")
    total_conflicts: int = Field(default=0, description="Count of conflicting recommendations detected")
    original_content: str = Field(..., description="Original manifest content byte-for-byte")
    proposed_content: str = Field(..., description="Synthesized proposed manifest content")
    unified_diff: str = Field(default="", description="Standard unified diff between original and proposed")
    integrity: PatchIntegrityResult = Field(..., description="Detailed patch integrity and safety verification")
    status: ProposalStatus = Field(default=ProposalStatus.READY_FOR_REVIEW, description="Overall proposal review status")
    requires_human_approval: bool = Field(default=True, description="Strict requirement for explicit human approval")
    diagnostics: List[str] = Field(default_factory=list, description="Diagnostics, warnings, and planning notices")
    summary: str = Field(default="", description="Executive summary of the upgrade proposal")


class WorkspaceApplyRequest(BaseModel):
    """Request payload to apply an approved proposal to a mutable workspace."""
    proposal_id: Optional[str] = Field(default=None, description="Server-generated unique identifier of the proposal to apply")
    approved: bool = Field(..., description="Explicit human confirmation signal (must be true)")
    approval_note: Optional[str] = Field(default=None, description="Optional note documenting user confirmation")
    target_path: Optional[str] = Field(
        default=None,
        description="Relative path to target manifest within authorized workspace root (e.g. 'requirements.txt')",
    )
    workspace_id: Optional[str] = Field(default=None, description="Optional workspace session ID")
    expected_sha256: Optional[str] = Field(default=None, description="Optional expected SHA-256 of original content for freshness check")
    proposal: Optional[UpgradeProposalResponse] = Field(
        default=None,
        description="Optional client-submitted proposal (never trusted over server-stored state)",
    )
    run_id: Optional[str] = Field(default=None, description="Optional analysis run ID")


class WorkspaceApplyResponse(BaseModel):
    """Result of attempting to apply an upgrade proposal to the workspace."""
    success: bool = Field(..., description="Whether the workspace mutation succeeded")
    status: str = Field(..., description="Result status: 'APPLIED', 'REJECTED_UNAPPROVED', 'VALIDATION_FAILED'")
    message: str = Field(..., description="Human-readable outcome message")
    applied_changes_count: int = Field(default=0, description="Number of modified dependencies applied")
    file_path: str = Field(..., description="Path to the modified manifest file")
    sha256: Optional[str] = Field(default=None, description="SHA-256 hash of the updated file")
    proposal_status: ProposalStatus = Field(..., description="Updated proposal status")

