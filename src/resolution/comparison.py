"""FR-32b introduced-package comparison (ADR-011). Informational only.

The comparison never changes a report's status or fingerprint and never affects approval. It is
available only when both role records are COMPATIBLE, each is bound to its own supervisor-held context,
and both contexts share the same common fingerprint elements (image, architecture, target, pip policy,
version policy, allow-list, proposal and constraints). Otherwise it is "comparison unavailable" with
a reason and no package is labelled introduced.
"""
from __future__ import annotations

from typing import List, Optional

from packaging.utils import canonicalize_name

from src.resolution.models import Comparison, ResolutionReport, ResolutionRole, ResolutionStatus
from src.resolution.verifier import ResolutionContext, check_binding


class EvidenceBindingError(Exception):
    """The proposed evidence does not belong to the proposed context. It must not be stored or used."""


def _names(report: ResolutionReport) -> Optional[List[str]]:
    names = [canonicalize_name(p.name) for p in report.packages]
    if not names or len(names) != len(set(names)):
        return None
    return names


def compare(
    original: Optional[ResolutionReport],
    proposed: ResolutionReport,
    original_ctx: ResolutionContext,
    proposed_ctx: ResolutionContext,
) -> Comparison:
    if ResolutionRole(proposed_ctx.role) != ResolutionRole.PROPOSED or ResolutionRole(original_ctx.role) != ResolutionRole.ORIGINAL:
        raise ValueError("contexts must be (original, proposed)")
    mismatch = check_binding(proposed, proposed_ctx)
    if mismatch:
        raise EvidenceBindingError(mismatch)

    def unavailable(reason: str) -> Comparison:
        return Comparison(available=False, reason=reason)

    if proposed.status != ResolutionStatus.COMPATIBLE:
        return unavailable(f"proposed_{proposed.status.value.lower()}")
    if original is None:
        return unavailable("original_missing")
    if check_binding(original, original_ctx):
        return unavailable("evidence_mismatch")
    if original.status != ResolutionStatus.COMPATIBLE:
        return unavailable(f"original_{original.status.value.lower()}")
    # Binding (above) already ties each report's recorded common_digest to its context, so the two
    # halves below are equivalent; both are kept as defence in depth.
    if (original_ctx.common_digest() != proposed_ctx.common_digest()
            or original.inputs.get("common_digest") != proposed.inputs.get("common_digest")):
        return unavailable("environment_mismatch")
    original_names, proposed_names = _names(original), _names(proposed)
    if original_names is None or proposed_names is None:
        return unavailable("invalid_evidence")
    return Comparison(available=True, introduced=sorted(set(proposed_names) - set(original_names)))


def apply_comparison(proposed: ResolutionReport, comparison: Comparison) -> ResolutionReport:
    """Attach the comparison and introduced flags. Status, fingerprint and everything else are unchanged."""
    introduced = set(comparison.introduced) if comparison.available else set()
    packages = [
        p.model_copy(update={"introduced": (canonicalize_name(p.name) in introduced) if comparison.available else None})
        for p in proposed.packages
    ]
    return proposed.model_copy(update={"comparison": comparison, "packages": packages})
