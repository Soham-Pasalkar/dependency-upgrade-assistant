"""Concurrency and lifecycle state verification for upgrade planning and workspace mutation.

Verifies:
- Test A: Concurrent application (two simultaneous requests for same proposal; exactly one APPLIED, other rejected).
- Test B: Concurrent registry claims (multiple simultaneous claims; exactly one succeeds).
- Test C: Failure before write releases claim back to AVAILABLE; subsequent retry succeeds.
- Test D: Failure during write transitions to ABANDONED (fail-closed; no replay permitted).
- Test E: Repeated application rejected with PROPOSAL_ALREADY_CONSUMED.
- Test F: Independent proposals targeting different manifests apply concurrently without blocking.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from src.models.manifest import Ecosystem
from src.models.plan import (
    PatchIntegrityResult,
    ProposalStatus,
    UpgradePlanRequest,
    UpgradeProposalResponse,
    WorkspaceApplyRequest,
    WorkspaceApplyResponse,
)
from src.models.recommendation import (
    PackageUpgradeRecommendation,
    RecommendationCategory,
    RecommendationPriority,
    ValidationStatus,
)
from src.services.proposal_registry import (
    ProposalLifecycleState,
    ProposalRegistry,
    RegistryCapacityExceededError,
    proposal_registry,
)
from src.services.upgrade_planner import UpgradePlannerService


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure global proposal registry is clean before and after each test."""
    proposal_registry.clear()
    yield
    proposal_registry.clear()


@pytest.fixture
def planner(tmp_path: Path) -> UpgradePlannerService:
    return UpgradePlannerService(workspace_root=tmp_path)


def _make_python_recommendation(
    pkg: str = "requests",
    current: str = "2.28.0",
    candidate: str = "2.32.3",
) -> PackageUpgradeRecommendation:
    return PackageUpgradeRecommendation(
        package_name=pkg,
        ecosystem=Ecosystem.PYTHON,
        current_version=current,
        declared_specifier=f"=={current}",
        candidate_version=candidate,
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.MEDIUM,
        explanation=f"Upgrade {pkg} to {candidate}",
        validation_status=ValidationStatus.VALIDATED,
    )


