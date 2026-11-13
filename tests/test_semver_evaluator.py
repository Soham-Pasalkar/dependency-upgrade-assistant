"""Unit tests for the pure-Python SemVer evaluator and fail-closed range validation."""
from __future__ import annotations

import pytest

from src.analysis.semver_evaluator import (
    SemVer,
    extract_pinned_version,
    find_latest_compatible,
    find_latest_version,
    is_supported_semver_range,
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


# ---------------------------------------------------------------------------
# Fail-Closed Tests for Unsupported and Malformed npm Specifications
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "unsupported_spec,expected_keyword",
    [
        ("workspace:*", "workspace"),
        ("workspace:^1.2.0", "workspace"),
        ("file:../local-package", "local filesystem"),
        ("file:./tarballs/pkg.tgz", "local filesystem"),
        ("link:../other-dir", "symlink"),
        ("github:user/repository", "GitHub"),
        ("gitlab:team/repo", "GitLab"),
        ("bitbucket:org/repo", "Bitbucket"),
        ("git+https://github.com/foo/bar.git", "git"),
        ("https://registry.example.com/custom.tgz", "URL"),
        ("npm:other-package@^1.0.0", "npm package alias"),
        ("not-a-version", "unsupported or unparseable"),
        (">>>1.0.0", "unsupported or unparseable"),
    ],
)
def test_unsupported_npm_specifiers_fail_closed(unsupported_spec, expected_keyword):
    # 1. is_supported_semver_range must return False with informative reason
    is_supported, reason = is_supported_semver_range(unsupported_spec)
    assert not is_supported
    assert expected_keyword.lower() in (reason or "").lower()

    # 2. semver_satisfies must return False (never True!)
    assert not semver_satisfies("1.0.0", unsupported_spec)
    assert not semver_satisfies("2.0.0", unsupported_spec)

    # 3. find_latest_compatible must return None
    assert find_latest_compatible(["1.0.0", "2.0.0"], unsupported_spec) is None


def test_prerelease_isolation_rules():
    # Normal range ^1.0.0 must NOT match 1.1.0-alpha.1
    assert not semver_satisfies("1.1.0-alpha.1", "^1.0.0")
    # Explicit prerelease comparator with matching major.minor.patch matches
    assert semver_satisfies("1.1.0-alpha.1", ">=1.1.0-alpha.0 <1.1.0")
    assert not semver_satisfies("1.2.0-beta.1", ">=1.1.0-alpha.0 <1.1.0")


def test_valid_wildcard_specifiers_supported():
    for wild in ["*", "x", "X", "latest", ""]:
        is_supported, reason = is_supported_semver_range(wild)
        assert is_supported
        assert reason is None
        assert semver_satisfies("1.5.0", wild)
        # Prereleases excluded from wildcards
        assert not semver_satisfies("1.5.0-beta.1", wild)
