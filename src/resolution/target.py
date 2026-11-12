"""Trusted facts about the resolver target environment (ADR-009, ADR-011).

The supervisor evaluates environment markers, ``Requires-Python`` and wheel tags itself, so it needs
the target's exact interpreter and platform. These facts belong to one image digest. They were read
from the pinned image's configuration on Docker Hub on 2026-11-12 (``PYTHON_VERSION=3.11.18`` on
both amd64 and arm64; base layer ``debian.sh ... 'trixie'``, glibc 2.41). If the pinned digest
changes, every supervisor check fails closed until these facts are re-verified and updated, and the
live Docker tests (P10) compare them with the running container.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List

from packaging.tags import Tag, compatible_tags, cpython_tags
from packaging.version import Version

from src.resolution.fingerprint import ResolutionEnvironment, ResolutionEnvironmentError

TARGET_FACTS_IMAGE_DIGEST = "sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535"
TARGET_PYTHON_FULL_VERSION = "3.11.18"
TARGET_GLIBC = (2, 41)
_MACHINES = {"amd64": "x86_64", "arm64": "aarch64"}

# Marker variables whose value depends on the Docker host kernel, which the supervisor cannot know.
UNVERIFIABLE_MARKER_VARIABLES = ("platform_release", "platform_version")


def _checked(env: ResolutionEnvironment) -> str:
    digest = env.image.split("@", 1)[1] if "@" in env.image else ""
    if digest != TARGET_FACTS_IMAGE_DIGEST:
        raise ResolutionEnvironmentError(
            "The resolver image differs from the image whose target facts were verified; "
            "update src/resolution/target.py after re-verifying them."
        )
    machine = _MACHINES.get(env.docker_arch)
    if machine is None:
        raise ResolutionEnvironmentError(f"Unsupported Docker host architecture: {env.docker_arch}")
    return machine


def target_python() -> Version:
    return Version(TARGET_PYTHON_FULL_VERSION)


def target_machine(env: ResolutionEnvironment) -> str:
    return _checked(env)


def marker_environment(env: ResolutionEnvironment) -> Dict[str, str]:
    """PEP 508 environment of the resolver container, excluding host-kernel variables."""
    machine = _checked(env)
    return {
        "implementation_name": "cpython",
        "implementation_version": TARGET_PYTHON_FULL_VERSION,
        "os_name": "posix",
        "platform_machine": machine,
        "platform_python_implementation": "CPython",
        "platform_system": "Linux",
        "python_full_version": TARGET_PYTHON_FULL_VERSION,
        "python_version": ".".join(TARGET_PYTHON_FULL_VERSION.split(".")[:2]),
        "sys_platform": "linux",
    }


def _platforms(machine: str) -> List[str]:
    major, minor = TARGET_GLIBC
    platforms = [f"manylinux_{major}_{m}_{machine}" for m in range(minor, 4, -1)]
    platforms.append(f"manylinux2014_{machine}")
    if machine == "x86_64":
        platforms += [f"manylinux2010_{machine}", f"manylinux1_{machine}"]
    platforms.append(f"linux_{machine}")
    return platforms


def supported_tags(env: ResolutionEnvironment) -> FrozenSet[Tag]:
    """Wheel tags pip accepts inside the resolver container (CPython 3.11, glibc 2.41)."""
    platforms = _platforms(_checked(env))
    tags = set(cpython_tags((3, 11), abis=["cp311"], platforms=platforms))
    tags |= set(compatible_tags((3, 11), interpreter="cp311", platforms=platforms))
    return frozenset(tags)