# ---------------------------------------------------------------------------
# Test A: Concurrent Application (Race between two apply requests)
# ---------------------------------------------------------------------------
def test_concurrent_application_single_winner(planner: UpgradePlannerService, tmp_path: Path):
    """Test A: Verify two simultaneous application requests for the same proposal result in exactly one write."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    rec = _make_python_recommendation()
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="requirements.txt",
            original_content="requests==2.28.0\n",
            recommendations=[rec],
            file_path="requirements.txt",
        )
    )

    barrier = threading.Barrier(2)
    results: List[WorkspaceApplyResponse] = []
    lock = threading.Lock()

    def _apply_worker():
        try:
            barrier.wait(timeout=5.0)
        except Exception:
            pass
        resp = planner.apply_workspace_mutation(
            WorkspaceApplyRequest(
                proposal_id=proposal.proposal_id,
                approved=True,
                target_path="requirements.txt",
            )
        )
        with lock:
            results.append(resp)

    t1 = threading.Thread(target=_apply_worker)
    t2 = threading.Thread(target=_apply_worker)

    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert len(results) == 2

    # Exactly one request must succeed with APPLIED
    applied_count = sum(1 for r in results if r.success and r.status == "APPLIED")
    rejected_count = sum(1 for r in results if not r.success)

    assert applied_count == 1, f"Expected 1 APPLIED response, got {applied_count}"
    assert rejected_count == 1, f"Expected 1 rejected response, got {rejected_count}"

    rejected_resp = next(r for r in results if not r.success)
    assert rejected_resp.status in ("PROPOSAL_ALREADY_IN_PROGRESS", "PROPOSAL_ALREADY_CONSUMED")

    # On-disk content must be updated exactly once to target version
    assert target_file.read_text(encoding="utf-8") == "requests==2.32.3\n"

    # Stored state must be CONSUMED
    stored = planner.proposal_registry.get(proposal.proposal_id)
    assert stored is not None
    assert stored.state == ProposalLifecycleState.CONSUMED


# ---------------------------------------------------------------------------
# Test B: Concurrent Registry Claims
# ---------------------------------------------------------------------------
def test_concurrent_registry_claims():
    """Test B: Verify multiple simultaneous claim_for_application calls allow exactly one winner."""
    reg = ProposalRegistry(ttl_seconds=60.0)
    from src.models.plan import PatchIntegrityResult
    proposal = UpgradeProposalResponse(
        proposal_id="",
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        file_path="requirements.txt",
        plan_items=[],
        total_planned=1,
        original_content="requests==2.28.0\n",
        proposed_content="requests==2.32.3\n",
        unified_diff="",
        integrity=PatchIntegrityResult(
            is_valid=True,
            can_apply=True,
            can_reverse=True,
            syntax_valid=True,
            target_versions_match=True,
            unrelated_dependencies_unchanged=True,
            original_unchanged=True,
        ),
        status=ProposalStatus.READY_FOR_REVIEW,
    )
    prop_id = reg.store(proposal)

    num_threads = 10
    barrier = threading.Barrier(num_threads)
    claim_results = []
    lock = threading.Lock()

    def _claim_worker():
        try:
            barrier.wait(timeout=5.0)
        except Exception:
            pass
        res = reg.claim_for_application(prop_id)
        with lock:
            claim_results.append(res)

    threads = [threading.Thread(target=_claim_worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert len(claim_results) == num_threads

    winners = [c for c in claim_results if c.success]
    losers = [c for c in claim_results if not c.success]

    assert len(winners) == 1, f"Expected exactly 1 claim winner, got {len(winners)}"
    assert winners[0].status == "OK"

    assert len(losers) == num_threads - 1
    for loser in losers:
        assert loser.status == "PROPOSAL_ALREADY_IN_PROGRESS"

    stored = reg.get(prop_id)
    assert stored is not None
    assert stored.state == ProposalLifecycleState.APPLYING


# ---------------------------------------------------------------------------
# Test C: Failure Before Write Releases Claim
# ---------------------------------------------------------------------------
def test_failure_before_write_releases_claim(planner: UpgradePlannerService, tmp_path: Path):
    """Test C: If validation or freshness fails before write, claim is released to AVAILABLE for subsequent retry."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    rec = _make_python_recommendation()
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="requirements.txt",
            original_content="requests==2.28.0\n",
            recommendations=[rec],
            file_path="requirements.txt",
        )
    )

    # 1. Modify file on disk to simulate concurrent conflict (freshness failure)
    target_file.write_text("requests==2.29.0\n", encoding="utf-8")

    # Attempt mutation -> must fail with STALE_BASE_CONTENT
    resp1 = planner.apply_workspace_mutation(
        WorkspaceApplyRequest(
            proposal_id=proposal.proposal_id,
            approved=True,
            target_path="requirements.txt",
        )
    )
    assert resp1.success is False
    assert resp1.status == "STALE_BASE_CONTENT"

    # Verify claim was released back to AVAILABLE
    stored = planner.proposal_registry.get(proposal.proposal_id)
    assert stored is not None
    assert stored.state == ProposalLifecycleState.AVAILABLE

    # 2. Restore file to original expected content
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    # Second attempt must successfully claim and apply
    resp2 = planner.apply_workspace_mutation(
        WorkspaceApplyRequest(
            proposal_id=proposal.proposal_id,
            approved=True,
            target_path="requirements.txt",
        )
    )
    assert resp2.success is True
    assert resp2.status == "APPLIED"
    assert target_file.read_text(encoding="utf-8") == "requests==2.32.3\n"

    # Stored state is now CONSUMED
    stored_final = planner.proposal_registry.get(proposal.proposal_id)
    assert stored_final.state == ProposalLifecycleState.CONSUMED


