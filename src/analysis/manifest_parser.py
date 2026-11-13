"""Deterministic and safe manifest parsers for Python requirements and Node.js package.json.

Guarantees:
- Zero code execution (no eval, no imports, no child processes).
- Line preservation and accurate line numbers for requirements.txt.
- Graceful error containment: single malformed lines do not break overall parsing.
- Support for requirements.txt directives (-r, -e, URLs) with diagnostic warnings.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet

from src.analysis.semver_evaluator import (
    extract_pinned_version,
    is_supported_semver_range,
)
from src.core.parser import (
    BARE_PACKAGE_PATTERN,
    EXACT_PIN_PATTERN,
    NON_PIN_PATTERN,
    PACKAGE_NAME_REGEX,
    _detect_newline,
)
from src.data.catalog_loader import normalize_package_name
from src.models.manifest import DependencyType, Ecosystem, ParsedDependency


class ManifestParseError(Exception):
    """Raised when an unrecoverable structural syntax error prevents manifest parsing."""

    def __init__(self, message: str, diagnostics: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or [message]


# Pip flags and directives regex
PIP_DIRECTIVE_PATTERN = re.compile(
    r"^[ \t]*(-r|--requirement|-c|--constraint|-e|--editable|-f|--find-links|-i|--index-url|--extra-index-url)[ \t]+([^#\r\n]+)",
    re.IGNORECASE,
)
VCS_URL_PATTERN = re.compile(
    r"^[ \t]*(?:git\+|hg\+|svn\+|bzr\+|https?://|file://)[^#\r\n]+",
    re.IGNORECASE,
)


def parse_requirements_manifest(content: str) -> Tuple[List[ParsedDependency], List[str]]:
    """Safely parse requirements.txt content without code execution.
    
    Returns:
        Tuple of (parsed_dependencies, global_diagnostics)
    """
    dependencies: List[ParsedDependency] = []
    diagnostics: List[str] = []

    lines = content.splitlines(keepends=True)
    if not lines and content.strip():
        lines = [content]

    for line_idx, raw_line in enumerate(lines, start=1):
        line_text, _ = _detect_newline(raw_line)
        stripped = line_text.strip()

        # Skip empty lines
        if not stripped:
            continue

        # Skip full-line comments
        if stripped.startswith("#"):
            continue

        # Check for pip flags / directives
        flag_match = PIP_DIRECTIVE_PATTERN.match(line_text)
        if flag_match:
            directive = flag_match.group(1)
            target = flag_match.group(2).strip()
            diag = f"Line {line_idx}: Preserved pip directive '{directive} {target}' (not evaluated for version upgrades)."
            diagnostics.append(diag)
            dependencies.append(
                ParsedDependency(
                    name=directive,
                    original_name=directive,
                    ecosystem=Ecosystem.PYTHON,
                    dependency_type=DependencyType.RUNTIME,
                    declared_specifier=target,
                    line_number=line_idx,
                    raw_line=line_text,
                    is_supported=False,
                    diagnostic_message=diag,
                )
            )
            continue

        # Check for VCS / direct URLs
        if VCS_URL_PATTERN.match(line_text):
            diag = f"Line {line_idx}: Preserved direct URL / VCS dependency (not evaluated for version upgrades)."
            diagnostics.append(diag)
            dependencies.append(
                ParsedDependency(
                    name="url-dependency",
                    original_name=stripped.split()[0],
                    ecosystem=Ecosystem.PYTHON,
                    dependency_type=DependencyType.RUNTIME,
                    declared_specifier=stripped,
                    line_number=line_idx,
                    raw_line=line_text,
                    is_supported=False,
                    diagnostic_message=diag,
                )
            )
            continue

        # Try exact pin: package[extras]==version ; markers # comment
        exact_match = EXACT_PIN_PATTERN.match(line_text)
        if exact_match:
            pkg_name = exact_match.group(2)
            extras = exact_match.group(3)
            version_str = exact_match.group(5)
            marker = exact_match.group(6)
            dependencies.append(
                ParsedDependency(
                    name=normalize_package_name(pkg_name),
                    original_name=pkg_name,
                    ecosystem=Ecosystem.PYTHON,
                    dependency_type=DependencyType.RUNTIME,
                    declared_specifier=f"=={version_str}",
                    current_version=version_str,
                    line_number=line_idx,
                    raw_line=line_text,
                    is_supported=True,
                    extras=extras,
                    environment_marker=marker.strip() if marker else None,
                )
            )
            continue

        # Try non-pinned / compound specifier: package>=1.0.0,<=2.0.0
        non_pin_match = NON_PIN_PATTERN.match(line_text)
        if non_pin_match:
            pkg_name = non_pin_match.group(2)
            extras = non_pin_match.group(3)
            specifier = non_pin_match.group(4).strip()
            marker = non_pin_match.group(5)
            diag = f"Line {line_idx}: Non-pinned constraint '{specifier}' preserved."
            dependencies.append(
                ParsedDependency(
                    name=normalize_package_name(pkg_name),
                    original_name=pkg_name,
                    ecosystem=Ecosystem.PYTHON,
                    dependency_type=DependencyType.RUNTIME,
                    declared_specifier=specifier,
                    current_version=None,
                    line_number=line_idx,
                    raw_line=line_text,
                    is_supported=True,
                    diagnostic_message=diag,
                    extras=extras,
                    environment_marker=marker.strip() if marker else None,
                )
            )
            continue

        # Try bare package: package
        bare_match = BARE_PACKAGE_PATTERN.match(line_text)
        if bare_match:
            pkg_name = bare_match.group(2)
            extras = bare_match.group(3)
            marker = bare_match.group(4)
            diag = f"Line {line_idx}: Bare package without version constraint (preserved)."
            dependencies.append(
                ParsedDependency(
                    name=normalize_package_name(pkg_name),
                    original_name=pkg_name,
                    ecosystem=Ecosystem.PYTHON,
                    dependency_type=DependencyType.RUNTIME,
                    declared_specifier="*",
                    current_version=None,
                    line_number=line_idx,
                    raw_line=line_text,
                    is_supported=True,
                    diagnostic_message=diag,
                    extras=extras,
                    environment_marker=marker.strip() if marker else None,
                )
            )
            continue

        # Try PEP 508 fallback via packaging.requirements.Requirement
        try:
            # Strip inline comments before passing to packaging
            clean_part = re.split(r"[ \t]+#", stripped, maxsplit=1)[0].strip()
            req = Requirement(clean_part)
            norm_name = normalize_package_name(req.name)

            # Check if this is a PEP 508 direct reference (e.g. pkg @ https://... or pkg @ git+https://...)
            if req.url:
                diag = f"Line {line_idx}: Preserved direct URL reference '{req.url}' for '{req.name}' (not evaluated for registry upgrades)."
                diagnostics.append(diag)
                dependencies.append(
                    ParsedDependency(
                        name=norm_name,
                        original_name=req.name,
                        ecosystem=Ecosystem.PYTHON,
                        dependency_type=DependencyType.RUNTIME,
                        declared_specifier=f"@ {req.url}",
                        current_version=None,
                        line_number=line_idx,
                        raw_line=line_text,
                        is_supported=False,
                        diagnostic_message=diag,
                        extras=f"[{','.join(req.extras)}]" if req.extras else None,
                        environment_marker=str(req.marker) if req.marker else None,
                    )
                )
                continue

            spec_str = str(req.specifier) if req.specifier else "*"
            curr_ver = None
            # Check if specifier is an exact pin ==x.y.z
            for s in req.specifier:
                if s.operator == "==":
                    curr_ver = s.version
                    break

            dependencies.append(
                ParsedDependency(
                    name=norm_name,
                    original_name=req.name,
                    ecosystem=Ecosystem.PYTHON,
                    dependency_type=DependencyType.RUNTIME,
                    declared_specifier=spec_str,
                    current_version=curr_ver,
                    line_number=line_idx,
                    raw_line=line_text,
                    is_supported=True,
                    extras=f"[{','.join(req.extras)}]" if req.extras else None,
                    environment_marker=str(req.marker) if req.marker else None,
                )
            )
            continue
        except (InvalidRequirement, Exception) as err:
            diag = f"Line {line_idx}: Unparseable requirement syntax '{stripped}': {err}"
            diagnostics.append(diag)
            dependencies.append(
                ParsedDependency(
                    name=f"unparseable-line-{line_idx}",
                    original_name=stripped,
                    ecosystem=Ecosystem.PYTHON,
                    dependency_type=DependencyType.RUNTIME,
                    declared_specifier="",
                    line_number=line_idx,
                    raw_line=line_text,
                    is_supported=False,
                    diagnostic_message=diag,
                )
            )

    return dependencies, diagnostics


def parse_package_json_manifest(content: str) -> Tuple[List[ParsedDependency], List[str]]:
    """Safely parse package.json content without code execution.
    
    Returns:
        Tuple of (parsed_dependencies, global_diagnostics)
    Raises:
        ManifestParseError if JSON is structurally invalid.
    """
    dependencies: List[ParsedDependency] = []
    diagnostics: List[str] = []

    if not content.strip():
        return dependencies, ["Manifest is empty."]

    try:
        data = json.loads(content)
    except json.JSONDecodeError as err:
        error_msg = f"Invalid JSON syntax in package.json at line {err.lineno}, column {err.colno}: {err.msg}"
        raise ManifestParseError(error_msg, diagnostics=[error_msg]) from err

    if not isinstance(data, dict):
        error_msg = "package.json root must be a JSON object"
        raise ManifestParseError(error_msg, diagnostics=[error_msg])

    # Parse dependencies (runtime)
    runtime_deps = data.get("dependencies")
    if isinstance(runtime_deps, dict):
        for pkg_name, spec in runtime_deps.items():
            if not isinstance(spec, str):
                diag = f"Dependency '{pkg_name}' has non-string version specifier '{spec}' (ignored)."
                diagnostics.append(diag)
                continue
            cleaned_spec = spec.strip()
            is_valid, reason = is_supported_semver_range(cleaned_spec)
            if not is_valid:
                diag = f"Dependency '{pkg_name}' has unsupported specifier '{cleaned_spec}' ({reason}). Preserved without modification."
                diagnostics.append(diag)
                dependencies.append(
                    ParsedDependency(
                        name=pkg_name.lower(),
                        original_name=pkg_name,
                        ecosystem=Ecosystem.NPM,
                        dependency_type=DependencyType.RUNTIME,
                        declared_specifier=cleaned_spec,
                        current_version=None,
                        is_supported=False,
                        diagnostic_message=diag,
                    )
                )
                continue

            pinned_ver = extract_pinned_version(cleaned_spec)
            dependencies.append(
                ParsedDependency(
                    name=pkg_name.lower(),
                    original_name=pkg_name,
                    ecosystem=Ecosystem.NPM,
                    dependency_type=DependencyType.RUNTIME,
                    declared_specifier=cleaned_spec,
                    current_version=pinned_ver,
                    is_supported=True,
                )
            )
    elif runtime_deps is not None:
        diagnostics.append("'dependencies' field is not a valid JSON object.")

    # Parse devDependencies (development)
    dev_deps = data.get("devDependencies")
    if isinstance(dev_deps, dict):
        for pkg_name, spec in dev_deps.items():
            if not isinstance(spec, str):
                diag = f"DevDependency '{pkg_name}' has non-string version specifier '{spec}' (ignored)."
                diagnostics.append(diag)
                continue
            cleaned_spec = spec.strip()
            is_valid, reason = is_supported_semver_range(cleaned_spec)
            if not is_valid:
                diag = f"DevDependency '{pkg_name}' has unsupported specifier '{cleaned_spec}' ({reason}). Preserved without modification."
                diagnostics.append(diag)
                dependencies.append(
                    ParsedDependency(
                        name=pkg_name.lower(),
                        original_name=pkg_name,
                        ecosystem=Ecosystem.NPM,
                        dependency_type=DependencyType.DEVELOPMENT,
                        declared_specifier=cleaned_spec,
                        current_version=None,
                        is_supported=False,
                        diagnostic_message=diag,
                    )
                )
                continue

            pinned_ver = extract_pinned_version(cleaned_spec)
            dependencies.append(
                ParsedDependency(
                    name=pkg_name.lower(),
                    original_name=pkg_name,
                    ecosystem=Ecosystem.NPM,
                    dependency_type=DependencyType.DEVELOPMENT,
                    declared_specifier=cleaned_spec,
                    current_version=pinned_ver,
                    is_supported=True,
                )
            )
    elif dev_deps is not None:
        diagnostics.append("'devDependencies' field is not a valid JSON object.")

    return dependencies, diagnostics


def parse_manifest(manifest_type: str, content: str) -> Tuple[List[ParsedDependency], List[str], Ecosystem]:
    """Unified entry point for manifest parsing.
    
    Identifies ecosystem, dispatches to safe parser, and handles errors.
    """
    clean_type = manifest_type.strip().lower()

    if clean_type in ("requirements.txt", "requirements", "python") or clean_type.endswith(".txt"):
        deps, diags = parse_requirements_manifest(content)
        return deps, diags, Ecosystem.PYTHON

    if clean_type in ("package.json", "npm", "node") or clean_type.endswith(".json"):
        deps, diags = parse_package_json_manifest(content)
        return deps, diags, Ecosystem.NPM

    raise ManifestParseError(
        f"Unsupported manifest type '{manifest_type}'. Supported types are 'requirements.txt' and 'package.json'."
    )

