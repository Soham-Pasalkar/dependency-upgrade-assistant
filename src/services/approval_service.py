"""Server-side approval enforcement shared by validation, workspace apply and export (FR-16)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import time

from src.api.config import settings
from src.services.proposal_registry import ProposalLifecycleState, ProposalRegistry, StoredProposal
from src.sandbox.validation_policy import ValidationPolicy, ValidationPolicyError, policy_for_proposal
from src.resolution.models import ResolutionStatus
from src.services.run_store import ApprovalRecord, ProposalBinding, RunStore, StoredResolution, sha256_text


class ApprovalError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass
class ApprovedProposal:
    stored: StoredProposal
    binding: ProposalBinding
    approval: ApprovalRecord
    policy: ValidationPolicy
    resolution: Optional[StoredResolution] = None


_NOT_VERIFIED = {
    "CONFLICT": "pip found conflicting requirements in the proposed dependency set.",
    "UNSUPPORTED": "this plan contains requirements that M12 cannot resolve (see the compatibility details).",
    "RESOLUTION_FAILED": "the compatibility check did not produce verifiable evidence.",
    "RESOLVER_UNAVAILABLE": "the isolated resolver (Docker) was unavailable.",
    "UNKNOWN": "no current compatibility evidence exists for this exact plan. Run the compatibility check.",
}


def require_compatible_resolution(proposal_id: str, store: RunStore, registry: ProposalRegistry,
                                  action: str = "continue") -> StoredResolution:
    """FR-28: only current, supervisor-verified COMPATIBLE evidence for the proposed set passes.

    Status and fingerprint come from server-held evidence re-bound to today's plan, image and policy;
    nothing the client sends is consulted. There is no override.
    """
    from src.services.job_service import JobServiceError, resolution_state  # import cycle: job_service uses us

    try:
        state = resolution_state(proposal_id, store, registry)
    except JobServiceError as exc:
        raise ApprovalError(exc.status_code, exc.code, exc.message)
    if state.status != ResolutionStatus.COMPATIBLE or state.proposed is None:
        detail = _NOT_VERIFIED.get(state.status.value, "compatibility is not verified.")
        raise ApprovalError(
            409, "compatibility_not_verified",
            f"Compatibility not verified ({state.status.value}): {detail} You cannot {action} until the "
            "proposed dependency set resolves.",
        )
    return state.proposed


def is_expired(binding: ProposalBinding, now: Optional[float] = None) -> bool:
    return (now or time.time()) - binding.created_at > settings.PLAN_RETENTION_HOURS * 3600


def ensure_registered(proposal_id: str, store: RunStore, registry: ProposalRegistry) -> Optional[StoredProposal]:
    """Return the live proposal, restoring it from SQLite after a restart or in-memory expiry.

    Restoration only happens for plans built from a server-held analysis, within PLAN_RETENTION_HOURS,
    and keeps the consumed state so an applied plan cannot be applied again.
    """
    binding = store.get_binding(proposal_id)
    if binding is not None and is_expired(binding):
        return None
    live = registry.get(proposal_id)
    if live is not None or binding is None:
        return live
    proposal = store.get_stored_proposal(proposal_id)
    if proposal is None:
        return None
    state = ProposalLifecycleState.CONSUMED if binding.consumed else ProposalLifecycleState.AVAILABLE
    return registry.restore(proposal_id, proposal, state)


def require_approved_proposal(
    proposal_id: Optional[str],
    store: RunStore,
    registry: ProposalRegistry,
    action: str = "continue",
) -> ApprovedProposal:
    """Return the approved proposal or raise ApprovalError. Never trusts client-held state."""
    if not proposal_id:
        raise ApprovalError(409, "approval_required", f"Approve an upgrade plan before you {action}.")
    binding = store.get_binding(proposal_id)
    stored = ensure_registered(proposal_id, store, registry)
    if binding is None and stored is None:
        raise ApprovalError(404, "not_found", "This plan was not found. It may have expired; build it again.")
    if binding is None:
        raise ApprovalError(
            409,
            "unbound_proposal",
            "This plan was not built from an analysis on this server. Build the plan again from your analysis.",
        )
    if stored is None:
        raise ApprovalError(410, "proposal_expired", "This plan has expired. Build it again to continue.")
    approval = store.get_approval(proposal_id)
    if approval is None:
        raise ApprovalError(409, "approval_required", f"Approve this plan before you {action}.")
    current = sha256_text(stored.proposal.unified_diff)
    if current != binding.diff_sha256 or approval.diff_sha256 != binding.diff_sha256:
        raise ApprovalError(409, "approval_mismatch", "The plan changed after it was approved. Review and approve it again.")
    try:
        policy = policy_for_proposal(stored.proposal)
    except ValidationPolicyError as exc:
        raise ApprovalError(409, "validation_unavailable", str(exc))
    if not approval.validation_fingerprint or approval.validation_fingerprint != policy.fingerprint:
        raise ApprovalError(
            409,
            "validation_settings_changed",
            "The validation settings changed after this plan was approved. Review and approve it again.",
        )
    resolution = require_compatible_resolution(proposal_id, store, registry, action)
    if not approval.resolution_fingerprint or approval.resolution_fingerprint != resolution.fingerprint:
        raise ApprovalError(
            409,
            "compatibility_changed",
            "The compatibility evidence for this plan changed after it was approved, or the approval predates "
            "compatibility checks. Review and approve it again.",
        )
    return ApprovedProposal(stored=stored, binding=binding, approval=approval, policy=policy, resolution=resolution)