# ---------------------------------------------------------------------------
# Test D: Failure During Write Fails Closed
# ---------------------------------------------------------------------------
def test_failure_during_write_fails_closed(planner: UpgradePlannerService, tmp_path: Path):
    """Test D: Exception during atomic write fails closed (ABANDONED), preventing replay."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    rec = _make_python_recommendation()
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="requirements.txt",
            original_content="requests==2.28.0\n",
            recommendations=[rec],
            file_path="requirements.txt",
        )
    )

    # Simulate filesystem crash/failure during atomic replace
    with patch("os.replace", side_effect=OSError("Simulated I/O failure during rename")):
        resp1 = planner.apply_workspace_mutation(
            WorkspaceApplyRequest(
                proposal_id=proposal.proposal_id,
                approved=True,
                target_path="requirements.txt",
            )
        )
        assert resp1.success is False
        assert resp1.status == "MUTATION_ERROR"

    # State must be ABANDONED (never released to AVAILABLE)
    stored = planner.proposal_registry.get(proposal.proposal_id)
    assert stored is not None
    assert stored.state == ProposalLifecycleState.ABANDONED

    # Any subsequent attempt must be rejected (cannot replay uncertain write)
    resp2 = planner.apply_workspace_mutation(
        WorkspaceApplyRequest(
            proposal_id=proposal.proposal_id,
            approved=True,
            target_path="requirements.txt",
        )
    )
    assert resp2.success is False
    assert resp2.status == "PROPOSAL_ALREADY_CONSUMED"


# ---------------------------------------------------------------------------
# Test E: Repeated Application Rejected
# ---------------------------------------------------------------------------
def test_repeated_application_rejected(planner: UpgradePlannerService, tmp_path: Path):
    """Test E: Applying a proposal a second time returns PROPOSAL_ALREADY_CONSUMED."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    rec = _make_python_recommendation()
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="requirements.txt",
            original_content="requests==2.28.0\n",
            recommendations=[rec],
            file_path="requirements.txt",
        )
    )

    resp1 = planner.apply_workspace_mutation(
        WorkspaceApplyRequest(
            proposal_id=proposal.proposal_id,
            approved=True,
            target_path="requirements.txt",
        )
    )
    assert resp1.success is True
    assert resp1.status == "APPLIED"

    # Second application attempt
    resp2 = planner.apply_workspace_mutation(
        WorkspaceApplyRequest(
            proposal_id=proposal.proposal_id,
            approved=True,
            target_path="requirements.txt",
        )
    )
    assert resp2.success is False
    assert resp2.status == "PROPOSAL_ALREADY_CONSUMED"


# ---------------------------------------------------------------------------
# Test F: Independent Proposals Do Not Block Each Other
# ---------------------------------------------------------------------------
def test_independent_proposals_apply_concurrently(planner: UpgradePlannerService, tmp_path: Path):
    """Test F: Two proposals for different manifests apply concurrently without blocking each other."""
    py_file = tmp_path / "requirements.txt"
    py_file.write_text("requests==2.28.0\n", encoding="utf-8")

    npm_file = tmp_path / "package.json"
    npm_file.write_text(
        json.dumps({"name": "pkg", "dependencies": {"react": "^18.2.0"}}, indent=2) + "\n",
        encoding="utf-8",
    )

    py_rec = _make_python_recommendation("requests", "2.28.0", "2.32.3")
    npm_rec = PackageUpgradeRecommendation(
        package_name="react",
        ecosystem=Ecosystem.NPM,
        current_version="18.2.0",
        declared_specifier="^18.2.0",
        candidate_version="18.3.1",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.MEDIUM,
        explanation="Upgrade react",
        validation_status=ValidationStatus.VALIDATED,
    )

    prop_py = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="requirements.txt",
            original_content="requests==2.28.0\n",
            recommendations=[py_rec],
            file_path="requirements.txt",
        )
    )
    prop_npm = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="package.json",
            original_content=npm_file.read_text(encoding="utf-8"),
            recommendations=[npm_rec],
            file_path="package.json",
        )
    )

    barrier = threading.Barrier(2)
    results = {}

    def _worker_py():
        barrier.wait()
        results["py"] = planner.apply_workspace_mutation(
            WorkspaceApplyRequest(
                proposal_id=prop_py.proposal_id,
                approved=True,
                target_path="requirements.txt",
            )
        )

    def _worker_npm():
        barrier.wait()
        results["npm"] = planner.apply_workspace_mutation(
            WorkspaceApplyRequest(
                proposal_id=prop_npm.proposal_id,
                approved=True,
                target_path="package.json",
            )
        )

    t1 = threading.Thread(target=_worker_py)
    t2 = threading.Thread(target=_worker_npm)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert results["py"].success is True
    assert results["py"].status == "APPLIED"
    assert py_file.read_text(encoding="utf-8") == "requests==2.32.3\n"

    assert results["npm"].success is True
    assert results["npm"].status == "APPLIED"
    assert '"react": "^18.3.1"' in npm_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Eviction Safety and Post-Write Lifecycle Integrity Tests
