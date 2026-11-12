"""Server-side approval enforcement shared by validation, workspace apply and export (FR-16)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.services.proposal_registry import ProposalRegistry, StoredProposal
from src.services.run_store import ApprovalRecord, ProposalBinding, RunStore, sha256_text


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
    if binding is None and registry.get(proposal_id) is None:
        raise ApprovalError(404, "not_found", "This plan was not found. It may have expired; build it again.")
    if binding is None:
        raise ApprovalError(
            409,
            "unbound_proposal",
            "This plan was not built from an analysis on this server. Build the plan again from your analysis.",
        )
    stored = registry.get(proposal_id)
    if stored is None:
        raise ApprovalError(410, "proposal_expired", "This plan has expired. Build it again to continue.")
    approval = store.get_approval(proposal_id)
    if approval is None:
        raise ApprovalError(409, "approval_required", f"Approve this plan before you {action}.")
    current = sha256_text(stored.proposal.unified_diff)
    if current != binding.diff_sha256 or approval.diff_sha256 != binding.diff_sha256:
        raise ApprovalError(409, "approval_mismatch", "The plan changed after it was approved. Review and approve it again.")
    return ApprovedProposal(stored=stored, binding=binding, approval=approval)
