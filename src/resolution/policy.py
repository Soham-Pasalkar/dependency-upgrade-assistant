"""Fixed resolution policy shared by the supervisor, the runtime and the fingerprint (ADR-009, ADR-011)."""
from __future__ import annotations

from typing import Dict, Tuple

RESOLUTION_POLICY_VERSION = "dua-resolution/1"
EGRESS_ALLOWLIST_VERSION = "egress-allowlist/1"
EGRESS_ALLOWLIST: Tuple[str, ...] = ("pypi.org", "files.pythonhosted.org")
PYPI_INDEX_URL = "https://pypi.org/simple"

# Target environment: identical to the validation sandbox (pinned python:3.11-slim, Linux).
TARGET_PYTHON = "cp311"
TARGET_PLATFORM = "linux"

# pip arguments that define the resolution semantics. Order matters for the fingerprint.
# Inputs (-r, -c), the proxy address and the report destination are added by the runtime and are
# not policy.
PIP_POLICY_ARGS: Tuple[str, ...] = (
    "install",
    "--dry-run",
    "--ignore-installed",
    "--only-binary=:all:",
    "--isolated",
    "--no-input",
    "--disable-pip-version-check",
    "--index-url",
    PYPI_INDEX_URL,
)

# FR-26: pip defaults, stated explicitly so they are part of the evidence and fingerprint.
VERSION_POLICY: Dict[str, str] = {
    "prereleases": "pip-default: only when a requirement explicitly pins a prerelease",
    "yanked": "pip-default: only for == pins, reported as a warning",
    "hashes": "honoured when present on requirement lines",
}

# FR-32a: fixed server text shown with every COMPATIBLE result and in the Markdown export.
COMPATIBILITY_DISCLOSURE = (
    "Compatible means the complete dependency set resolves successfully for the validation target. "
    "It does not mean the packages have been vetted or are safe. "
    "DUA does not perform malware detection."
)