# ---------------------------------------------------------------------------
def test_active_proposal_protected_from_eviction():
    """Verify proposal in APPLYING state is NEVER evicted when registry reaches capacity."""
    reg = ProposalRegistry(max_entries=2)

    def _make_dummy_proposal():
        return UpgradeProposalResponse(
            proposal_id="",
            manifest_type="requirements.txt",
            ecosystem=Ecosystem.PYTHON,
            file_path="requirements.txt",
            plan_items=[],
            total_planned=1,
            original_content="requests==2.28.0\n",
            proposed_content="requests==2.32.3\n",
            unified_diff="",
            integrity=PatchIntegrityResult(
                is_valid=True,
                can_apply=True,
                can_reverse=True,
                syntax_valid=True,
                target_versions_match=True,
                unrelated_dependencies_unchanged=True,
                original_unchanged=True,
            ),
            status=ProposalStatus.READY_FOR_REVIEW,
        )

    id1 = reg.store(_make_dummy_proposal())
    id2 = reg.store(_make_dummy_proposal())

    # Claim id1 -> state becomes APPLYING
    claim = reg.claim_for_application(id1)
    assert claim.success is True

    # Now store id3. Capacity is 2. id1 is APPLYING, id2 is AVAILABLE.
    # Must evict id2, NOT id1!
    id3 = reg.store(_make_dummy_proposal())

    stored1 = reg.get(id1)
    assert stored1 is not None
    assert stored1.state == ProposalLifecycleState.APPLYING

    assert reg.get(id2) is None  # evicted
    assert reg.get(id3) is not None


def test_capacity_exhaustion_when_all_proposals_applying():
    """Verify RegistryCapacityExceededError is raised deterministically when all entries are actively claimed."""
    reg = ProposalRegistry(max_entries=2)

    def _make_dummy_proposal():
        return UpgradeProposalResponse(
            proposal_id="",
            manifest_type="requirements.txt",
            ecosystem=Ecosystem.PYTHON,
            file_path="requirements.txt",
            plan_items=[],
            total_planned=1,
            original_content="requests==2.28.0\n",
            proposed_content="requests==2.32.3\n",
            unified_diff="",
            integrity=PatchIntegrityResult(
                is_valid=True,
                can_apply=True,
                can_reverse=True,
                syntax_valid=True,
                target_versions_match=True,
                unrelated_dependencies_unchanged=True,
                original_unchanged=True,
            ),
            status=ProposalStatus.READY_FOR_REVIEW,
        )

    id1 = reg.store(_make_dummy_proposal())
    id2 = reg.store(_make_dummy_proposal())

    reg.claim_for_application(id1)
    reg.claim_for_application(id2)

    # Both are APPLYING. No evictable entries exist.
    with pytest.raises(RegistryCapacityExceededError, match="all entries are actively in progress"):
        reg.store(_make_dummy_proposal())

    # Verify active claims remain intact
    assert reg.get(id1) is not None
    assert reg.get(id1).state == ProposalLifecycleState.APPLYING
    assert reg.get(id2) is not None
    assert reg.get(id2).state == ProposalLifecycleState.APPLYING


