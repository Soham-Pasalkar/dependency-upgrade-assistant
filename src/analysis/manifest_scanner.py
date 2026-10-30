"""Recursive manifest scanner for repository dependency files.

Discovers supported Python dependency manifests:
- requirements.txt, requirements-dev.txt, requirements-*.txt, and custom requirements files.
- pyproject.toml.
- Nested requirements files referenced via `-r` or `--requirement`.
- Constraint files referenced via `-c` or `--constraint`.

Enforces safety and integrity:
- Excludes .git, .venv, venv, caches, build artifacts, and non-project directories.
- Prevents following symlinks outside the repository root.
- Detects duplicate manifests and circular requirements references.
- Preserves relative source paths.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

DEFAULT_SCAN_EXCLUDED_DIRS: Set[str] = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".tox",
    "node_modules",
}

# Regex to detect -r / --requirement and -c / --constraint references in requirements files
REQUIREMENT_REF_PATTERN = re.compile(
    r"^[ \t]*(?:-r|--requirement)[ \t]+([^#\r\n]+)", re.IGNORECASE
)
CONSTRAINT_REF_PATTERN = re.compile(
    r"^[ \t]*(?:-c|--constraint)[ \t]+([^#\r\n]+)", re.IGNORECASE
)


class ManifestType(str, Enum):
    """Categorization of dependency manifest types."""
    REQUIREMENTS_TXT = "requirements_txt"
    PYPROJECT_TOML = "pyproject_toml"
    CONSTRAINTS_TXT = "constraints_txt"


@dataclass(frozen=True)
class DiscoveredManifest:
    """Represents a discovered dependency manifest file."""
    path: Path
    relative_path: str
    manifest_type: ManifestType
    referenced_by: Optional[str] = None  # Relative path of referring manifest if nested


@dataclass
class ManifestScanResult:
    """Aggregated output of repository manifest discovery."""
    manifests: List[DiscoveredManifest] = field(default_factory=list)
    nested_references: Dict[str, List[str]] = field(default_factory=dict)
    circular_references: List[Tuple[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def has_manifests(self) -> bool:
        """Check whether at least one supported manifest was discovered."""
        return len(self.manifests) > 0

    @property
    def relative_paths(self) -> List[str]:
        """Return list of discovered manifest relative paths."""
        return [m.relative_path for m in self.manifests]


def is_requirements_filename(filename: str) -> bool:
    """Check if a filename follows requirements conventions."""
    name_lower = filename.lower()
    if name_lower == "requirements.txt" or name_lower == "requirements-dev.txt":
        return True
    if name_lower.startswith("requirements") and name_lower.endswith(".txt"):
        return True
    if name_lower.endswith("-requirements.txt") or name_lower.endswith("_requirements.txt"):
        return True
    return False


def is_safe_relative_path(base_dir: Path, target_path: Path) -> bool:
    """Check if target_path resolves inside base_dir without escaping via symlinks or traversal."""
    try:
        resolved_base = base_dir.resolve()
        resolved_target = target_path.resolve()
        return resolved_base in resolved_target.parents or resolved_base == resolved_target
    except (OSError, RuntimeError):
        return False


def scan_manifests(
    repo_root: Path | str,
    exclude_dirs: Optional[Set[str]] = None,
) -> ManifestScanResult:
    """Scan a repository workspace for dependency manifests.
    
    Args:
        repo_root: Path to the repository root directory.
        exclude_dirs: Set of directory names to skip during scan.
        
    Returns:
        ManifestScanResult containing discovered manifests, nested references,
        circular dependencies, and scan warnings.
    """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Repository root directory does not exist: {root}")

    ignored = exclude_dirs or DEFAULT_SCAN_EXCLUDED_DIRS
    result = ManifestScanResult()

    discovered_by_path: Dict[Path, DiscoveredManifest] = {}
    pending_nested_scans: List[Tuple[Path, Optional[str], ManifestType]] = []

    # 1. Primary walk of repository workspace
    for current_dir, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current_dir)
        
        # Security: Do not follow paths escaping repository root
        if not is_safe_relative_path(root, current_path):
            dirs[:] = []
            continue

        # Prune excluded directories
        dirs[:] = [
            d for d in dirs
            if d not in ignored and not d.endswith(".egg-info")
        ]

        for file_name in files:
            file_path = current_path / file_name

            # Security: Ensure individual file is safe
            if not is_safe_relative_path(root, file_path):
                continue

            rel_str = str(file_path.relative_to(root))

            # Match pyproject.toml
            if file_name.lower() == "pyproject.toml":
                manifest = DiscoveredManifest(
                    path=file_path,
                    relative_path=rel_str,
                    manifest_type=ManifestType.PYPROJECT_TOML,
                )
                discovered_by_path[file_path.resolve()] = manifest
                result.manifests.append(manifest)

            # Match requirements*.txt
            elif is_requirements_filename(file_name):
                manifest = DiscoveredManifest(
                    path=file_path,
                    relative_path=rel_str,
                    manifest_type=ManifestType.REQUIREMENTS_TXT,
                )
                discovered_by_path[file_path.resolve()] = manifest
                result.manifests.append(manifest)
                pending_nested_scans.append((file_path, None, ManifestType.REQUIREMENTS_TXT))

    # 2. Inspect requirements files for nested references (-r, -c)
    visited_for_nesting: Set[Path] = set()

    def process_nested_references(parent_path: Path, call_chain: List[Path]) -> None:
        resolved_parent = parent_path.resolve()
        if resolved_parent in call_chain:
            # Circular reference detected!
            cycle_pair = (
                str(call_chain[-1].relative_to(root)),
                str(resolved_parent.relative_to(root)),
            )
            result.circular_references.append(cycle_pair)
            result.warnings.append(
                f"Circular reference detected: '{cycle_pair[0]}' references '{cycle_pair[1]}'"
            )
            return

        new_chain = call_chain + [resolved_parent]
        parent_rel = str(parent_path.relative_to(root))

        try:
            content = parent_path.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            result.warnings.append(f"Unable to read manifest '{parent_rel}': {err}")
            return

        for line in content.splitlines():
            line_str = line.strip()
            
            # Check for -r / --requirement
            req_match = REQUIREMENT_REF_PATTERN.match(line_str)
            const_match = CONSTRAINT_REF_PATTERN.match(line_str)
            
            ref_type: Optional[ManifestType] = None
            ref_target: Optional[str] = None
            
            if req_match:
                ref_type = ManifestType.REQUIREMENTS_TXT
                ref_target = req_match.group(1).strip()
            elif const_match:
                ref_type = ManifestType.CONSTRAINTS_TXT
                ref_target = const_match.group(1).strip()

            if not ref_target or not ref_type:
                continue

            # Resolve reference relative to the directory containing parent_path
            child_path = (parent_path.parent / ref_target).resolve()

            # Security check: Must reside within repository root
            if not is_safe_relative_path(root, child_path):
                result.warnings.append(
                    f"Blocked unsafe path traversal reference in '{parent_rel}': '{ref_target}'"
                )
                continue

            child_rel = str(child_path.relative_to(root))

            # Record nested reference link
            if parent_rel not in result.nested_references:
                result.nested_references[parent_rel] = []
            if child_rel not in result.nested_references[parent_rel]:
                result.nested_references[parent_rel].append(child_rel)

            if not child_path.is_file():
                result.warnings.append(
                    f"Referenced manifest not found: '{ref_target}' referenced from '{parent_rel}'"
                )
                continue

            # Add to discovered manifests if not already recorded
            if child_path not in discovered_by_path:
                discovered_child = DiscoveredManifest(
                    path=child_path,
                    relative_path=child_rel,
                    manifest_type=ref_type,
                    referenced_by=parent_rel,
                )
                discovered_by_path[child_path] = discovered_child
                result.manifests.append(discovered_child)

            # Recurse to process children of the nested file
            process_nested_references(child_path, new_chain)

    for path, _, _ in pending_nested_scans:
        if path.resolve() not in visited_for_nesting:
            visited_for_nesting.add(path.resolve())
            process_nested_references(path, [])

    # Sort manifests deterministically by relative path
    result.manifests.sort(key=lambda m: m.relative_path)
    return result

