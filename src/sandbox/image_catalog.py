"""Immutable allowlisted catalog of approved base container images for sandbox execution."""
from __future__ import annotations

from typing import Set

# Explicitly authorized immutable base images
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
    """Check if the provided container image tag is approved for sandbox execution."""
    if not image or not isinstance(image, str):
        return False
    return image.strip().lower() in ALL_ALLOWLISTED_IMAGES


def validate_image_or_raise(image: str) -> str:
    """Validate that image is in the authorized catalog; raise ValueError if not."""
    if not image or not isinstance(image, str):
        raise ValueError("Container image name must be a non-empty string.")
    cleaned = image.strip().lower()
    if cleaned not in ALL_ALLOWLISTED_IMAGES:
        raise ValueError(
            f"Unauthorized container image: '{image}'. "
            f"Image must be one of approved allowlist: {sorted(list(ALL_ALLOWLISTED_IMAGES))}"
        )
    return cleaned


def get_default_image_for_ecosystem(ecosystem_or_manifest: str) -> str:
    """Determine default allowlisted image based on ecosystem or manifest type."""
    clean = (ecosystem_or_manifest or "").strip().lower()
    if "npm" in clean or "node" in clean or clean.endswith("package.json"):
        return DEFAULT_NODE_IMAGE
    return DEFAULT_PYTHON_IMAGE