def test_completion_failure_fails_closed_and_rejects_applied(planner: UpgradePlannerService, tmp_path: Path):
    """Verify that if complete_application fails after file write, API fails closed and never returns APPLIED."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    rec = _make_python_recommendation()
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="requirements.txt",
            original_content="requests==2.28.0\n",
            recommendations=[rec],
            file_path="requirements.txt",
        )
    )

    # Force complete_application to return False
    with patch.object(planner.proposal_registry, "complete_application", return_value=False):
        resp = planner.apply_workspace_mutation(
            WorkspaceApplyRequest(
                proposal_id=proposal.proposal_id,
                approved=True,
                target_path="requirements.txt",
            )
        )
        assert resp.success is False
        assert resp.status == "LIFECYCLE_ERROR"
        assert resp.status != "APPLIED"

    # Verify proposal is marked ABANDONED (fail-closed)
    stored = planner.proposal_registry.get(proposal.proposal_id)
    assert stored is not None
    assert stored.state == ProposalLifecycleState.ABANDONED

    # Any subsequent attempt must be rejected
    resp2 = planner.apply_workspace_mutation(
        WorkspaceApplyRequest(
            proposal_id=proposal.proposal_id,
            approved=True,
            target_path="requirements.txt",
        )
    )
    assert resp2.success is False
    assert resp2.status == "PROPOSAL_ALREADY_CONSUMED"


def test_post_write_verification_exception_abandons_proposal(planner: UpgradePlannerService, tmp_path: Path):
    """Verify that an exception during post-write read-back transitions proposal to ABANDONED."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    rec = _make_python_recommendation()
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="requirements.txt",
            original_content="requests==2.28.0\n",
            recommendations=[rec],
            file_path="requirements.txt",
        )
    )

    # Simulate read-back failure: pre-write read succeeds, post-write read raises
    real_read_text = Path.read_text
    read_calls = [0]

    def _mock_read_text(self, *args, **kwargs):
        read_calls[0] += 1
        if read_calls[0] == 1:
            # Pre-write freshness check
            return real_read_text(self, *args, **kwargs)
        # Post-write check raises
        raise OSError("Simulated post-write read-back I/O crash")

    with patch.object(Path, "read_text", _mock_read_text):
        resp = planner.apply_workspace_mutation(
            WorkspaceApplyRequest(
                proposal_id=proposal.proposal_id,
                approved=True,
                target_path="requirements.txt",
            )
        )
        assert resp.success is False
        assert resp.status == "MUTATION_ERROR"
        assert "post-write" in resp.message.lower()

    # Proposal must be ABANDONED
    stored = planner.proposal_registry.get(proposal.proposal_id)
    assert stored is not None
    assert stored.state == ProposalLifecycleState.ABANDONED

    # Replay is prohibited
    resp2 = planner.apply_workspace_mutation(
        WorkspaceApplyRequest(
            proposal_id=proposal.proposal_id,
            approved=True,
            target_path="requirements.txt",
        )
    )
    assert resp2.success is False
    assert resp2.status == "PROPOSAL_ALREADY_CONSUMED"


