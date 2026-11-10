"""Pure-Python Semantic Versioning (SemVer 2.0.0) parser and range evaluator.

Provides deterministic evaluation of Node.js/npm version constraints without
spawning external Node.js subprocesses.
"""
from __future__ import annotations

import re
from functools import total_ordering
from typing import Any, List, Optional, Tuple


SEMVER_REGEX = re.compile(
    r"^v?"
    r"(?P<major>0|[1-9]\d*)"
    r"(?:\.(?P<minor>0|[1-9]\d*))?"
    r"(?:\.(?P<patch>0|[1-9]\d*))?"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


@total_ordering
class SemVer:
    """Represents a SemVer 2.0.0 version."""

    def __init__(
        self,
        major: int,
        minor: int = 0,
        patch: int = 0,
        prerelease: Optional[str] = None,
        build: Optional[str] = None,
        raw: Optional[str] = None,
    ) -> None:
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = prerelease
        self.build = build
        self.raw = raw or f"{major}.{minor}.{patch}" + (f"-{prerelease}" if prerelease else "")

    @property
    def canonical(self) -> str:
        """Returns standard SemVer string without 'v' prefix or build metadata."""
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += f"-{self.prerelease}"
        return s

    @classmethod
    def parse(cls, version_str: str) -> Optional[SemVer]:
        """Parse a version string into a SemVer object. Returns None if invalid."""
        if not version_str or not isinstance(version_str, str):
            return None
        cleaned = version_str.strip()
        match = SEMVER_REGEX.match(cleaned)
        if not match:
            return None
        major = int(match.group("major"))
        minor = int(match.group("minor")) if match.group("minor") is not None else 0
        patch = int(match.group("patch")) if match.group("patch") is not None else 0
        prerelease = match.group("prerelease")
        build = match.group("build")
        return cls(major, minor, patch, prerelease, build, raw=cleaned)

    def is_prerelease(self) -> bool:
        return self.prerelease is not None

    def _prerelease_tuple(self) -> Tuple[Any, ...]:
        """Convert prerelease string to tuple for SemVer precedence comparison."""
        if self.prerelease is None:
            # Normal versions have higher precedence than prereleases
            return (1,)
        parts = []
        for part in self.prerelease.split("."):
            if part.isdigit():
                parts.append((0, int(part)))
            else:
                parts.append((1, part))
        return (0, tuple(parts))

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, SemVer):
            return False
        return (
            self.major == other.major
            and self.minor == other.minor
            and self.patch == other.patch
            and self.prerelease == other.prerelease
        )

    def __lt__(self, other: Any) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)
        return self._prerelease_tuple() < other._prerelease_tuple()

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def __repr__(self) -> str:
        return f"SemVer({self.raw})"

    def __str__(self) -> str:
        return self.raw


# ---------------------------------------------------------------------------
# Range Evaluation
# ---------------------------------------------------------------------------

class Comparator:
    """A single atomic comparator, e.g. '>=1.2.3', '<2.0.0', or '=1.0.0'."""

    def __init__(self, op: str, ver: SemVer) -> None:
        self.op = op
        self.ver = ver

    def satisfies(self, target: SemVer) -> bool:
        if self.op == "=":
            return target == self.ver
        elif self.op == ">":
            return target > self.ver
        elif self.op == ">=":
            return target >= self.ver
        elif self.op == "<":
            return target < self.ver
        elif self.op == "<=":
            return target <= self.ver
        return False

    def __repr__(self) -> str:
        return f"Comparator({self.op}{self.ver})"


