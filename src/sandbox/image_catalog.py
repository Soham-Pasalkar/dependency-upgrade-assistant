"""Immutable allowlisted catalog of approved base container images with pinned SHA-256 digests."""
from __future__ import annotations

from typing import Dict, Optional, Set

# Explicitly verified immutable SHA-256 digests from official registry manifests
PINNED_IMAGE_DIGESTS: Dict[str, str] = {
    "python:3.11-slim": "sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535",
    "python:3.12-slim": "sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b",
    "node:20-slim": "sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0",
    "node:22-slim": "sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d",
}

PYTHON_ALLOWLIST: Set[str] = {
    "python:3.11-slim",
    "python:3.12-slim",
}

NODE_ALLOWLIST: Set[str] = {
    "node:20-slim",
    "node:22-slim",
}

ALL_ALLOWLISTED_IMAGES: Set[str] = PYTHON_ALLOWLIST | NODE_ALLOWLIST

DEFAULT_PYTHON_IMAGE = "python:3.11-slim"
DEFAULT_NODE_IMAGE = "node:20-slim"


def is_image_allowlisted(image: str) -> bool:
    """Check if the provided container image tag or digest reference is approved."""
    if not image or not isinstance(image, str):
        return False
    cleaned = image.strip().lower()
    # Support checking base tag or digest reference
    base_tag = cleaned.split("@")[0]
    return base_tag in ALL_ALLOWLISTED_IMAGES


def validate_image_or_raise(image: str) -> str:
    """Validate that image is in the authorized catalog; raise ValueError if not."""
    if not image or not isinstance(image, str):
        raise ValueError("Container image name must be a non-empty string.")
    cleaned = image.strip().lower()
    base_tag = cleaned.split("@")[0]
    if base_tag not in ALL_ALLOWLISTED_IMAGES:
        raise ValueError(
            f"Unauthorized container image: '{image}'. "
            f"Image must be one of approved allowlist: {sorted(list(ALL_ALLOWLISTED_IMAGES))}"
        )
    return base_tag


def get_image_digest(image_tag: str) -> Optional[str]:
    """Retrieve the pinned immutable SHA-256 digest for an approved tag."""
    base_tag = validate_image_or_raise(image_tag)
    return PINNED_IMAGE_DIGESTS.get(base_tag)


def get_pinned_image_reference(image_tag: str) -> str:
    """Generate the full digest-pinned container reference (e.g. image@sha256:...)."""
    base_tag = validate_image_or_raise(image_tag)
    digest = PINNED_IMAGE_DIGESTS.get(base_tag)
    if not digest:
        raise ValueError(f"No pinned digest available for image: '{base_tag}'.")
    return f"{base_tag}@{digest}"


def get_default_image_for_ecosystem(ecosystem_or_manifest: str) -> str:
    """Determine default allowlisted image based on ecosystem or manifest type."""
    clean = (ecosystem_or_manifest or "").strip().lower()
    if "npm" in clean or "node" in clean or clean.endswith("package.json"):
        return DEFAULT_NODE_IMAGE
    return DEFAULT_PYTHON_IMAGE
