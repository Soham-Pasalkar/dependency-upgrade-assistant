"""Upgrade planning and proposal orchestration service for Dependency Upgrade Assistant 2.0.

Coordinates:
1. Converting validated M3 recommendations into deterministic upgrade plans.
2. Conflict detection across versions, packages, and constraints.
3. Safe manifest mutation using surgical line modifications.
4. Unified diff generation with standard hunks.
5. Multi-point patch integrity and safety verification.
6. Enforcing explicit human approval gates prior to workspace mutation.
7. Server-side proposal lifecycle registry preventing forgery.
8. Server-controlled workspace boundary enforcement with atomic writes.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from packaging.version import InvalidVersion, parse as parse_pep440
from src.analysis.manifest_parser import parse_manifest
from src.analysis.semver_evaluator import SemVer
from src.api.config import settings
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
from src.services.patch_validator import (
    PatchValidator,
    is_safe_manifest_path,
    validate_workspace_destination,
)
from src.services.proposal_registry import ProposalRegistry, proposal_registry as default_proposal_registry

logger = logging.getLogger(__name__)

# Only validation statuses established as safe by M3 ground-truth evaluation
TRUSTED_VALIDATION_STATUSES: Set[ValidationStatus] = {
    ValidationStatus.VALIDATED,
    ValidationStatus.FALLBACK_DETERMINISTIC,
}


class HumanApprovalRequiredError(Exception):
    """Raised when a workspace mutation is attempted without explicit human approval."""
    pass


class StaleManifestError(Exception):
    """Raised when on-disk manifest content does not match the proposal's original content."""
    pass