def _parse_simple_range(subrange: str) -> List[Comparator]:
    """Parse a simple space-delimited comparator set, or desugar caret/tilde."""
    subrange = subrange.strip()
    if not subrange or subrange in ("*", "x", "X"):
        return []

    # Handle hyphen range: 1.2.3 - 2.3.4
    if " - " in subrange:
        parts = subrange.split(" - ", 1)
        v1 = SemVer.parse(parts[0])
        v2 = SemVer.parse(parts[1])
        comps: List[Comparator] = []
        if v1:
            comps.append(Comparator(">=", v1))
        if v2:
            comps.append(Comparator("<=", v2))
        return comps

    tokens = subrange.split()
    comparators: List[Comparator] = []

    for token in tokens:
        token = token.strip()
        if not token or token in ("*", "x", "X"):
            continue

        # Caret range: ^1.2.3
        if token.startswith("^"):
            raw_ver = token[1:].strip()
            v = SemVer.parse(raw_ver)
            if not v:
                continue
            comparators.append(Comparator(">=", v))
            if v.major > 0:
                comparators.append(Comparator("<", SemVer(v.major + 1, 0, 0)))
            elif v.minor > 0:
                comparators.append(Comparator("<", SemVer(0, v.minor + 1, 0)))
            else:
                comparators.append(Comparator("<", SemVer(0, 0, v.patch + 1)))
            continue

        # Tilde range: ~1.2.3, ~1.2, ~1
        if token.startswith("~"):
            raw_ver = token[1:].strip()
            # Check precision
            parts = raw_ver.split(".")
            v = SemVer.parse(raw_ver)
            if not v:
                continue
            comparators.append(Comparator(">=", v))
            if len(parts) <= 1:
                comparators.append(Comparator("<", SemVer(v.major + 1, 0, 0)))
            else:
                comparators.append(Comparator("<", SemVer(v.major, v.minor + 1, 0)))
            continue

        # Relational operator
        m = re.match(r"^(>=|<=|>|<|=)?\s*(v?[0-9A-Za-z.-]+)$", token)
        if m:
            op = m.group(1) or "="
            raw_ver = m.group(2)
            # Handle wildcards in versions like 1.x or 1.2.*
            if ".x" in raw_ver.lower() or ".*" in raw_ver:
                base = re.sub(r"(\.[xX*])+", "", raw_ver)
                parts = [int(p) for p in base.split(".") if p.isdigit()]
                if len(parts) == 1:
                    comparators.append(Comparator(">=", SemVer(parts[0], 0, 0)))
                    comparators.append(Comparator("<", SemVer(parts[0] + 1, 0, 0)))
                elif len(parts) == 2:
                    comparators.append(Comparator(">=", SemVer(parts[0], parts[1], 0)))
                    comparators.append(Comparator("<", SemVer(parts[0], parts[1] + 1, 0)))
                continue

            v = SemVer.parse(raw_ver)
            if v:
                comparators.append(Comparator(op, v))
            continue

    return comparators


def semver_satisfies(version: str | SemVer, specifier: str) -> bool:
    """Check whether a given SemVer version satisfies an npm range specifier."""
    v = version if isinstance(version, SemVer) else SemVer.parse(version)
    if not v:
        return False

    spec = specifier.strip()
    if not spec or spec in ("*", "x", "X", "latest"):
        # Matches all non-prerelease versions (or prereleases if v has prerelease matching)
        return not v.is_prerelease()

    # Split by OR condition (||)
    or_clauses = [clause.strip() for clause in spec.split("||") if clause.strip()]
    if not or_clauses:
        return True

    for clause in or_clauses:
        comps = _parse_simple_range(clause)
        if not comps:
            return True
        # Target version must satisfy all comparators in this AND clause
        if all(comp.satisfies(v) for comp in comps):
            # By SemVer rules, prerelease is not satisfied unless the comparator explicitly references prerelease
            if v.is_prerelease():
                has_prerelease_in_clause = any(comp.ver.is_prerelease() for comp in comps)
                if has_prerelease_in_clause:
                    return True
            else:
                return True

    return False


def extract_pinned_version(specifier: str) -> Optional[str]:
    """If specifier is an exact pin (e.g. '1.2.3', '=1.2.3', 'v1.2.3'), returns that version."""
    s = specifier.strip()
    if re.match(r"^=?\s*v?\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$", s):
        parsed = SemVer.parse(s.lstrip("=").strip())
        return parsed.canonical if parsed else None
    return None


def find_latest_compatible(available_versions: List[str], specifier: str) -> Optional[str]:
    """Find the highest SemVer among available_versions that satisfies specifier."""
    candidates: List[SemVer] = []
    for ver_str in available_versions:
        v = SemVer.parse(ver_str)
        if v and semver_satisfies(v, specifier):
            candidates.append(v)
    if not candidates:
        return None
    candidates.sort()
    return str(candidates[-1])


def find_latest_version(available_versions: List[str], allow_prerelease: bool = False) -> Optional[str]:
    """Find the highest stable SemVer among available_versions."""
    valid: List[SemVer] = []
    for ver_str in available_versions:
        v = SemVer.parse(ver_str)
        if v:
            if allow_prerelease or not v.is_prerelease():
                valid.append(v)
    if not valid:
        return None
    valid.sort()
    return str(valid[-1])