def test_pre_replacement_failure_safely_releases_to_available(planner: UpgradePlannerService, tmp_path: Path):
    """Verify failure before replacement touches destination releases claim back to AVAILABLE."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    rec = _make_python_recommendation()
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="requirements.txt",
            original_content="requests==2.28.0\n",
            recommendations=[rec],
            file_path="requirements.txt",
        )
    )

    # Simulate temp file creation failure (before os.replace)
    with patch("builtins.open", side_effect=PermissionError("Cannot write temp file")):
        resp = planner.apply_workspace_mutation(
            WorkspaceApplyRequest(
                proposal_id=proposal.proposal_id,
                approved=True,
                target_path="requirements.txt",
            )
        )
        assert resp.success is False
        assert resp.status == "MUTATION_ERROR"

    # Because destination was untouched, claim must be released to AVAILABLE
    stored = planner.proposal_registry.get(proposal.proposal_id)
    assert stored is not None
    assert stored.state == ProposalLifecycleState.AVAILABLE

    # Subsequent retry succeeds
    resp2 = planner.apply_workspace_mutation(
        WorkspaceApplyRequest(
            proposal_id=proposal.proposal_id,
            approved=True,
            target_path="requirements.txt",
        )
    )
    assert resp2.success is True
    assert resp2.status == "APPLIED"
    assert target_file.read_text(encoding="utf-8") == "requests==2.32.3\n"


# ---------------------------------------------------------------------------
# Active Claim TTL and Pre-Replacement Exception Safety Tests
# ---------------------------------------------------------------------------
def test_active_proposal_survives_ttl():
    """Test A: An actively claiming/APPLYING proposal is not deleted by direct TTL check during a competing claim."""
    reg = ProposalRegistry(ttl_seconds=10.0)
    proposal = UpgradeProposalResponse(
        proposal_id="",
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        file_path="requirements.txt",
        plan_items=[],
        total_planned=1,
        original_content="requests==2.28.0\n",
        proposed_content="requests==2.32.3\n",
        unified_diff="",
        integrity=PatchIntegrityResult(
            is_valid=True,
            can_apply=True,
            can_reverse=True,
            syntax_valid=True,
            target_versions_match=True,
            unrelated_dependencies_unchanged=True,
            original_unchanged=True,
        ),
        status=ProposalStatus.READY_FOR_REVIEW,
    )
    prop_id = reg.store(proposal)
    claim1 = reg.claim_for_application(prop_id)
    assert claim1.success is True

    # Simulate proposal created_at older than TTL
    stored = reg.get(prop_id)
    assert stored is not None
    stored.created_at = time.time() - 100.0  # past 10s TTL

    # Second claim attempt against expired-but-applying proposal
    claim2 = reg.claim_for_application(prop_id)
    assert claim2.success is False
    assert claim2.status == "PROPOSAL_ALREADY_IN_PROGRESS"

    # Verify original proposal remains registered in APPLYING
    still_stored = reg.get(prop_id)
    assert still_stored is not None
    assert still_stored.state == ProposalLifecycleState.APPLYING


def test_active_proposal_survives_ttl_cleanup():
    """Test B: _evict_expired preserves actively claiming proposal even when its creation timestamp is expired."""
    reg = ProposalRegistry(ttl_seconds=5.0)
    proposal = UpgradeProposalResponse(
        proposal_id="",
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        file_path="requirements.txt",
        plan_items=[],
        total_planned=1,
        original_content="requests==2.28.0\n",
        proposed_content="requests==2.32.3\n",
        unified_diff="",
        integrity=PatchIntegrityResult(
            is_valid=True,
            can_apply=True,
            can_reverse=True,
            syntax_valid=True,
            target_versions_match=True,
            unrelated_dependencies_unchanged=True,
            original_unchanged=True,
        ),
        status=ProposalStatus.READY_FOR_REVIEW,
    )
    prop_id = reg.store(proposal)
    reg.claim_for_application(prop_id)

    # Force timestamp to be expired
    reg._store[prop_id].created_at = time.time() - 100.0

    # Trigger explicit eviction cleanup via _evict_expired()
    with reg._lock:
        reg._evict_expired()

    # Verify still in store and state is APPLYING
    assert prop_id in reg._store
    assert reg._store[prop_id].state == ProposalLifecycleState.APPLYING


def test_expired_inactive_proposal_is_cleaned_up():
    """Test C: Inactive proposals (AVAILABLE, CONSUMED, ABANDONED) are cleaned up by TTL expiration."""
    reg = ProposalRegistry(ttl_seconds=5.0)

    def _make_prop():
        return UpgradeProposalResponse(
            proposal_id="",
            manifest_type="requirements.txt",
            ecosystem=Ecosystem.PYTHON,
            file_path="requirements.txt",
            plan_items=[],
            total_planned=1,
            original_content="requests==2.28.0\n",
            proposed_content="requests==2.32.3\n",
            unified_diff="",
            integrity=PatchIntegrityResult(
                is_valid=True,
                can_apply=True,
                can_reverse=True,
                syntax_valid=True,
                target_versions_match=True,
                unrelated_dependencies_unchanged=True,
                original_unchanged=True,
            ),
            status=ProposalStatus.READY_FOR_REVIEW,
        )

    avail_id = reg.store(_make_prop())
    consumed_id = reg.store(_make_prop())
    abandoned_id = reg.store(_make_prop())

    # Transition states
    reg.claim_for_application(consumed_id)
    reg.complete_application(consumed_id)

    reg.claim_for_application(abandoned_id)
    reg.fail_application(abandoned_id, reason="test failure")

    # Set created_at into the past for all three
    now = time.time()
    reg._store[avail_id].created_at = now - 100.0
    reg._store[consumed_id].created_at = now - 100.0
    reg._store[abandoned_id].created_at = now - 100.0

    # Claiming available should find it expired and delete it
    claim_avail = reg.claim_for_application(avail_id)
    assert claim_avail.success is False
    assert claim_avail.status == "PROPOSAL_NOT_FOUND"

    # Eviction should purge consumed and abandoned once inactive and expired
    with reg._lock:
        reg._evict_expired()

    assert reg.get(avail_id) is None
    assert reg.get(consumed_id) is None
    assert reg.get(abandoned_id) is None


def test_pre_replacement_read_exception_releases_claim(planner: UpgradePlannerService, tmp_path: Path):
    """Test D: An unexpected exception during pre-replacement file reading releases claim to AVAILABLE."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    rec = _make_python_recommendation()
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="requirements.txt",
            original_content="requests==2.28.0\n",
            recommendations=[rec],
            file_path="requirements.txt",
        )
    )

    # Mock Path.read_text to raise an unexpected OSError during pre-replacement freshness check
    with patch.object(Path, "read_text", side_effect=OSError("Disk read I/O error on pre-replacement read")):
        resp = planner.apply_workspace_mutation(
            WorkspaceApplyRequest(
                proposal_id=proposal.proposal_id,
                approved=True,
                target_path="requirements.txt",
            )
        )
        assert resp.success is False
        assert resp.status == "MUTATION_ERROR"

    # Destination was NOT touched
    assert target_file.read_text(encoding="utf-8") == "requests==2.28.0\n"

    # Proposal must have been safely released to AVAILABLE
    stored = planner.proposal_registry.get(proposal.proposal_id)
    assert stored is not None
    assert stored.state == ProposalLifecycleState.AVAILABLE

    # Subsequent retry succeeds
    resp2 = planner.apply_workspace_mutation(
        WorkspaceApplyRequest(
            proposal_id=proposal.proposal_id,
            approved=True,
            target_path="requirements.txt",
        )
    )
    assert resp2.success is True
    assert resp2.status == "APPLIED"
    assert target_file.read_text(encoding="utf-8") == "requests==2.32.3\n"


