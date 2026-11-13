"""Server-controlled validation policy bound into approval (M11, audit finding F13).

The validation operation — container image, test-discovery rules, dependency installation mode and
resource limits — is derived on the server from the approved plan and trusted configuration. Clients
cannot supply a test command or an image. The policy's SHA-256 fingerprint is recorded with the
approval and re-checked before validation and export, so any change to the effective validation
configuration invalidates the approval.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from src.api.config import settings
from src.sandbox.container_config import sanitize_resource_limits
from src.sandbox.image_catalog import get_default_image_for_ecosystem, get_pinned_image_reference

POLICY_VERSION = "dua-validation/1"

# Fixed discovery rules (see src/sandbox/test_discovery.py). Part of the fingerprint so a change to the
# rules requires fresh approval.
TEST_DISCOVERY_RULES = {
    "python": "pytest -q when tests/ or test_*.py or *_test.py exist; otherwise NO_TESTS_FOUND",
    "npm": "npm test when package.json defines a non-placeholder scripts.test; otherwise NO_TESTS_FOUND",
}


class ValidationPolicyError(ValueError):
    """The server cannot construct a trusted validation policy (fails closed)."""


@dataclass(frozen=True)
class ValidationPolicy:
    policy_version: str
    ecosystem: str
    manifest_path: str
    image: str  # digest-pinned reference
    network: str
    dependency_install: str
    test_discovery: str
    timeout_seconds: int
    memory_limit_mb: int
    cpu_limit: float
    pids_limit: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def image_tag(self) -> str:
        return self.image.split("@", 1)[0]


def _ecosystem_of(manifest_type: Optional[str], ecosystem: Optional[str]) -> str:
    value = (ecosystem or "").lower()
    if "npm" in value or "node" in value or (manifest_type or "").lower() == "package.json":
        return "npm"
    return "python"


def derive_validation_policy(
    manifest_type: Optional[str],
    ecosystem: Optional[str],
    manifest_path: Optional[str],
) -> ValidationPolicy:
    """Build the only validation configuration the server will run for a plan."""
    eco = _ecosystem_of(manifest_type, ecosystem)
    tag = get_default_image_for_ecosystem(eco)
    try:
        image = get_pinned_image_reference(tag)
    except ValueError as exc:
        raise ValidationPolicyError(f"No pinned image is configured for {eco} validation.") from exc
    mem, cpus, pids, timeout = sanitize_resource_limits(
        memory_mb=settings.DOCKER_MAX_MEMORY_MB,
        cpu_limit=settings.DOCKER_MAX_CPUS,
        pids_limit=None,
        timeout_seconds=settings.DOCKER_TIMEOUT_SECONDS,
    )
    return ValidationPolicy(
        policy_version=POLICY_VERSION,
        ecosystem=eco,
        manifest_path=manifest_path or ("package.json" if eco == "npm" else "requirements.txt"),
        image=image,
        network="none",
        dependency_install="offline-staged",
        test_discovery=TEST_DISCOVERY_RULES[eco],
        timeout_seconds=timeout,
        memory_limit_mb=mem,
        cpu_limit=float(cpus),
        pids_limit=pids,
    )


def policy_for_proposal(proposal) -> ValidationPolicy:
    eco = getattr(proposal.ecosystem, "value", proposal.ecosystem)
    return derive_validation_policy(proposal.manifest_type, eco, proposal.file_path)
