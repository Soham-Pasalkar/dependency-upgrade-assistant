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
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from src.models.manifest import Ecosystem
from src.models.plan import (
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