def test_pre_replacement_validation_exception_releases_claim(planner: UpgradePlannerService, tmp_path: Path):
    """Test E: An unexpected exception during PatchValidator.validate_patch releases claim to AVAILABLE."""
    target_file = tmp_path / "requirements.txt"
    target_file.write_text("requests==2.28.0\n", encoding="utf-8")

    rec = _make_python_recommendation()
    proposal = planner.create_upgrade_proposal(
        UpgradePlanRequest(
            manifest_type="requirements.txt",
            original_content="requests==2.28.0\n",
            recommendations=[rec],
            file_path="requirements.txt",
        )
    )

    with patch("src.services.patch_validator.PatchValidator.validate_patch", side_effect=RuntimeError("Unexpected AST parser crash")):
        resp = planner.apply_workspace_mutation(
            WorkspaceApplyRequest(
                proposal_id=proposal.proposal_id,
                approved=True,
                target_path="requirements.txt",
            )
        )
        assert resp.success is False
        assert resp.status == "MUTATION_ERROR"

    # Target file unmodified
    assert target_file.read_text(encoding="utf-8") == "requests==2.28.0\n"

    # Claim released to AVAILABLE
    stored = planner.proposal_registry.get(proposal.proposal_id)
    assert stored is not None
    assert stored.state == ProposalLifecycleState.AVAILABLE

    # Subsequent retry succeeds
    resp2 = planner.apply_workspace_mutation(
        WorkspaceApplyRequest(
            proposal_id=proposal.proposal_id,
            approved=True,
            target_path="requirements.txt",
        )
    )
    assert resp2.success is True
    assert resp2.status == "APPLIED"
    assert target_file.read_text(encoding="utf-8") == "requests==2.32.3\n"
