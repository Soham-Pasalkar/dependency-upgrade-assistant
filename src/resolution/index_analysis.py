"""Per-version view of a PEP 691 index for the target (used by the supervisor only).

A version "has a compatible wheel" when a non-yanked wheel's tags intersect the target tags and its
``requires-python`` admits the target. It "has an admitted sdist" when a non-yanked sdist's
``requires-python`` admits the target. Legacy formats pip cannot install (.egg, .exe, ...) are
ignored. An unparseable ``requires-python`` is treated as not admitting the target.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import Tag
from packaging.utils import InvalidSdistFilename, InvalidWheelFilename, parse_sdist_filename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from src.resolution.metadata_client import ProjectIndex


@dataclass
class VersionFiles:
    compatible_wheel: bool = False
    admitted_sdist: bool = False
    any_file: bool = False
    any_admitted: bool = False
    all_yanked: bool = True
    excluded_by_python: set = field(default_factory=set)  # requires-python strings that exclude the target


def admits(requires_python: Optional[str], target: Version) -> bool:
    if requires_python is None:
        return True
    try:
        return SpecifierSet(requires_python).contains(target, prereleases=True)
    except InvalidSpecifier:
        return False


def summarize(index: ProjectIndex, tags: FrozenSet[Tag], target: Version) -> Dict[Version, VersionFiles]:
    versions: Dict[Version, VersionFiles] = {}
    for f in index.files:
        try:
            if f.filename.endswith(".whl"):
                _name, version, _build, file_tags = parse_wheel_filename(f.filename)
                kind = "wheel"
            else:
                _name, version = parse_sdist_filename(f.filename)
                file_tags, kind = frozenset(), "sdist"
        except (InvalidWheelFilename, InvalidSdistFilename, InvalidVersion):
            continue
        info = versions.setdefault(version, VersionFiles())
        info.any_file = True
        if not f.yanked:
            info.all_yanked = False
        ok = admits(f.requires_python, target)
        if not ok and f.requires_python:
            info.excluded_by_python.add(f.requires_python)
        if ok:
            info.any_admitted = True
        if f.yanked or not ok:
            continue
        if kind == "wheel" and file_tags & tags:
            info.compatible_wheel = True
        elif kind == "sdist":
            info.admitted_sdist = True
    return versions


def satisfying(versions: Dict[Version, VersionFiles], spec: SpecifierSet, allow_prereleases: Optional[bool] = None):
    """Versions in the index that satisfy ``spec`` (prereleases per PEP 440 unless overridden)."""
    return [v for v in versions if spec.contains(v, prereleases=allow_prereleases)]
