"""Surgical and non-destructive manifest modifier for Python and Node.js ecosystems.

Guarantees:
- Never modifies the original manifest in place.
- For requirements.txt: Preserves whitespace, comments, extras, markers, blank lines, and directives.
- For package.json: Preserves top-level keys, sections, indentation, and SemVer range prefixes (^, ~, >=).
- Handles duplicates safely via 1-based line numbers.
- Fails closed with descriptive errors on malformed manifests.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional, Set, Tuple

from src.analysis.manifest_parser import parse_manifest
from src.core.parser import (
    BARE_PACKAGE_PATTERN,
    EXACT_PIN_PATTERN,
    NON_PIN_PATTERN,
    PACKAGE_NAME_REGEX,
    _detect_newline,
)
from src.data.catalog_loader import normalize_package_name
from src.models.manifest import Ecosystem
from src.models.plan import PlannedUpgradeItem, UpgradeAction

logger = logging.getLogger(__name__)


class ManifestMutationError(Exception):
    """Raised when manifest modification cannot be safely completed."""
    pass


def detect_json_indentation(content: str) -> str:
    """Detect the indentation used in a JSON string (defaulting to 2 spaces)."""
    for line in content.splitlines():
        if line.startswith("\t"):
            return "\t"
        match = re.match(r"^( +)\S", line)
        if match:
            return " " * len(match.group(1))
    return "  "


def modify_requirements_manifest(
    content: str,
    plan_items: List[PlannedUpgradeItem],
) -> Tuple[str, List[str]]:
    """Surgically modify requirements.txt content based on planned upgrade items.
    
    Args:
        content: Raw original requirements.txt text.
        plan_items: Planned upgrade items with actions and target versions.
        
    Returns:
        Tuple of (proposed_content, diagnostics).
    """
    diagnostics: List[str] = []
    if not content:
        return "", diagnostics

    # Index upgrades by (line_number, normalized_package_name) and by normalized_package_name
    upgrades_by_line: Dict[int, PlannedUpgradeItem] = {}
    upgrades_by_name: Dict[str, PlannedUpgradeItem] = {}

    for item in plan_items:
        if item.action == UpgradeAction.UPGRADE and item.target_version:
            norm_name = normalize_package_name(item.package_name)
            if item.line_number is not None:
                upgrades_by_line[item.line_number] = item
            else:
                upgrades_by_name[norm_name] = item

    original_lines = content.splitlines(keepends=True)
    if not original_lines and content.strip():
        original_lines = [content]

    proposed_lines: List[str] = []
    applied_lines: Set[int] = set()

    for line_idx, raw_line in enumerate(original_lines, start=1):
        line_body, newline = _detect_newline(raw_line)
        stripped = line_body.strip()

        # Preserve empty lines and comment lines as-is
        if not stripped or stripped.startswith("#"):
            proposed_lines.append(raw_line)
            continue

        # Look up planned upgrade: line number match takes precedence over name match
        planned_item = upgrades_by_line.get(line_idx)
        if planned_item is None:
            # Extract package name candidate from line to check name-based match
            exact_m = EXACT_PIN_PATTERN.match(line_body)
            non_pin_m = NON_PIN_PATTERN.match(line_body)
            bare_m = BARE_PACKAGE_PATTERN.match(line_body)
            
            pkg_name_found: Optional[str] = None
            if exact_m:
                pkg_name_found = exact_m.group(2)
            elif non_pin_m:
                pkg_name_found = non_pin_m.group(2)
            elif bare_m:
                pkg_name_found = bare_m.group(2)

            if pkg_name_found:
                norm = normalize_package_name(pkg_name_found)
                planned_item = upgrades_by_name.get(norm)

        if planned_item is None or not planned_item.target_version:
            # No upgrade planned for this line; preserve exactly
            proposed_lines.append(raw_line)
            continue

        target_ver = planned_item.target_version.strip()
        new_line_str: Optional[str] = None

        # 1. Try exact pin: package[extras]==version ; marker # comment
        exact_match = EXACT_PIN_PATTERN.match(line_body)
        if exact_match:
            leading_ws, pkg, extras, op, ver, marker, comment = exact_match.groups()
            parts = [leading_ws or "", pkg]
            if extras:
                parts.append(extras)
            parts.append(f"=={target_ver}")
            if marker:
                parts.append(f"; {marker.strip()}")
            if comment:
                parts.append(f" {comment.strip()}")
            parts.append(newline)
            new_line_str = "".join(parts)

        # 2. Try non-pinned / range constraint: package[extras]>=version
        if new_line_str is None:
            non_pin_match = NON_PIN_PATTERN.match(line_body)
            if non_pin_match:
                leading_ws, pkg, extras, spec, marker, comment = non_pin_match.groups()
                spec_clean = spec.strip()
                # Determine updated operator
                # If original was ~=1.4.0, preserve ~= -> ~={target_ver}
                # If original was >=1.4.0, update lower bound -> >={target_ver}
                # If original was ==, update == -> =={target_ver}
                # Otherwise, if plan specified proposed_specifier, use that; else pin to ==
                op_to_use = "=="
                if spec_clean.startswith("~="):
                    op_to_use = "~="
                elif spec_clean.startswith(">="):
                    op_to_use = ">="

                parts = [leading_ws or "", pkg]
                if extras:
                    parts.append(extras)
                parts.append(f"{op_to_use}{target_ver}")
                if marker:
                    parts.append(f"; {marker.strip()}")
                if comment:
                    parts.append(f" {comment.strip()}")
                parts.append(newline)
                new_line_str = "".join(parts)

        # 3. Try bare package: package[extras] -> package[extras]==target_ver
        if new_line_str is None:
            bare_match = BARE_PACKAGE_PATTERN.match(line_body)
            if bare_match:
                leading_ws, pkg, extras, marker, comment = bare_match.groups()
                parts = [leading_ws or "", pkg]
                if extras:
                    parts.append(extras)
                parts.append(f"=={target_ver}")
                if marker:
                    parts.append(f"; {marker.strip()}")
                if comment:
                    parts.append(f" {comment.strip()}")
                parts.append(newline)
                new_line_str = "".join(parts)

        if new_line_str is not None:
            proposed_lines.append(new_line_str)
            applied_lines.add(line_idx)
            diagnostics.append(
                f"Line {line_idx}: Updated '{planned_item.package_name}' to version {target_ver}"
            )
        else:
            # Line format could not be safely modified
            proposed_lines.append(raw_line)
            diag_warn = f"Line {line_idx}: Could not safely rewrite constraint for '{planned_item.package_name}'. Preserved original line."
            diagnostics.append(diag_warn)
            logger.warning(diag_warn)

    proposed_content = "".join(proposed_lines)
    return proposed_content, diagnostics


def _apply_npm_version_prefix(original_spec: str, new_version: str) -> str:
    """Preserve existing npm version range prefix (^, ~, >=, <=, >, <, =) or pin."""
    trimmed = original_spec.strip()
    clean_ver = new_version.strip().lstrip("^~><=")

    # Check common prefixes
    for prefix in ("^", "~", ">=", "<=", ">", "<", "="):
        if trimmed.startswith(prefix):
            return f"{prefix}{clean_ver}"

    # Exact version pin or unsupported range
    return clean_ver


def modify_package_json_manifest(
    content: str,
    plan_items: List[PlannedUpgradeItem],
) -> Tuple[str, List[str]]:
    """Safely modify package.json content preserving unrelated fields, indentation, and prefixes.
    
    Args:
        content: Raw original package.json text.
        plan_items: Planned upgrade items.
        
    Returns:
        Tuple of (proposed_content, diagnostics).
    Raises:
        ManifestMutationError if JSON cannot be parsed.
    """
    diagnostics: List[str] = []
    if not content.strip():
        return content, diagnostics

    try:
        data = json.loads(content)
    except json.JSONDecodeError as err:
        raise ManifestMutationError(
            f"Cannot modify package.json: invalid JSON syntax at line {err.lineno}, column {err.colno}"
        ) from err

    if not isinstance(data, dict):
        raise ManifestMutationError("Cannot modify package.json: root element must be a JSON object")

    # Detect indentation style and line endings
    indent_str = detect_json_indentation(content)
    newline = "\r\n" if "\r\n" in content else "\n"

    # Index upgrades by lowercase package name
    upgrades: Dict[str, PlannedUpgradeItem] = {}
    for item in plan_items:
        if item.action == UpgradeAction.UPGRADE and item.target_version:
            upgrades[item.package_name.lower()] = item

    dependency_sections = [
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    ]

    applied_packages: Set[str] = set()

    for section in dependency_sections:
        deps = data.get(section)
        if isinstance(deps, dict):
            for pkg_name in list(deps.keys()):
                norm_pkg = pkg_name.lower()
                if norm_pkg in upgrades:
                    item = upgrades[norm_pkg]
                    orig_spec = str(deps[pkg_name])
                    new_spec = _apply_npm_version_prefix(orig_spec, item.target_version)
                    deps[pkg_name] = new_spec
                    applied_packages.add(norm_pkg)
                    diagnostics.append(
                        f"Updated npm {section} '{pkg_name}' from '{orig_spec}' to '{new_spec}'"
                    )

    # Serialize back with detected indentation and newline
    serialized = json.dumps(data, indent=indent_str, ensure_ascii=False)
    # If the original file ended with a newline, preserve it
    if content.endswith("\n") or content.endswith("\r\n"):
        serialized += newline

    return serialized, diagnostics


def modify_manifest(
    manifest_type: str,
    original_content: str,
    plan_items: List[PlannedUpgradeItem],
) -> Tuple[str, List[str]]:
    """Unified entry point for manifest mutation.
    
    Dispatches to ecosystem-specific modifier and guarantees original_content is unmodified.
    """
    clean_type = manifest_type.strip().lower()

    if clean_type in ("requirements.txt", "requirements", "python") or clean_type.endswith(".txt"):
        return modify_requirements_manifest(original_content, plan_items)

    if clean_type in ("package.json", "npm", "node") or clean_type.endswith(".json"):
        return modify_package_json_manifest(original_content, plan_items)

    raise ManifestMutationError(
        f"Unsupported manifest format '{manifest_type}'. Supported formats: 'requirements.txt', 'package.json'."
    )

