"""Independent closure check of a resolved set (ADR-011 steps 6-8).

Inputs are trusted only: the supervisor's sanitized requirement and constraint lines, dependency
metadata the supervisor fetched from PyPI itself, and the target facts. The check proves that the
selected set satisfies every applicable direct requirement, constraint, ``Requires-Dist`` (with
markers and extras) and ``Requires-Python``, that nothing applicable is missing, and that nothing
unrelated is present.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from src.resolution.target import UNVERIFIABLE_MARKER_VARIABLES


class ClosureError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class InputRequirement:
    requirement: Requirement
    hashes: FrozenSet[str] = frozenset()  # "sha256:<hex>" values from --hash

    @property
    def canonical(self) -> str:
        return canonicalize_name(self.requirement.name)


@dataclass(frozen=True)
class SelectedPackage:
    canonical: str
    version: Version
    sha256: str
    requires_dist: Tuple[Requirement, ...]
    requires_python: Optional[SpecifierSet]


@dataclass
class ClosureResult:
    direct: Set[str] = field(default_factory=set)
    direct_extras: Dict[str, Set[str]] = field(default_factory=dict)
    extras: Dict[str, Set[str]] = field(default_factory=dict)
    # Every specifier that applied to each package, for the prerelease policy.
    specifiers: Dict[str, List[SpecifierSet]] = field(default_factory=dict)
    exact_pins: Set[str] = field(default_factory=set)


def parse_input_lines(lines: Iterable[str]) -> List[InputRequirement]:
    """Parse the supervisor's own sanitized lines (python_input output)."""
    parsed = []
    for line in lines:
        text, *hash_parts = line.split(" --hash=")
        try:
            parsed.append(InputRequirement(Requirement(text), frozenset(h.strip().lower() for h in hash_parts)))
        except InvalidRequirement as err:  # pragma: no cover - sanitized input is always valid
            raise ClosureError("invalid_input", line[:200]) from err
    return parsed


def _applies(marker: Optional[Marker], env: Dict[str, str], extras: Iterable[str]) -> bool:
    if marker is None:
        return True
    text = str(marker)
    if any(variable in text for variable in UNVERIFIABLE_MARKER_VARIABLES):
        raise ClosureError(
            "unverifiable_marker",
            f"marker depends on the Docker host kernel and cannot be verified: {text[:200]}",
        )
    return any(marker.evaluate({**env, "extra": extra}) for extra in ("", *extras))


def _is_exact_pin(spec: SpecifierSet) -> bool:
    return any(s.operator == "===" or (s.operator == "==" and "*" not in s.version) for s in spec)


def check_closure(
    direct: List[InputRequirement],
    constraints: List[InputRequirement],
    selected: Dict[str, SelectedPackage],
    marker_env: Dict[str, str],
    target_python: Version,
) -> ClosureResult:
    result = ClosureResult()

    def note(name: str, spec: SpecifierSet) -> None:
        result.specifiers.setdefault(name, []).append(spec)
        if _is_exact_pin(spec):
            result.exact_pins.add(name)

    def require(name: str, spec: SpecifierSet, why: str) -> SelectedPackage:
        package = selected.get(name)
        if package is None:
            raise ClosureError("incomplete", f"{name} is required ({why}) but not in the resolved set")
        if not spec.contains(package.version, prereleases=True):
            raise ClosureError("unsatisfied", f"{name} {package.version} does not satisfy {spec} ({why})")
        return package

    queue: List[str] = []
    for item in direct:
        req = item.requirement
        if req.url:
            raise ClosureError("direct_reference", req.name)
        if not _applies(req.marker, marker_env, ()):
            continue
        name = item.canonical
        package = require(name, req.specifier, "direct requirement")
        if item.hashes and f"sha256:{package.sha256}" not in item.hashes:
            raise ClosureError("hash_mismatch", f"{name}: the selected file is not among the pinned --hash values")
        result.direct.add(name)
        note(name, req.specifier)
        extras = {canonicalize_name(e) for e in req.extras}
        result.direct_extras.setdefault(name, set()).update(extras)
        result.extras.setdefault(name, set()).update(extras)
        queue.append(name)

    visited: Dict[str, FrozenSet[str]] = {}
    while queue:
        name = queue.pop()
        extras = frozenset(result.extras.get(name, set()))
        if visited.get(name) == extras:
            continue
        visited[name] = extras
        package = selected[name]
        if package.requires_python is not None and not package.requires_python.contains(target_python, prereleases=True):
            raise ClosureError("requires_python", f"{name} {package.version} requires Python {package.requires_python}")
        for dep in package.requires_dist:
            if dep.url:
                raise ClosureError("direct_reference", f"{name} depends on a direct URL: {dep.name}")
            if not _applies(dep.marker, marker_env, extras):
                continue
            dep_name = canonicalize_name(dep.name)
            require(dep_name, dep.specifier, f"required by {name} {package.version}")
            note(dep_name, dep.specifier)
            dep_extras = {canonicalize_name(e) for e in dep.extras}
            known = result.extras.setdefault(dep_name, set())
            if dep_name not in visited or not dep_extras <= known:
                known.update(dep_extras)
                queue.append(dep_name)

    unrelated = sorted(set(selected) - set(visited))
    if unrelated:
        raise ClosureError("extra_packages", f"not required by any applicable requirement: {', '.join(unrelated[:20])}")

    for item in constraints:
        req = item.requirement
        if not _applies(req.marker, marker_env, ()):
            continue
        name = item.canonical
        if name in selected:
            if not req.specifier.contains(selected[name].version, prereleases=True):
                raise ClosureError("constraint_violated", f"{name} {selected[name].version} violates {req.specifier}")
            note(name, req.specifier)
    return result
