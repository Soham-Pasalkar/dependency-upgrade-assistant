"""Python 3.10–3.14 compatible parser for pyproject.toml dependency declarations.

Supports:
- PEP 621 [project.dependencies]
- PEP 621 [project.optional-dependencies] (with group naming)
- PEP 735 [dependency-groups]
- Poetry [tool.poetry.dependencies] and [tool.poetry.group.<name>.dependencies]

Features:
- Seamless cross-version TOML parsing (standard tomllib in 3.11+, tomli fallback for 3.10).
- Pure static analysis without executing build backends or arbitrary project code.
- PEP 508 standard requirement decomposition via packaging.requirements.Requirement.
- Seamless bridge to RequirementLine objects for uniform downstream comparison.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Cross-version TOML parsing: tomllib in Python 3.11+, tomli fallback for Python 3.10
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        raise ImportError(
            "Neither 'tomllib' (built-in in Python 3.11+) nor 'tomli' was found. "
            "Python 3.10 requires 'tomli' for TOML parsing."
        )

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, parse as parse_version

from src.core.models import LineType, RequirementLine


@dataclass(frozen=True)
class ParsedPyProjectDependency:
    """Represents a structured dependency extracted from pyproject.toml."""
    package_name: str
    specifier: str
    operator: Optional[str]
    version: Optional[str]
    extras: Optional[str]
    environment_marker: Optional[str]
    source_section: str
    group_name: Optional[str]
    raw_declaration: str
    is_exact_pin: bool

    def to_requirement_line(self, line_number: int = 1) -> RequirementLine:
        """Convert to a core RequirementLine for downstream compatibility evaluation."""
        line_type = LineType.EXACT_PIN if self.is_exact_pin else (
            LineType.NON_PINNED_SPECIFIER if self.operator else LineType.BARE_PACKAGE
        )
        return RequirementLine(
            line_number=line_number,
            raw_text=self.raw_declaration,
            line_type=line_type,
            package_name=self.package_name,
            extras=self.extras,
            operator=self.operator,
            version=self.version,
            environment_marker=self.environment_marker,
        )


@dataclass
class PyProjectParseResult:
    """Aggregated outcome of pyproject.toml dependency extraction."""
    dependencies: List[ParsedPyProjectDependency]
    unsupported_declarations: List[str]
    build_backend: Optional[str] = None
    project_name: Optional[str] = None
    project_version: Optional[str] = None
    requires_python: Optional[str] = None


def _decompose_pep508_requirement(
    req_str: str,
    source_section: str,
    group_name: Optional[str] = None,
) -> Tuple[Optional[ParsedPyProjectDependency], Optional[str]]:
    """Parse a PEP 508 string into ParsedPyProjectDependency using packaging.requirements."""
    clean_str = req_str.strip()
    if not clean_str or clean_str.startswith("#"):
        return None, None

    try:
        req = Requirement(clean_str)
    except InvalidRequirement as err:
        return None, f"Invalid PEP 508 requirement '{clean_str}' in {source_section}: {err}"

    package_name = req.name
    extras_str = f"[{','.join(sorted(req.extras))}]" if req.extras else None
    marker_str = str(req.marker) if req.marker else None

    # Analyze specifiers
    spec_str = str(req.specifier) if req.specifier else ""
    operator: Optional[str] = None
    version_str: Optional[str] = None
    is_exact_pin = False

    spec_list = list(req.specifier) if req.specifier else []
    if len(spec_list) == 1 and spec_list[0].operator == "==":
        candidate_ver = spec_list[0].version
        if "*" not in candidate_ver:
            try:
                parse_version(candidate_ver)
                operator = "=="
                version_str = candidate_ver
                is_exact_pin = True
            except InvalidVersion:
                pass
    elif len(spec_list) == 1:
        operator = spec_list[0].operator
        version_str = spec_list[0].version
    elif len(spec_list) > 1:
        operator = spec_str

    dep = ParsedPyProjectDependency(
        package_name=package_name,
        specifier=spec_str,
        operator=operator,
        version=version_str,
        extras=extras_str,
        environment_marker=marker_str,
        source_section=source_section,
        group_name=group_name,
        raw_declaration=clean_str,
        is_exact_pin=is_exact_pin,
    )
    return dep, None


def _decompose_poetry_dependency(
    pkg_name: str,
    spec: Any,
    source_section: str,
    group_name: Optional[str] = None,
) -> Tuple[Optional[ParsedPyProjectDependency], Optional[str]]:
    """Parse a Poetry dependency declaration (string or dict) into ParsedPyProjectDependency."""
    # Skip python version constraint entry in poetry
    if pkg_name.lower() == "python":
        return None, None

    version_constraint = ""
    extras_list: List[str] = []
    marker_str: Optional[str] = None

    if isinstance(spec, str):
        version_constraint = spec.strip()
    elif isinstance(spec, dict):
        version_constraint = str(spec.get("version", "")).strip()
        raw_extras = spec.get("extras", [])
        if isinstance(raw_extras, list):
            extras_list = [str(e) for e in raw_extras]
        raw_markers = spec.get("markers")
        if raw_markers:
            marker_str = str(raw_markers)
    else:
        return None, f"Unsupported Poetry dependency format for '{pkg_name}' in {source_section}: {type(spec)}"

    extras_str = f"[{','.join(sorted(extras_list))}]" if extras_list else None
    raw_decl = f"{pkg_name}{extras_str or ''} {version_constraint}".strip()

    operator: Optional[str] = None
    version_str: Optional[str] = None
    is_exact_pin = False

    # Check for exact pin (e.g. "==1.2.3" or "1.2.3")
    exact_match = re.match(r"^(?:==)?([0-9A-Za-z_.\-+]+)$", version_constraint)
    if exact_match and "*" not in version_constraint and not version_constraint.startswith(("^", "~", ">", "<", "!")):
        v_cand = exact_match.group(1)
        try:
            parse_version(v_cand)
            operator = "=="
            version_str = v_cand
            is_exact_pin = True
        except InvalidVersion:
            pass

    if not is_exact_pin and version_constraint:
        operator = version_constraint

    dep = ParsedPyProjectDependency(
        package_name=pkg_name,
        specifier=version_constraint,
        operator=operator,
        version=version_str,
        extras=extras_str,
        environment_marker=marker_str,
        source_section=source_section,
        group_name=group_name,
        raw_declaration=raw_decl,
        is_exact_pin=is_exact_pin,
    )
    return dep, None


def parse_pyproject_toml(file_path: Path | str) -> PyProjectParseResult:
    """Parse dependency declarations from a pyproject.toml file.
    
    Extracts:
    - [project.dependencies]
    - [project.optional-dependencies]
    - [dependency-groups]
    - [tool.poetry.dependencies]
    - [tool.poetry.group.<group>.dependencies]
    
    Raises:
        FileNotFoundError: If file_path does not exist.
        ValueError: If file content is not valid TOML.
    """
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"pyproject.toml not found at: {path}")

    try:
        content = path.read_bytes()
        data = tomllib.loads(content.decode("utf-8"))
    except Exception as err:
        raise ValueError(f"Failed to parse pyproject.toml: {err}") from err

    dependencies: List[ParsedPyProjectDependency] = []
    unsupported: List[str] = []

    # Extract build-system metadata
    build_sys = data.get("build-system", {})
    build_backend = build_sys.get("build-backend") if isinstance(build_sys, dict) else None

    # Extract project table
    project = data.get("project", {})
    project_name: Optional[str] = None
    project_version: Optional[str] = None
    requires_python: Optional[str] = None

    if isinstance(project, dict):
        project_name = project.get("name")
        project_version = project.get("version")
        requires_python = project.get("requires-python")

        # 1. Standard [project.dependencies]
        main_deps = project.get("dependencies", [])
        if isinstance(main_deps, list):
            for item in main_deps:
                if isinstance(item, str):
                    dep, err = _decompose_pep508_requirement(item, "project.dependencies")
                    if dep:
                        dependencies.append(dep)
                    elif err:
                        unsupported.append(err)
                else:
                    unsupported.append(f"Non-string item in project.dependencies: {item}")
        elif main_deps is not None:
            unsupported.append(f"project.dependencies must be a list, got {type(main_deps)}")

        # 2. Standard [project.optional-dependencies]
        opt_deps = project.get("optional-dependencies", {})
        if isinstance(opt_deps, dict):
            for group, group_list in opt_deps.items():
                if isinstance(group_list, list):
                    for item in group_list:
                        if isinstance(item, str):
                            dep, err = _decompose_pep508_requirement(
                                item,
                                f"project.optional-dependencies.{group}",
                                group_name=group,
                            )
                            if dep:
                                dependencies.append(dep)
                            elif err:
                                unsupported.append(err)
                        else:
                            unsupported.append(f"Non-string item in optional-dependencies.{group}: {item}")
                else:
                    unsupported.append(f"optional-dependencies.{group} must be a list, got {type(group_list)}")

    # 3. Standard PEP 735 [dependency-groups]
    dep_groups = data.get("dependency-groups", {})
    if isinstance(dep_groups, dict):
        for group, items in dep_groups.items():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, str):
                        dep, err = _decompose_pep508_requirement(
                            item,
                            f"dependency-groups.{group}",
                            group_name=group,
                        )
                        if dep:
                            dependencies.append(dep)
                        elif err:
                            unsupported.append(err)
                    elif isinstance(item, dict) and "include-group" in item:
                        # Informational record of group inclusion
                        continue
                    else:
                        unsupported.append(f"Unsupported item in dependency-groups.{group}: {item}")

    # 4. Poetry [tool.poetry.dependencies]
    tool = data.get("tool", {})
    if isinstance(tool, dict):
        poetry = tool.get("poetry", {})
        if isinstance(poetry, dict):
            poetry_deps = poetry.get("dependencies", {})
            if isinstance(poetry_deps, dict):
                for pkg_name, spec in poetry_deps.items():
                    dep, err = _decompose_poetry_dependency(
                        pkg_name, spec, "tool.poetry.dependencies"
                    )
                    if dep:
                        dependencies.append(dep)
                    elif err:
                        unsupported.append(err)

            # Poetry [tool.poetry.group.<group>.dependencies]
            poetry_groups = poetry.get("group", {})
            if isinstance(poetry_groups, dict):
                for group_name, group_data in poetry_groups.items():
                    if isinstance(group_data, dict):
                        g_deps = group_data.get("dependencies", {})
                        if isinstance(g_deps, dict):
                            for pkg_name, spec in g_deps.items():
                                dep, err = _decompose_poetry_dependency(
                                    pkg_name,
                                    spec,
                                    f"tool.poetry.group.{group_name}.dependencies",
                                    group_name=group_name,
                                )
                                if dep:
                                    dependencies.append(dep)
                                elif err:
                                    unsupported.append(err)

    return PyProjectParseResult(
        dependencies=dependencies,
        unsupported_declarations=unsupported,
        build_backend=build_backend,
        project_name=project_name,
        project_version=project_version,
        requires_python=requires_python,
    )

