"""Test helper: bind a directly registered proposal to an analysis run and record approval.

Production code only approves plans built from server-held analysis (FR-15/FR-16). Tests that
register proposals directly use this helper to reach the same approved state through the real
RunStore API, so the enforcement code path is still exercised end to end.
"""
from __future__ import annotations

from src.sandbox.validation_policy import policy_for_proposal
from src.services.proposal_registry import get_proposal_registry
from src.services.run_store import StoredManifest, get_run_store, sha256_text


def approve_registered_proposal(proposal_id: str, note: str = "approved in test") -> str:
    stored = get_proposal_registry().get(proposal_id)
    assert stored is not None, "proposal must be registered first"
    store = get_run_store()
    run_id = store.create_run(source="manifest")
    store.add_manifest(StoredManifest(
        run_id=run_id, index=0, path=stored.proposal.file_path, manifest_type=stored.proposal.manifest_type,
        kind="package_json" if stored.proposal.manifest_type == "package.json" else "requirements",
        plannable=True, content=stored.proposal.original_content, analysis=None, recommendations=None,
        proposal_id=None,
    ))
    store.bind_proposal(proposal_id, run_id, 0, stored.proposal.unified_diff)
    # Same as POST /proposals/{id}/approve: the approval covers the server-derived validation policy and
    # the supervisor-verified COMPATIBLE resolution (M12, produced through the real job pipeline).
    from tests.resolution_doubles import resolve_compatible
    evidence = resolve_compatible(proposal_id)
    store.record_approval(
        proposal_id, sha256_text(stored.proposal.unified_diff), note, policy_for_proposal(stored.proposal).fingerprint,
        evidence.fingerprint,
    )
    return run_id
