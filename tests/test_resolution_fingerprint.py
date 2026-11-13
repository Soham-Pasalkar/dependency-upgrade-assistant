"""Supervisor-computed per-role fingerprints (FR-27c, NFR-14; M12 criterion 6 unit part, criterion 14b)."""
from __future__ import annotations

import pytest

from src.resolution import fingerprint as fp
from src.resolution.models import ResolutionRole
from src.resolution.python_input import ConstraintsFile

IMAGE = "python:3.11-slim@sha256:" + "1" * 64
C1 = ConstraintsFile(path="c.txt", sha256="2" * 64, content="numpy<2\n")


def env(**overrides):
    base = fp.resolution_environment("amd64", image=IMAGE)
    return fp.ResolutionEnvironment(**{**base.__dict__, **overrides})


def proposed(e=None, **kw):
    args = dict(role=ResolutionRole.PROPOSED, env=e or env(), proposal_id="prop_1", original_content="a==1\n",
                constraints=(C1,), proposed_content="a==2\n", unified_diff="diff-1")
    args.update(kw)
    return fp.compute_fingerprint(**args)


def original(e=None, **kw):
    args = dict(role=ResolutionRole.ORIGINAL, env=e or env(), proposal_id="prop_1", original_content="a==1\n",
                constraints=(C1,))
    args.update(kw)
    return fp.compute_fingerprint(**args)


def test_deterministic_and_hex():
    assert proposed() == proposed()
    assert len(proposed()) == 64 and int(proposed(), 16) >= 0


def test_roles_have_separate_fingerprints():
    assert original() != proposed()


def test_constraint_order_does_not_matter():
    c2 = ConstraintsFile(path="b.txt", sha256="3" * 64, content="")
    assert proposed(constraints=(C1, c2)) == proposed(constraints=(c2, C1))


@pytest.mark.parametrize(
    "change",
    [
        {"proposal_id": "prop_2"},
        {"original_content": "a==1.0\n"},
        {"proposed_content": "a==3\n"},
        {"unified_diff": "diff-2"},
        {"constraints": ()},
        {"constraints": (ConstraintsFile(path="c.txt", sha256="9" * 64, content="numpy<2\n"),)},
        {"constraints": (ConstraintsFile(path="d.txt", sha256="2" * 64, content="numpy<2\n"),)},
    ],
)
def test_every_input_changes_the_proposed_fingerprint(change):
    assert proposed(**change) != proposed()


@pytest.mark.parametrize(
    "override",
    [
        {"image": "python:3.11-slim@sha256:" + "4" * 64},
        {"docker_arch": "arm64"},
        {"target_python": "cp312"},
        {"target_platform": "macos"},
        {"pip_args": fp.PIP_POLICY_ARGS[:-1]},
        {"version_policy": (("prereleases", "allow"),)},
        {"allowlist_version": "egress-allowlist/2"},
        {"policy_version": "dua-resolution/2"},
    ],
)
def test_every_environment_element_changes_both_fingerprints(override):
    changed = env(**override)
    assert proposed(e=changed) != proposed()
    assert original(e=changed) != original()


def test_original_ignores_proposed_inputs():
    assert fp.compute_fingerprint(ResolutionRole.ORIGINAL, env(), "prop_1", "a==1\n", (C1,), "x", "y") == original()


def test_proposed_requires_proposed_inputs():
    with pytest.raises(ValueError):
        fp.compute_fingerprint(ResolutionRole.PROPOSED, env(), "prop_1", "a==1\n")


def test_common_digest_is_shared_by_both_roles_and_sensitive_to_environment():
    assert fp.common_digest(env(), "prop_1", (C1,)) == fp.common_digest(env(), "prop_1", (C1,))
    assert fp.common_digest(env(docker_arch="arm64"), "prop_1", (C1,)) != fp.common_digest(env(), "prop_1", (C1,))


def test_inputs_expose_no_resource_limits_or_outputs():
    payload = fp.fingerprint_inputs(ResolutionRole.PROPOSED, env(), "prop_1", "a", (C1,), "b", "d")
    flat = str(payload)
    for excluded in ("timeout", "memory", "pids", "tmpfs", "upload", "introduced", "status"):
        assert excluded not in flat


@pytest.mark.parametrize("arch,expected", [("x86_64", "amd64"), ("AMD64", "amd64"), ("aarch64", "arm64"),
                                           ("arm64", "arm64"), ("s390x", "s390x")])
def test_architecture_is_normalized(arch, expected):
    assert fp.resolution_environment(arch, image=IMAGE).docker_arch == expected


def test_environment_fails_closed():
    with pytest.raises(fp.ResolutionEnvironmentError):
        fp.resolution_environment("", image=IMAGE)
    with pytest.raises(fp.ResolutionEnvironmentError):
        fp.resolution_environment("amd64", image="python:3.11-slim")


def test_default_environment_uses_pinned_validation_image():
    e = fp.resolution_environment("amd64")
    assert e.image.startswith("python:3.11-slim@sha256:")
    assert "--only-binary=:all:" in e.pip_args
