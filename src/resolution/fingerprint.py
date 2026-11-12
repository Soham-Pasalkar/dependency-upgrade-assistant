"""Supervisor-computed resolution fingerprints (FR-27c, NFR-14; ADR-011).

Each role (original, proposed) has its own fingerprint: the SHA-256 of canonical JSON (sorted keys,
no whitespace) over every resolution input and environment element. Resource limits and timeouts
are excluded because they cannot change a verified result, only whether a run finishes. Upload dates
and the comparison are outputs, never inputs. Only the trusted supervisor calls these functions;
client-supplied fingerprints are never accepted.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple

from src.resolution.models import ResolutionRole
from src.resolution.policy import (
    EGRESS_ALLOWLIST_VERSION,
    PIP_POLICY_ARGS,
    RESOLUTION_POLICY_VERSION,
    TARGET_PLATFORM,
    TARGET_PYTHON,
    VERSION_POLICY,
)
from src.resolution.python_input import ConstraintsFile
from src.sandbox.image_catalog import DEFAULT_PYTHON_IMAGE, get_pinned_image_reference

_ARCH_ALIASES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class ResolutionEnvironmentError(ValueError):
    """The server cannot construct a trusted resolution environment (fails closed)."""


def normalize_arch(arch: str) -> str:
    value = (arch or "").strip().lower()
    if not value:
        raise ResolutionEnvironmentError("The Docker host architecture is unknown.")
    return _ARCH_ALIASES.get(value, value)


@dataclass(frozen=True)
class ResolutionEnvironment:
    image: str  # digest-pinned reference
    docker_arch: str
    target_python: str = TARGET_PYTHON
    target_platform: str = TARGET_PLATFORM
    pip_args: Tuple[str, ...] = PIP_POLICY_ARGS
    version_policy: Tuple[Tuple[str, str], ...] = field(default_factory=lambda: tuple(sorted(VERSION_POLICY.items())))
    allowlist_version: str = EGRESS_ALLOWLIST_VERSION
    policy_version: str = RESOLUTION_POLICY_VERSION
    ecosystem: str = "python"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "ecosystem": self.ecosystem,
            "image": self.image,
            "docker_arch": self.docker_arch,
            "target": {"python": self.target_python, "platform": self.target_platform},
            "pip_args": list(self.pip_args),
            "version_policy": dict(self.version_policy),
            "allowlist_version": self.allowlist_version,
        }


def resolution_environment(docker_arch: str, image: Optional[str] = None) -> ResolutionEnvironment:
    """The only environment the server resolves in: the pinned validation image and fixed policy."""
    if image is None:
        try:
            image = get_pinned_image_reference(DEFAULT_PYTHON_IMAGE)
        except ValueError as exc:
            raise ResolutionEnvironmentError("No pinned image is configured for Python resolution.") from exc
    if "@sha256:" not in image:
        raise ResolutionEnvironmentError("The resolver image must be pinned by digest.")
    return ResolutionEnvironment(image=image, docker_arch=normalize_arch(docker_arch))


def sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _constraints(constraints: Iterable[ConstraintsFile]) -> list:
    return sorted(({"path": c.path, "sha256": c.sha256} for c in constraints), key=lambda c: c["path"])


def common_elements(
    env: ResolutionEnvironment, proposal_id: str, constraints: Iterable[ConstraintsFile]
) -> Dict[str, Any]:
    """Elements shared by both roles. The FR-32b comparison requires them to be identical."""
    return {"environment": env.to_dict(), "proposal_id": proposal_id, "constraints": _constraints(constraints)}


def common_digest(env: ResolutionEnvironment, proposal_id: str, constraints: Iterable[ConstraintsFile]) -> str:
    return hashlib.sha256(_canonical(common_elements(env, proposal_id, constraints)).encode()).hexdigest()


def fingerprint_inputs(
    role: ResolutionRole,
    env: ResolutionEnvironment,
    proposal_id: str,
    original_content: str,
    constraints: Iterable[ConstraintsFile] = (),
    proposed_content: Optional[str] = None,
    unified_diff: Optional[str] = None,
) -> Dict[str, Any]:
    role = ResolutionRole(role)
    payload = common_elements(env, proposal_id, constraints)
    payload["role"] = role.value
    payload["original_sha256"] = sha256_hex(original_content)
    if role == ResolutionRole.PROPOSED:
        if proposed_content is None or unified_diff is None:
            raise ValueError("the proposed role requires the proposed content and the unified diff")
        payload["proposed_sha256"] = sha256_hex(proposed_content)
        payload["diff_sha256"] = sha256_hex(unified_diff)
    return payload


def compute_fingerprint(
    role: ResolutionRole,
    env: ResolutionEnvironment,
    proposal_id: str,
    original_content: str,
    constraints: Iterable[ConstraintsFile] = (),
    proposed_content: Optional[str] = None,
    unified_diff: Optional[str] = None,
) -> str:
    payload = fingerprint_inputs(role, env, proposal_id, original_content, constraints, proposed_content, unified_diff)
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
