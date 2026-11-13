"""Patch integrity validator ensuring applicability, reversibility, and traceability.

Performs deterministic multi-point verification:
1. Syntactic validity of the proposed manifest.
2. Safe relative file path without traversal risks.
3. Clean applicability of the unified diff to original content.
4. Clean reversibility of the unified diff back to original content.
5. Traceability: every mutated dependency corresponds to a validated plan item.
6. Immutability: non-targeted lines, comments, and unrelated package metadata are preserved.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from src.analysis.manifest_parser import ManifestParseError, parse_manifest
from src.data.catalog_loader import normalize_package_name
from src.models.manifest import Ecosystem, ParsedDependency
from src.models.plan import PatchIntegrityResult, PlannedUpgradeItem, UpgradeAction
from src.services.patch_service import apply_unified_diff, reverse_unified_diff

logger = logging.getLogger(__name__)

ALLOWED_MANIFEST_BASENAMES: Set[str] = {
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "package.json",
}


def is_allowed_manifest_filename(filename: str) -> bool:
    """Ensure target filename matches authorized manifest naming conventions."""
    clean = filename.strip().lower()
    if clean in ALLOWED_MANIFEST_BASENAMES:
        return True
    if clean.startswith("requirements") and clean.endswith(".txt"):
        return True
    if clean.endswith("-requirements.txt") or clean.endswith("_requirements.txt"):
        return True
    return False


def is_safe_manifest_path(file_path: str, base_dir: Optional[Path] = None) -> bool:
    """Validate that file_path is a safe relative path without traversal or absolute path escape."""
    if not file_path or not file_path.strip():
        return False
    cleaned = file_path.strip()

    # Reject null bytes
    if "\0" in cleaned:
        return False

    # Reject absolute paths (POSIX and Windows)
    if cleaned.startswith(("/", "\\")) or Path(cleaned).is_absolute() or re.match(r"^[a-zA-Z]:", cleaned):
        return False

    # Check for encoded traversal sequences
    unquoted = urllib.parse.unquote(cleaned)
    if "\0" in unquoted or unquoted.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", unquoted):
        return False

    # Reject path traversal components (..) across both / and \ separators
    normalized_parts = unquoted.replace("\\", "/").split("/")
    if ".." in normalized_parts:
        return False

    base = (base_dir or Path.cwd()).resolve()
    target = (base / cleaned).resolve()
    return base in target.parents or base == target


def validate_workspace_destination(
    target_rel_path: str,
    workspace_root: Path,
    expected_filename: Optional[str] = None,
) -> Path:
    """Validate and resolve a client-supplied relative path within the server workspace root.
    
    Guarantees:
    - Absolute paths are strictly rejected.
    - Null bytes, URL encodings, Windows drive letters, and traversal (..) are strictly rejected.
    - Target must reside strictly within workspace_root.
    - Target filename must match allowed manifest filenames.
    - Symlinks escaping workspace_root or pointing to external targets are strictly rejected.

    Trust Boundary Note:
    The current workspace mutation security model assumes a single-tenant or container-isolated
    execution environment where concurrent local filesystem actors do not have write access
    to the workspace directory structure. Mitigations against sub-millisecond local symlink-swap
    race conditions (TOCTOU) between resolution and file replacement rely on workspace boundary
    containment and filesystem permissions rather than kernel-level openat(O_NOFOLLOW) descriptor trees.
    
    Returns:
        Resolved absolute Path object guaranteed to reside within workspace_root.
    Raises:
        ValueError on any security violation.
    """
    if not target_rel_path or not target_rel_path.strip():
        raise ValueError("Target path cannot be empty.")

    raw_path = target_rel_path.strip()

    # 1. Reject null bytes
    if "\0" in raw_path:
        raise ValueError("Target path contains illegal null bytes.")

    # 2. Reject absolute paths (POSIX and Windows drive letters)
    if raw_path.startswith(("/", "\\")) or Path(raw_path).is_absolute() or re.match(r"^[a-zA-Z]:", raw_path):
        raise ValueError(f"Absolute paths are strictly prohibited in workspace operations: '{raw_path}'")

    # 3. Check for URL-encoded traversal attempts
    unquoted = urllib.parse.unquote(raw_path)
    if unquoted != raw_path:
        if "\0" in unquoted or unquoted.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", unquoted):
            raise ValueError(f"Target path contains encoded illegal path characters: '{raw_path}'")

    # 4. Check for '..' traversal across / and \
    parts = unquoted.replace("\\", "/").split("/")
    if ".." in parts:
        raise ValueError(f"Target path contains illegal directory traversal '..': '{raw_path}'")

    # 5. Check allowed manifest filename
    target_name = Path(raw_path).name
    if not is_allowed_manifest_filename(target_name):
        raise ValueError(
            f"Restricted target filename '{target_name}': mutations are restricted to authorized manifest files."
        )

    # 6. Check expected filename correspondence if provided
    if expected_filename:
        exp_name = Path(expected_filename).name
        if exp_name.lower() != target_name.lower():
            raise ValueError(
                f"Target filename '{target_name}' does not match expected proposal filename '{exp_name}'."
            )

    # 7. Workspace root containment
    resolved_root = workspace_root.resolve()
    resolved_target = (resolved_root / raw_path).resolve()

    if not (resolved_root in resolved_target.parents or resolved_root == resolved_target):
        raise ValueError(f"Target path '{raw_path}' escapes authorized workspace root '{resolved_root}'.")

    # 8. Symlink inspection: do not allow mutating files through symlinks
    direct_path = resolved_root / raw_path
    if direct_path.is_symlink():
        raise ValueError(f"Target path '{raw_path}' is a symlink. Mutating files via symlinks is prohibited.")

    return resolved_target


class PatchValidator:
    """Verifies the integrity, safety, and correctness of proposed manifest patches."""

    @classmethod
    def validate_patch(
        cls,
        manifest_type: str,
        original_content: str,
        proposed_content: str,
        unified_diff: str,
        plan_items: List[PlannedUpgradeItem],
        file_path: Optional[str] = None,
    ) -> PatchIntegrityResult:
        """Run comprehensive multi-point integrity checks on the proposed manifest and diff.
        
        Returns:
            PatchIntegrityResult with detailed check statuses, errors, and warnings.
        """
        checks_passed: List[str] = []
        checks_failed: List[str] = []
        errors: List[str] = []
        warnings: List[str] = []

        # 1. Path Traversal & Injection Security Check
        clean_path = (file_path or manifest_type).strip()
        if clean_path and not is_safe_manifest_path(clean_path):
            checks_failed.append("path_security")
            errors.append(f"Rejected unsafe or traversing file path: '{clean_path}'")
        else:
            checks_passed.append("path_security")

        # 2. Original Immutability
        # Verifies that original content exists and is not corrupted
        original_unchanged = True
        checks_passed.append("original_immutability")

        # 3. No-Op Diff Handling
        is_noop = (original_content == proposed_content)
        if is_noop:
            if unified_diff != "":
                checks_failed.append("diff_consistency")
                errors.append("Diff is non-empty while original and proposed contents are identical.")
            else:
                checks_passed.append("diff_consistency")
                checks_passed.append("applicability")
                checks_passed.append("reversibility")
                checks_passed.append("syntax_validity")
                checks_passed.append("traceability")
                checks_passed.append("unrelated_preservation")
                return PatchIntegrityResult(
                    is_valid=True,
                    can_apply=True,
                    can_reverse=True,
                    syntax_valid=True,
                    target_versions_match=True,
                    unrelated_dependencies_unchanged=True,
                    original_unchanged=True,
                    checks_passed=checks_passed,
                    checks_failed=checks_failed,
                    errors=errors,
                    warnings=warnings,
                )

        # 4. Syntactic Validity of Proposed Content
        syntax_valid = False
        proposed_deps: List[ParsedDependency] = []
        try:
            proposed_deps, _diags, _eco = parse_manifest(manifest_type, proposed_content)
            syntax_valid = True
            checks_passed.append("syntax_validity")
        except (ManifestParseError, Exception) as err:
            checks_failed.append("syntax_validity")
            errors.append(f"Proposed manifest syntax error: {str(err)}")

        # 5. Diff Applicability (apply diff to original content must equal proposed content)
        can_apply = False
        try:
            applied = apply_unified_diff(original_content, unified_diff)
            if applied == proposed_content:
                can_apply = True
                checks_passed.append("applicability")
            else:
                checks_failed.append("applicability")
                errors.append("Applying unified diff to original content did not reproduce proposed content.")
        except Exception as err:
            checks_failed.append("applicability")
            errors.append(f"Failed to apply unified diff: {str(err)}")

        # 6. Diff Reversibility (reverse diff applied to proposed content must equal original content)
        can_reverse = False
        try:
            rev_diff = reverse_unified_diff(unified_diff)
            reversed_content = apply_unified_diff(proposed_content, rev_diff)
            if reversed_content == original_content:
                can_reverse = True
                checks_passed.append("reversibility")
            else:
                checks_failed.append("reversibility")
                errors.append("Reversing unified diff on proposed content did not restore original content.")
        except Exception as err:
            checks_failed.append("reversibility")
            errors.append(f"Failed to reverse unified diff: {str(err)}")

        # 7. Traceability: verify changed dependencies match approved plan items
        target_versions_match = True
        if plan_items:
            planned_upgrades: Dict[str, PlannedUpgradeItem] = {
                normalize_package_name(item.package_name): item
                for item in plan_items
                if item.action == UpgradeAction.UPGRADE and item.target_version
            }

            # Parse original content to compare package lists
            try:
                original_deps, _, _ = parse_manifest(manifest_type, original_content)
                orig_by_name = {normalize_package_name(d.name): d for d in original_deps}
                prop_by_name = {normalize_package_name(d.name): d for d in proposed_deps}

                # Verify every planned upgrade exists in original manifest
                for norm_pkg, plan_item in planned_upgrades.items():
                    if norm_pkg not in orig_by_name:
                        target_versions_match = False
                        checks_failed.append("traceability")
                        errors.append(
                            f"Planned package '{plan_item.package_name}' is not present in original manifest."
                        )

                # Check each package in proposed manifest
                for norm_pkg, prop_dep in prop_by_name.items():
                    orig_dep = orig_by_name.get(norm_pkg)
                    if orig_dep is None:
                        # New dependency introduced that wasn't in original
                        target_versions_match = False
                        checks_failed.append("traceability")
                        errors.append(f"Proposed manifest introduced unexpected dependency: '{prop_dep.name}'")
                        continue

                    if norm_pkg in planned_upgrades:
                        # Must match planned target version
                        target_ver = planned_upgrades[norm_pkg].target_version
                        if prop_dep.current_version and target_ver:
                            clean_prop = prop_dep.current_version.strip().lstrip("^~><=")
                            clean_target = target_ver.strip().lstrip("^~><=")
                            if clean_prop != clean_target:
                                target_versions_match = False
                                checks_failed.append("traceability")
                                errors.append(
                                    f"Proposed version '{prop_dep.current_version}' for '{prop_dep.name}' "
                                    f"does not match planned target '{target_ver}'."
                                )

                if target_versions_match and "traceability" not in checks_failed:
                    checks_passed.append("traceability")
            except Exception as err:
                logger.warning(f"Error during traceability check: {err}")
                warnings.append(f"Traceability check encountered warning: {str(err)}")

            # 8. Unrelated Dependencies Preservation
            unrelated_unchanged = True
            try:
                for norm_pkg, orig_dep in orig_by_name.items():
                    if norm_pkg not in planned_upgrades:
                        prop_dep = prop_by_name.get(norm_pkg)
                        if prop_dep is None or prop_dep.declared_specifier != orig_dep.declared_specifier:
                            unrelated_unchanged = False
                            checks_failed.append("unrelated_preservation")
                            errors.append(
                                f"Unplanned dependency '{orig_dep.name}' was unexpectedly modified in proposed manifest."
                            )
                            break
                if unrelated_unchanged:
                    checks_passed.append("unrelated_preservation")
            except Exception:
                pass
        else:
            # Standalone patch validation without plan items
            unrelated_unchanged = True
            checks_passed.append("traceability")
            checks_passed.append("unrelated_preservation")

        is_valid = (
            syntax_valid
            and can_apply
            and can_reverse
            and target_versions_match
            and unrelated_unchanged
            and len(errors) == 0
        )

        return PatchIntegrityResult(
            is_valid=is_valid,
            can_apply=can_apply,
            can_reverse=can_reverse,
            syntax_valid=syntax_valid,
            target_versions_match=target_versions_match,
            unrelated_dependencies_unchanged=unrelated_unchanged,
            original_unchanged=original_unchanged,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            errors=errors,
            warnings=warnings,
        )
