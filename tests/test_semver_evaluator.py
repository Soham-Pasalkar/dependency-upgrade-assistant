"""Unit tests for the pure-Python SemVer evaluator."""
from __future__ import annotations

import pytest

from src.analysis.semver_evaluator import (
    SemVer,
    extract_pinned_version,
    find_latest_compatible,
    find_latest_version,
    semver_satisfies,
)


def test_semver_parsing_and_equality():
    v1 = SemVer.parse("1.2.3")
    v2 = SemVer.parse("v1.2.3")
    assert v1 is not None
    assert v2 is not None
    assert v1 == v2
    assert v1.major == 1
    assert v1.minor == 2
    assert v1.patch == 3
    assert not v1.is_prerelease()

    v_pre = SemVer.parse("2.0.0-beta.1")
    assert v_pre is not None
    assert v_pre.is_prerelease()
    assert v_pre.prerelease == "beta.1"


def test_semver_precedence():
    assert SemVer.parse("1.0.0") < SemVer.parse("2.0.0")
    assert SemVer.parse("1.1.0") < SemVer.parse("1.2.0")
    assert SemVer.parse("1.1.1") < SemVer.parse("1.1.2")
    # Prerelease has lower precedence than normal version
    assert SemVer.parse("1.0.0-alpha") < SemVer.parse("1.0.0")
    assert SemVer.parse("1.0.0-alpha") < SemVer.parse("1.0.0-beta")
    assert SemVer.parse("1.0.0-beta.2") < SemVer.parse("1.0.0-beta.11")


def test_semver_satisfies_caret():
    # ^1.2.3 allows >=1.2.3 <2.0.0
    assert semver_satisfies("1.2.3", "^1.2.3")
    assert semver_satisfies("1.9.9", "^1.2.3")
    assert not semver_satisfies("1.2.2", "^1.2.3")
    assert not semver_satisfies("2.0.0", "^1.2.3")

    # ^0.2.3 allows >=0.2.3 <0.3.0
    assert semver_satisfies("0.2.3", "^0.2.3")
    assert semver_satisfies("0.2.9", "^0.2.3")
    assert not semver_satisfies("0.3.0", "^0.2.3")
    assert not semver_satisfies("0.2.2", "^0.2.3")

    # ^0.0.3 allows >=0.0.3 <0.0.4
    assert semver_satisfies("0.0.3", "^0.0.3")
    assert not semver_satisfies("0.0.4", "^0.0.3")


def test_semver_satisfies_tilde():
    # ~1.2.3 allows >=1.2.3 <1.3.0
    assert semver_satisfies("1.2.3", "~1.2.3")
    assert semver_satisfies("1.2.9", "~1.2.3")
    assert not semver_satisfies("1.3.0", "~1.2.3")
    assert not semver_satisfies("1.2.2", "~1.2.3")

    # ~1.2 allows >=1.2.0 <1.3.0
    assert semver_satisfies("1.2.0", "~1.2")
    assert semver_satisfies("1.2.5", "~1.2")
    assert not semver_satisfies("1.3.0", "~1.2")


def test_semver_satisfies_relational_and_wildcard():
    assert semver_satisfies("2.5.0", ">=2.0.0 <3.0.0")
    assert not semver_satisfies("3.0.0", ">=2.0.0 <3.0.0")
    assert not semver_satisfies("1.9.9", ">=2.0.0 <3.0.0")

    assert semver_satisfies("1.5.0", "*")
    assert semver_satisfies("1.2.4", "1.2.x")
    assert not semver_satisfies("1.3.0", "1.2.x")


def test_semver_satisfies_disjunction():
    assert semver_satisfies("1.5.0", "^1.0.0 || ^2.0.0")
    assert semver_satisfies("2.1.0", "^1.0.0 || ^2.0.0")
    assert not semver_satisfies("3.0.0", "^1.0.0 || ^2.0.0")


def test_extract_pinned_version():
    assert extract_pinned_version("1.2.3") == "1.2.3"
    assert extract_pinned_version("=1.2.3") == "1.2.3"
    assert extract_pinned_version("v1.2.3") == "1.2.3"
    assert extract_pinned_version("^1.2.3") is None
    assert extract_pinned_version("~1.2.3") is None
    assert extract_pinned_version(">=1.2.3") is None


def test_find_latest_compatible_and_latest():
    versions = ["1.0.0", "1.1.0", "1.2.0", "1.2.5", "2.0.0", "2.1.0", "3.0.0-beta.1"]
    assert find_latest_compatible(versions, "^1.0.0") == "1.2.5"
    assert find_latest_compatible(versions, "~1.1.0") == "1.1.0"
    assert find_latest_compatible(versions, ">=2.0.0") == "2.1.0"
    assert find_latest_compatible(versions, "^4.0.0") is None

    assert find_latest_version(versions) == "2.1.0"
    assert find_latest_version(versions, allow_prerelease=True) == "3.0.0-beta.1"
