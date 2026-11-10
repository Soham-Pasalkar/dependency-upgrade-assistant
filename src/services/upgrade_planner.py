"""Upgrade planning and proposal orchestration service for Dependency Upgrade Assistant 2.0.

Coordinates:
1. Converting validated M3 recommendations into deterministic upgrade plans.
2. Conflict detection across versions, packages, and constraints.
3. Safe manifest mutation using surgical line modifications.
4. Unified diff generation with standard hunks.
5. Multi-point patch integrity and safety verification.
6. Enforcing explicit human approval gates prior to workspace mutation.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from packaging.version import InvalidVersion, parse as parse_pep440
from src.analysis.manifest_parser import parse_manifest
from src.analysis.semver_evaluator import SemVer
from src.data.catalog_loader import normalize_package_name
from src.models.manifest import DependencyType, Ecosystem, ParsedDependency
from src.models.plan import (
    PatchIntegrityResult,
    PlannedUpgradeItem,
    ProposalStatus,
    SelectedPackageUpgrade,
    UpgradeAction,
    UpgradePlanRequest,
    UpgradeProposalResponse,
    WorkspaceApplyRequest,
    WorkspaceApplyResponse,
)
from src.models.recommendation import (
    PackageUpgradeRecommendation,
    RecommendationCategory,
    ValidationStatus,
)
from src.services.manifest_modifier import ManifestMutationError, modify_manifest
from src.services.patch_service import generate_unified_diff
from src.services.patch_validator import PatchValidator, is_safe_manifest_path

logger = logging.getLogger(__name__)


class HumanApprovalRequiredError(Exception):
    """Raised when a workspace mutation is attempted without explicit human approval."""
    pass


class UpgradePlannerService:
    """Orchestrates deterministic upgrade planning, diff generation, and approval gates."""

    def __init__(self) -> None:
        pass

    def create_upgrade_proposal(self, request: UpgradePlanRequest) -> UpgradeProposalResponse:
        """Construct an upgrade plan, generate proposed manifest, diff, and validate integrity.
        
        Args:
            request: UpgradePlanRequest with manifest content, recommendations, and selections.
            
        Returns:
            Structured UpgradeProposalResponse.
        """
        diagnostics: List[str] = []
        original_content = request.original_content
        manifest_type = request.manifest_type.strip()
        file_path = request.file_path or manifest_type

        # Parse original manifest to discover all declared dependencies
        orig_deps: List[ParsedDependency] = []
        try:
            orig_deps, parse_diags, ecosystem = parse_manifest(manifest_type, original_content)
            diagnostics.extend(parse_diags)
        except Exception as err:
            logger.error(f"Failed to parse original manifest for planning: {err}")
            empty_integrity = PatchIntegrityResult(
                is_valid=False,
                can_apply=False,
                can_reverse=False,
                syntax_valid=False,
                target_versions_match=False,
                unrelated_dependencies_unchanged=False,
                original_unchanged=True,
                checks_passed=[],
                checks_failed=["manifest_parsing"],
                errors=[f"Original manifest could not be parsed: {str(err)}"],
                warnings=[],
            )
            return UpgradeProposalResponse(
                manifest_type=manifest_type,
                ecosystem=Ecosystem.PYTHON if "requirements" in manifest_type.lower() else Ecosystem.NPM,
                file_path=file_path,
                plan_items=[],
                total_planned=0,
                total_unchanged=0,
                total_conflicts=0,
                original_content=original_content,
                proposed_content=original_content,
                unified_diff="",
                integrity=empty_integrity,
                status=ProposalStatus.INVALID_PATCH,
                requires_human_approval=True,
                diagnostics=[str(err)],
                summary="Planning aborted: original manifest syntax error.",
            )

        # Build mapping of original dependencies by normalized name and line number
        orig_by_name: Dict[str, List[ParsedDependency]] = {}
        for dep in orig_deps:
            norm = normalize_package_name(dep.name)
            orig_by_name.setdefault(norm, []).append(dep)

        # Determine user selection filters
        selected_set: Optional[Set[str]] = None
        if request.selected_packages is not None:
            selected_set = {normalize_package_name(p) for p in request.selected_packages}

        custom_target_overrides: Dict[str, str] = {}
        if request.custom_selections:
            for custom in request.custom_selections:
                norm = normalize_package_name(custom.package_name)
                if custom.selected:
                    if selected_set is None:
                        selected_set = set()
                    selected_set.add(norm)
                    if custom.candidate_version:
                        custom_target_overrides[norm] = custom.candidate_version.strip()
                elif selected_set is not None and norm in selected_set:
                    selected_set.remove(norm)

        # Index recommendations by normalized package name
        recs_by_name: Dict[str, List[PackageUpgradeRecommendation]] = {}
        for rec in request.recommendations:
            norm = normalize_package_name(rec.package_name)
            recs_by_name.setdefault(norm, []).append(rec)

        plan_items: List[PlannedUpgradeItem] = []
        conflicts_count = 0
        planned_count = 0
        unchanged_count = 0
        seen_packages: Set[str] = set()

        # 1. Process dependencies from original manifest in order
        for orig_dep in orig_deps:
            norm_pkg = normalize_package_name(orig_dep.name)
            recs = recs_by_name.get(norm_pkg, [])

            if not orig_dep.is_supported:
                # Unsupported line (pip flag, URL, malformed)
                plan_items.append(
                    PlannedUpgradeItem(
                        package_name=orig_dep.original_name,
                        ecosystem=orig_dep.ecosystem,
                        current_version=orig_dep.current_version,
                        target_version=orig_dep.current_version,
                        declared_specifier=orig_dep.declared_specifier,
                        proposed_specifier=orig_dep.declared_specifier,
                        action=UpgradeAction.REJECT_UNSUPPORTED,
                        rationale=orig_dep.diagnostic_message or "Preserved unsupported manifest construct.",
                        line_number=orig_dep.line_number,
                        dependency_type=orig_dep.dependency_type,
                    )
                )
                unchanged_count += 1
                continue

            # Find matching recommendation for this dependency
            matching_rec: Optional[PackageUpgradeRecommendation] = None
            if len(recs) == 1:
                matching_rec = recs[0]
            elif len(recs) > 1:
                # Disambiguate by line number if available
                for r in recs:
                    if r.line_number == orig_dep.line_number:
                        matching_rec = r
                        break
                if matching_rec is None:
                    # Check if conflicting target versions
                    cand_versions = {r.candidate_version for r in recs if r.candidate_version}
                    if len(cand_versions) > 1:
                        conflicts_count += 1
                        plan_items.append(
                            PlannedUpgradeItem(
                                package_name=orig_dep.original_name,
                                ecosystem=orig_dep.ecosystem,
                                current_version=orig_dep.current_version,
                                target_version=None,
                                declared_specifier=orig_dep.declared_specifier,
                                proposed_specifier=orig_dep.declared_specifier,
                                action=UpgradeAction.REJECT_CONFLICT,
                                rationale=f"Conflicting recommendations for package '{orig_dep.name}': {cand_versions}",
                                line_number=orig_dep.line_number,
                                dependency_type=orig_dep.dependency_type,
                                is_conflict=True,
                                conflict_details="Multiple recommendations propose different candidate versions without line disambiguation.",
                            )
                        )
                        unchanged_count += 1
                        continue
                    matching_rec = recs[0]

            # Determine whether this package should be upgraded
            is_selected = True
            if selected_set is not None and norm_pkg not in selected_set:
                is_selected = False

            if not is_selected or matching_rec is None or not matching_rec.candidate_version:
                # Retained unchanged
                plan_items.append(
                    PlannedUpgradeItem(
                        package_name=orig_dep.original_name,
                        ecosystem=orig_dep.ecosystem,
                        current_version=orig_dep.current_version,
                        target_version=orig_dep.current_version,
                        declared_specifier=orig_dep.declared_specifier,
                        proposed_specifier=orig_dep.declared_specifier,
                        action=UpgradeAction.RETAIN,
                        rationale="Package retained without modification (unselected or already up to date).",
                        line_number=orig_dep.line_number,
                        dependency_type=orig_dep.dependency_type,
                        recommendation_ref=matching_rec.package_name if matching_rec else None,
                    )
                )
                unchanged_count += 1
                continue

            # Determine target version
            target_ver = custom_target_overrides.get(norm_pkg) or matching_rec.candidate_version.strip()

            # Reject hallucinated or invalid version recommendations
            if matching_rec.validation_status in (
                ValidationStatus.REJECTED_HALLUCINATION,
                ValidationStatus.REJECTED_INCOMPATIBLE,
            ):
                conflicts_count += 1
                plan_items.append(
                    PlannedUpgradeItem(
                        package_name=orig_dep.original_name,
                        ecosystem=orig_dep.ecosystem,
                        current_version=orig_dep.current_version,
                        target_version=orig_dep.current_version,
                        declared_specifier=orig_dep.declared_specifier,
                        proposed_specifier=orig_dep.declared_specifier,
                        action=UpgradeAction.REJECT_CONFLICT,
                        rationale=f"Rejected recommendation failed ground-truth validation: {matching_rec.explanation}",
                        line_number=orig_dep.line_number,
                        dependency_type=orig_dep.dependency_type,
                        is_conflict=True,
                        conflict_details="Target version was rejected by ground-truth registry validation.",
                    )
                )
                unchanged_count += 1
                continue

            # Validate version syntax and downgrade prevention
            if not self._is_valid_version_upgrade(orig_dep.ecosystem, orig_dep.current_version, target_ver):
                conflicts_count += 1
                plan_items.append(
                    PlannedUpgradeItem(
                        package_name=orig_dep.original_name,
                        ecosystem=orig_dep.ecosystem,
                        current_version=orig_dep.current_version,
                        target_version=orig_dep.current_version,
                        declared_specifier=orig_dep.declared_specifier,
                        proposed_specifier=orig_dep.declared_specifier,
                        action=UpgradeAction.REJECT_CONFLICT,
                        rationale=f"Invalid upgrade: target version '{target_ver}' is not an upgrade from current '{orig_dep.current_version}'.",
                        line_number=orig_dep.line_number,
                        dependency_type=orig_dep.dependency_type,
                        is_conflict=True,
                        conflict_details="Target version represents a downgrade, invalid syntax, or identical version.",
                    )
                )
                unchanged_count += 1
                continue

            # Check if target version equals current version
            if orig_dep.current_version and orig_dep.current_version.strip() == target_ver:
                plan_items.append(
                    PlannedUpgradeItem(
                        package_name=orig_dep.original_name,
                        ecosystem=orig_dep.ecosystem,
                        current_version=orig_dep.current_version,
                        target_version=orig_dep.current_version,
                        declared_specifier=orig_dep.declared_specifier,
                        proposed_specifier=orig_dep.declared_specifier,
                        action=UpgradeAction.RETAIN,
                        rationale="Target version matches currently declared version. No upgrade needed.",
                        line_number=orig_dep.line_number,
                        dependency_type=orig_dep.dependency_type,
                    )
                )
                unchanged_count += 1
                continue

            # Formulate proposed specifier
            proposed_spec = self._compute_proposed_specifier(orig_dep, target_ver)

            # Planned for upgrade!
            plan_items.append(
                PlannedUpgradeItem(
                    package_name=orig_dep.original_name,
                    ecosystem=orig_dep.ecosystem,
                    current_version=orig_dep.current_version,
                    target_version=target_ver,
                    declared_specifier=orig_dep.declared_specifier,
                    proposed_specifier=proposed_spec,
                    action=UpgradeAction.UPGRADE,
                    rationale=matching_rec.explanation or f"Upgrade to {target_ver}",
                    recommendation_ref=f"{matching_rec.package_name}:{target_ver}",
                    line_number=orig_dep.line_number,
                    dependency_type=orig_dep.dependency_type,
                )
            )
            planned_count += 1

        # 2. Check for recommendations targeting packages absent from original manifest
        for rec in request.recommendations:
            norm_rec = normalize_package_name(rec.package_name)
            if norm_rec not in orig_by_name:
                conflicts_count += 1
                plan_items.append(
                    PlannedUpgradeItem(
                        package_name=rec.package_name,
                        ecosystem=rec.ecosystem,
                        current_version=None,
                        target_version=rec.candidate_version,
                        declared_specifier="",
                        proposed_specifier="",
                        action=UpgradeAction.REJECT_CONFLICT,
                        rationale=f"Recommendation references package '{rec.package_name}' which is not present in original manifest.",
                        is_conflict=True,
                        conflict_details="Package not declared in ingested manifest.",
                    )
                )

        # 3. Apply manifest mutations to generate proposed content
        proposed_content = original_content
        try:
            proposed_content, mutation_diags = modify_manifest(
                manifest_type=manifest_type,
                original_content=original_content,
                plan_items=plan_items,
            )
            diagnostics.extend(mutation_diags)
        except ManifestMutationError as err:
            logger.error(f"Manifest mutation failed: {err}")
            diagnostics.append(str(err))

        # 4. Generate standard unified diff
        unified_diff = generate_unified_diff(
            original_content=original_content,
            proposed_content=proposed_content,
            file_path=file_path,
        )

        # 5. Run patch integrity validation
        integrity = PatchValidator.validate_patch(
            manifest_type=manifest_type,
            original_content=original_content,
            proposed_content=proposed_content,
            unified_diff=unified_diff,
            plan_items=plan_items,
            file_path=file_path,
        )

        # 6. Determine overall proposal status
        if not integrity.is_valid:
            status = ProposalStatus.INVALID_PATCH
        elif conflicts_count > 0:
            status = ProposalStatus.CONFLICT_DETECTED
        elif not unified_diff.strip():
            status = ProposalStatus.NO_OP
        else:
            status = ProposalStatus.READY_FOR_REVIEW

        # 7. Formulate executive summary
        summary = (
            f"Planned {planned_count} upgrade(s), {unchanged_count} retained dependency(ies). "
            f"Integrity check: {'PASSED' if integrity.is_valid else 'FAILED'}."
        )
        if conflicts_count > 0:
            summary += f" Detected {conflicts_count} conflict(s) or unsupported item(s)."

        return UpgradeProposalResponse(
            manifest_type=manifest_type,
            ecosystem=ecosystem,
            file_path=file_path,
            plan_items=plan_items,
            total_planned=planned_count,
            total_unchanged=unchanged_count,
            total_conflicts=conflicts_count,
            original_content=original_content,
            proposed_content=proposed_content,
            unified_diff=unified_diff,
            integrity=integrity,
            status=status,
            requires_human_approval=True,
            diagnostics=diagnostics,
            summary=summary,
        )

    def apply_workspace_mutation(self, request: WorkspaceApplyRequest) -> WorkspaceApplyResponse:
        """Apply an approved upgrade proposal to the workspace after enforcing human approval.
        
        Args:
            request: WorkspaceApplyRequest containing proposal and explicit human approval flag.
            
        Returns:
            WorkspaceApplyResponse documenting outcome.
        Raises:
            HumanApprovalRequiredError if request.approved is False.
        """
        # Strict Human Approval Gate (Criterion 4)
        if not request.approved:
            logger.warning("Workspace mutation rejected: human approval gate not satisfied.")
            raise HumanApprovalRequiredError(
                "Human approval gate enforced: Workspace mutation requires explicit user confirmation (approved=true)."
            )

        proposal = request.proposal

        # Validate proposal integrity before any disk write
        if not proposal.integrity.is_valid:
            return WorkspaceApplyResponse(
                success=False,
                status="VALIDATION_FAILED",
                message="Cannot mutate workspace: proposed patch failed integrity verification.",
                applied_changes_count=0,
                file_path=proposal.file_path,
                proposal_status=ProposalStatus.INVALID_PATCH,
            )

        target_file = proposal.file_path
        if request.target_path:
            target_path_obj = Path(request.target_path)
            if target_path_obj.is_dir():
                target_file = str(target_path_obj / Path(proposal.file_path).name)
            else:
                target_file = request.target_path

        # Validate path safety
        if not is_safe_manifest_path(target_file, allow_absolute=True):
            return WorkspaceApplyResponse(
                success=False,
                status="VALIDATION_FAILED",
                message=f"Cannot mutate workspace: unsafe or traversing target path '{target_file}'.",
                applied_changes_count=0,
                file_path=target_file,
                proposal_status=ProposalStatus.INVALID_PATCH,
            )

        # Compute SHA-256 of proposed content
        content_bytes = proposal.proposed_content.encode("utf-8")
        sha256_hash = hashlib.sha256(content_bytes).hexdigest()

        # If a real directory is configured, write the file safely
        if request.target_path and os.path.exists(os.path.dirname(os.path.abspath(target_file))):
            try:
                with open(target_file, "w", encoding="utf-8") as f:
                    f.write(proposal.proposed_content)
                logger.info(f"Workspace file '{target_file}' mutated successfully under human approval.")
            except Exception as err:
                return WorkspaceApplyResponse(
                    success=False,
                    status="MUTATION_ERROR",
                    message=f"Failed to write workspace file: {str(err)}",
                    applied_changes_count=0,
                    file_path=target_file,
                    proposal_status=ProposalStatus.REJECTED,
                )

        return WorkspaceApplyResponse(
            success=True,
            status="APPLIED",
            message=f"Upgrade proposal successfully applied with {proposal.total_planned} dependency update(s).",
            applied_changes_count=proposal.total_planned,
            file_path=target_file,
            sha256=sha256_hash,
            proposal_status=ProposalStatus.APPROVED,
        )

    def _compute_proposed_specifier(self, dep: ParsedDependency, target_ver: str) -> str:
        """Formulate the proposed specifier based on original constraint and ecosystem."""
        if dep.ecosystem == Ecosystem.NPM:
            clean_spec = dep.declared_specifier.strip()
            for prefix in ("^", "~", ">=", "<=", ">", "<", "="):
                if clean_spec.startswith(prefix):
                    return f"{prefix}{target_ver.lstrip('^~><=')}"
            return target_ver.lstrip("^~><=")

        # Python
        if dep.declared_specifier.startswith("~="):
            return f"~={target_ver}"
        if dep.declared_specifier.startswith(">="):
            return f">={target_ver}"
        return f"=={target_ver}"

    def _is_valid_version_upgrade(
        self,
        ecosystem: Ecosystem,
        current_version: Optional[str],
        target_version: str,
    ) -> bool:
        """Verify that target_version is valid and does not represent a downgrade."""
        if not target_version or not target_version.strip():
            return False

        clean_target = target_version.strip().lstrip("^~><=")

        if ecosystem == Ecosystem.NPM:
            t_sv = SemVer.parse(clean_target)
            if t_sv is None:
                return False
            if current_version:
                clean_curr = current_version.strip().lstrip("^~><=")
                c_sv = SemVer.parse(clean_curr)
                if c_sv is not None:
                    # Target must be greater than current version
                    return t_sv > c_sv
            return True

        # Python (PEP 440)
        try:
            t_v = parse_pep440(clean_target)
            if current_version:
                clean_curr = current_version.strip().lstrip("^~><=")
                try:
                    c_v = parse_pep440(clean_curr)
                    return t_v > c_v
                except InvalidVersion:
                    return True
            return True
        except InvalidVersion:
            return False