class UpgradePlannerService:
    """Orchestrates deterministic upgrade planning, diff generation, and approval gates."""

    def __init__(
        self,
        workspace_root: Optional[Path | str] = None,
        registry: Optional[ProposalRegistry] = None,
    ) -> None:
        if workspace_root:
            self.workspace_root = Path(workspace_root).resolve()
        else:
            self.workspace_root = settings.resolved_workspace_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.proposal_registry = registry or default_proposal_registry

    def create_upgrade_proposal(self, request: UpgradePlanRequest) -> UpgradeProposalResponse:
        """Construct an upgrade plan, generate proposed manifest, diff, and validate integrity.
        
        Args:
            request: UpgradePlanRequest with manifest content, recommendations, and selections.
            
        Returns:
            Structured UpgradeProposalResponse stored in server proposal registry.
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
            failed_proposal = UpgradeProposalResponse(
                proposal_id="",
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
            # Register in server store for lifecycle consistency
            self.proposal_registry.store(failed_proposal)
            return failed_proposal

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

            # Determine whether this package was selected by the caller
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

            # -------------------------------------------------------------
            # Recommendation Provenance & Validation Enforcement
            # -------------------------------------------------------------

            # A. Ecosystem match verification
            if matching_rec.ecosystem != orig_dep.ecosystem:
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
                        rationale=f"Ecosystem mismatch: recommendation ecosystem '{matching_rec.ecosystem}' does not match manifest ecosystem '{orig_dep.ecosystem}'.",
                        line_number=orig_dep.line_number,
                        dependency_type=orig_dep.dependency_type,
                        is_conflict=True,
                        conflict_details="Ecosystem mismatch between manifest and recommendation.",
                    )
                )
                unchanged_count += 1
                continue

            # B. Trusted ValidationStatus enforcement
            if matching_rec.validation_status not in TRUSTED_VALIDATION_STATUSES:
                conflicts_count += 1
                if matching_rec.validation_status == ValidationStatus.REJECTED_HALLUCINATION:
                    rationale = f"Rejected recommendation: candidate version was flagged as hallucinated ({matching_rec.explanation})"
                    details = "Target version was rejected as hallucination not found in registry ground truth."
                elif matching_rec.validation_status == ValidationStatus.REJECTED_INCOMPATIBLE:
                    rationale = f"Rejected recommendation: candidate version is incompatible with manifest constraint ({matching_rec.explanation})"
                    details = "Target version was rejected by ground-truth registry validation as incompatible."
                elif matching_rec.validation_status == ValidationStatus.SKIPPED:
                    rationale = f"Rejected recommendation: registry validation status was SKIPPED; candidate is unverified ({matching_rec.explanation})"
                    details = "Candidate has SKIPPED validation status and cannot be safely upgraded."
                else:
                    rationale = f"Rejected recommendation: untrusted validation status '{matching_rec.validation_status}' ({matching_rec.explanation})"
                    details = "Recommendation validation status is not established as safe."

                plan_items.append(
                    PlannedUpgradeItem(
                        package_name=orig_dep.original_name,
                        ecosystem=orig_dep.ecosystem,
                        current_version=orig_dep.current_version,
                        target_version=orig_dep.current_version,
                        declared_specifier=orig_dep.declared_specifier,
                        proposed_specifier=orig_dep.declared_specifier,
                        action=UpgradeAction.REJECT_CONFLICT,
                        rationale=rationale,
                        line_number=orig_dep.line_number,
                        dependency_type=orig_dep.dependency_type,
                        is_conflict=True,
                        conflict_details=details,
                    )
                )
                unchanged_count += 1
                continue

            # C. Custom target version validation: prohibited from arbitrary override
            validated_candidate = matching_rec.candidate_version.strip()
            custom_override = custom_target_overrides.get(norm_pkg)
            if custom_override is not None:
                clean_custom = custom_override.strip().lstrip("^~><=")
                clean_validated = validated_candidate.lstrip("^~><=")
                if clean_custom != clean_validated:
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
                            rationale=f"Arbitrary custom target version '{custom_override}' rejected: custom target must match explicitly validated M3 candidate '{validated_candidate}'.",
                            line_number=orig_dep.line_number,
                            dependency_type=orig_dep.dependency_type,
                            is_conflict=True,
                            conflict_details="Custom candidate version does not match registry-validated candidate.",
                        )
                    )
                    unchanged_count += 1
                    continue

            target_ver = validated_candidate

            # D. Validate version syntax and downgrade prevention
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

        proposal = UpgradeProposalResponse(
            proposal_id="",
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

        # Store proposal in server-controlled registry
        self.proposal_registry.store(proposal)
        return proposal

    def apply_workspace_mutation(self, request: WorkspaceApplyRequest) -> WorkspaceApplyResponse:
        """Apply an approved upgrade proposal to a workspace file atomically and safely.

        Concurrency & Lifecycle Guarantees:
        1. Human approval gate: verified before claiming (approved=true).
        2. Atomic proposal claim: transitions AVAILABLE -> APPLYING under registry lock.
           Competing concurrent requests receive PROPOSAL_ALREADY_IN_PROGRESS or PROPOSAL_ALREADY_CONSUMED.
        3. Pre-write validation:
           - Server-side patch re-validation
           - Workspace boundary resolution and containment
           - Destination file existence and regular file check
           - Manifest freshness check (disk content matches proposal.original_content)
           - Optional expected_sha256 verification
           If any pre-write check fails, release_application_claim() returns proposal to AVAILABLE.
        4. Atomic filesystem replacement:
           Writes to temporary file in target directory, flushes, fsyncs, preserves permissions,
           and atomically renames (os.replace).
           If write fails, fail_application() marks proposal ABANDONED (fail closed, no replay).
        5. Post-write verification & completion:
           Reads back written content to ensure byte-level match, then complete_application()
           transitions APPLYING -> CONSUMED. Returns APPLIED with SHA-256.

        Trust Boundary Note:
        The current workspace mutation security model assumes a single-tenant or container-isolated
        execution environment where concurrent local filesystem actors do not have write access
        to the workspace directory structure. Mitigations against sub-millisecond local symlink-swap
        race conditions (TOCTOU) between resolution and file replacement rely on workspace boundary
        containment and filesystem permissions rather than kernel-level openat(O_NOFOLLOW) descriptor trees.
        """
        # 1. Human Approval Gate
        if not request.approved:
            logger.warning("Workspace mutation rejected: human approval gate not satisfied.")
            raise HumanApprovalRequiredError(
                "Human approval gate enforced: Workspace mutation requires explicit confirmation (approved=true)."
            )

        # 2. Extract authoritative proposal ID
        prop_id = request.proposal_id or (request.proposal.proposal_id if request.proposal else None)
        if not prop_id:
            logger.warning("Workspace mutation rejected: missing proposal_id.")
            return WorkspaceApplyResponse(
                success=False,
                status="PROPOSAL_NOT_FOUND",
                message="Missing server proposal_id. Unregistered or forged proposals cannot be applied.",
                applied_changes_count=0,
                file_path=request.target_path or "",
                proposal_status=ProposalStatus.REJECTED,
            )

        # 3. Atomically claim proposal for application (transitions AVAILABLE -> APPLYING)
        claim = self.proposal_registry.claim_for_application(prop_id)
        if not claim.success or claim.stored is None:
            logger.warning(f"Workspace mutation rejected: cannot claim proposal '{prop_id}': {claim.status}")
            return WorkspaceApplyResponse(
                success=False,
                status=claim.status,
                message=claim.message,
                applied_changes_count=0,
                file_path=request.target_path or "",
                proposal_status=ProposalStatus.REJECTED,
            )

        stored = claim.stored
        proposal = stored.proposal

        # Helper to safely release claim when pre-write checks fail
        def _abort_pre_write(status: str, message: str, file_path: str = "", prop_status: ProposalStatus = ProposalStatus.REJECTED) -> WorkspaceApplyResponse:
            self.proposal_registry.release_application_claim(prop_id)
            return WorkspaceApplyResponse(
                success=False,
                status=status,
                message=message,
                applied_changes_count=0,
                file_path=file_path,
                proposal_status=prop_status,
            )

        # 4. Independent server-side patch re-validation (do NOT trust client flags)
        recomputed_integrity = PatchValidator.validate_patch(
            manifest_type=proposal.manifest_type,
            original_content=proposal.original_content,
            proposed_content=proposal.proposed_content,
            unified_diff=proposal.unified_diff,
            plan_items=proposal.plan_items,
            file_path=proposal.file_path,
        )
        if not recomputed_integrity.is_valid:
            logger.error(f"Workspace mutation aborted: server-side patch re-validation failed for '{prop_id}'.")
            return _abort_pre_write(
                status="VALIDATION_FAILED",
                message=f"Cannot mutate workspace: server-side patch re-validation failed ({'; '.join(recomputed_integrity.errors)}).",
                file_path=proposal.file_path,
                prop_status=ProposalStatus.INVALID_PATCH,
            )

        # 5. Resolve effective workspace boundary
        effective_root = self.workspace_root
        if request.workspace_id:
            ws_id = request.workspace_id.strip()
            if ".." in ws_id or "\0" in ws_id or ws_id.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", ws_id):
                return _abort_pre_write(
                    status="INVALID_PATH",
                    message=f"Illegal workspace ID format: '{ws_id}'",
                )
            effective_root = (self.workspace_root / ws_id).resolve()
            if not (self.workspace_root in effective_root.parents or self.workspace_root == effective_root):
                return _abort_pre_write(
                    status="INVALID_PATH",
                    message=f"Workspace ID '{ws_id}' escapes server workspace root.",
                )

        target_rel = request.target_path if request.target_path is not None else proposal.file_path
        if not target_rel or not target_rel.strip():
            return _abort_pre_write(
                status="NOT_APPLIED",
                message="No target workspace path specified for mutation.",
            )

        # 6. Path safety & workspace containment validation
        try:
            resolved_file = validate_workspace_destination(
                target_rel_path=target_rel,
                workspace_root=effective_root,
                expected_filename=proposal.file_path,
            )
        except ValueError as err:
            logger.warning(f"Path safety violation during workspace mutation: {err}")
            return _abort_pre_write(
                status="INVALID_PATH",
                message=f"Path safety violation: {str(err)}",
                file_path=target_rel,
            )

        # 7. Verify destination file exists and is a regular file
        if not resolved_file.exists():
            return _abort_pre_write(
                status="TARGET_NOT_FOUND",
                message=f"Target manifest file does not exist: '{resolved_file.name}'",
                file_path=str(resolved_file),
            )

        if not resolved_file.is_file() or resolved_file.is_symlink():
            return _abort_pre_write(
                status="INVALID_PATH",
                message=f"Target destination '{resolved_file.name}' is not a regular file or is a symlink.",
                file_path=str(resolved_file),
            )

        # 8. Concurrency / Freshness verification: disk content must match proposal base
        current_disk_content = resolved_file.read_text(encoding="utf-8")
        if current_disk_content != proposal.original_content:
            logger.warning(f"Stale proposal detected for '{resolved_file}': on-disk content has changed.")
            return _abort_pre_write(
                status="STALE_BASE_CONTENT",
                message="Destination file has been modified since the upgrade proposal was generated. Aborting mutation to prevent overwriting concurrent changes.",
                file_path=str(resolved_file),
            )

        # Check expected SHA-256 if supplied by caller
        if request.expected_sha256:
            disk_sha = hashlib.sha256(current_disk_content.encode("utf-8")).hexdigest()
            if disk_sha.lower() != request.expected_sha256.lower():
                return _abort_pre_write(
                    status="STALE_BASE_CONTENT",
                    message="Destination file checksum does not match expected SHA-256.",
                    file_path=str(resolved_file),
                )

        # 9. Safe atomic file replacement
        # Explicitly distinguish pre-replacement from post-replacement failures
        mutation_attempted = False
        temp_file = resolved_file.with_name(f".tmp_{resolved_file.name}_{uuid.uuid4().hex[:8]}")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(proposal.proposed_content)
                f.flush()
                os.fsync(f.fileno())

            # Preserve file permissions if possible
            try:
                shutil.copymode(resolved_file, temp_file)
            except Exception:
                pass

            # Point of replacement: once os.replace is initiated, filesystem state may be modified
            mutation_attempted = True
            os.replace(temp_file, resolved_file)
            logger.info(f"Workspace file '{resolved_file}' mutated successfully via atomic replacement.")
        except Exception as err:
            logger.exception(f"Error during workspace file write/replacement: {err}")
            if mutation_attempted:
                # Replacement was initiated; fail closed (ABANDONED) to prevent unsafe replay
                self.proposal_registry.fail_application(prop_id, reason=f"Filesystem replacement failure: {str(err)}")
            else:
                # Failed before destination was touched; safe to release claim back to AVAILABLE
                self.proposal_registry.release_application_claim(prop_id)

            return WorkspaceApplyResponse(
                success=False,
                status="MUTATION_ERROR",
                message=f"Failed to write workspace file: {str(err)}",
                applied_changes_count=0,
                file_path=str(resolved_file),
                proposal_status=ProposalStatus.REJECTED,
            )
        finally:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass

        # 10. Post-write verification & completion protected block
        # Once os.replace() has executed, the destination has been replaced on disk.
        # Any failure here MUST fail closed (transition to ABANDONED) and never release back to AVAILABLE.
        try:
            post_write_content = resolved_file.read_text(encoding="utf-8")
            if post_write_content != proposal.proposed_content:
                logger.critical(f"Post-write verification failed for '{resolved_file}': content mismatch!")
                self.proposal_registry.fail_application(prop_id, reason="Post-write content mismatch")
                return WorkspaceApplyResponse(
                    success=False,
                    status="MUTATION_ERROR",
                    message="Post-write verification failed: on-disk content does not match proposed content.",
                    applied_changes_count=0,
                    file_path=str(resolved_file),
                    proposal_status=ProposalStatus.REJECTED,
                )

            # 11. Complete proposal application (mandatory transition APPLYING -> CONSUMED)
            completed = self.proposal_registry.complete_application(prop_id)
            if not completed:
                logger.critical(f"Lifecycle integrity failure: could not transition proposal '{prop_id}' to CONSUMED.")
                self.proposal_registry.fail_application(prop_id, reason="Registry completion transition failed")
                return WorkspaceApplyResponse(
                    success=False,
                    status="LIFECYCLE_ERROR",
                    message="Workspace file was replaced, but registry lifecycle completion failed. Failing closed to prevent replay.",
                    applied_changes_count=0,
                    file_path=str(resolved_file),
                    proposal_status=ProposalStatus.REJECTED,
                )

            content_bytes = proposal.proposed_content.encode("utf-8")
            sha256_hash = hashlib.sha256(content_bytes).hexdigest()

            return WorkspaceApplyResponse(
                success=True,
                status="APPLIED",
                message=f"Upgrade proposal successfully applied with {proposal.total_planned} dependency update(s).",
                applied_changes_count=proposal.total_planned,
                file_path=str(resolved_file),
                sha256=sha256_hash,
                proposal_status=ProposalStatus.APPROVED,
            )
        except Exception as err:
            logger.exception(f"Unexpected error during post-write verification or completion: {err}")
            self.proposal_registry.fail_application(prop_id, reason=f"Post-write exception: {str(err)}")
            return WorkspaceApplyResponse(
                success=False,
                status="MUTATION_ERROR",
                message=f"Post-write verification failed due to unexpected error: {str(err)}",
                applied_changes_count=0,
                file_path=str(resolved_file),
                proposal_status=ProposalStatus.REJECTED,
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
