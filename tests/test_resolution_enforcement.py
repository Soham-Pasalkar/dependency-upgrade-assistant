"""Fail-closed compatibility enforcement (FR-28, FR-31; ADR-011; M12 criteria 6 and 7).

Real routes, store, job service, supervisor and enforcement; FakeRuntime stands in for Docker.
"""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.resolution import contexts
from src.resolution.models import ResolutionRole, ResolutionStatus
from src.resolution.resolver_runtime import RunResult, RuntimeUnavailable
from src.sandbox.validation_policy import policy_for_proposal
from src.services.job_service import resolution_state
from src.services.proposal_registry import get_proposal_registry
from src.services.run_store import get_run_store
from tests.resolution_api_helpers import PACKAGE_JSON, approve, digest, make_plan
from tests.resolution_doubles import install_service, wait_for_job

FACTS = {"python_full_version": "3.11.18", "machine": "x86_64", "implementation": "cpython",
         "libc": ["glibc", "2.41"], "uid": 65534}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def resolve(service, plan):
    result = service.submit(plan["proposal_id"])
    if result.job:
        wait_for_job(result.job.id)
    return resolution_state(plan["proposal_id"], service.store, service.registry)


def actions(client, plan):
    pid = plan["proposal_id"]
    return {
        "validate": client.post("/api/validation/execute", json={"proposal_id": pid}),
        "apply": client.post("/api/plan/apply", json={"proposal_id": pid, "approved": True}),
        "export": client.post("/api/export", json={"proposal_id": pid}),
    }


def legacy_approval(plan):
    """An approval recorded the way M11 did: diff and validation policy, no resolution fingerprint."""
    store = get_run_store()
    stored = get_proposal_registry().get(plan["proposal_id"])
    store.record_approval(plan["proposal_id"], digest(plan), None, policy_for_proposal(stored.proposal).fingerprint)


def assert_blocked(responses, code_text="ompatibility"):
    for name, resp in responses.items():
        expected = 403 if name == "apply" else 409
        assert resp.status_code == expected, (name, resp.status_code, resp.text)
        assert code_text in resp.text, (name, resp.text)


def behave(kind):
    def run(role, manifest):
        if role != "proposed":
            return None
        if kind == "forged":
            return RunResult("completed", 0, b'{"status": "COMPATIBLE", "fingerprint": "f"}', "", FACTS, "x")
        if kind == "timeout":
            return RunResult("timed_out", None, b"", "", None, "x")
        if kind == "unavailable":
            raise RuntimeUnavailable("docker gone")
        if kind == "failed":
            return RunResult("completed", 1, b"", "ERROR: boom\n", FACTS, "x")
    return run


@pytest.mark.parametrize("kind,status", [
    ("forged", ResolutionStatus.RESOLUTION_FAILED),
    ("failed", ResolutionStatus.RESOLUTION_FAILED),
    ("timeout", ResolutionStatus.RESOLUTION_FAILED),
    ("unavailable", ResolutionStatus.RESOLVER_UNAVAILABLE),
])
def test_non_compatible_results_block_every_action(client, kind, status):
    service, _, _ = install_service(behave(kind))
    plan = make_plan(client)
    assert resolve(service, plan).status == status
    resp = approve(client, plan)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "compatibility_not_verified"
    assert status.value in resp.json()["detail"]["message"]
    legacy_approval(plan)  # even with an approval on record, nothing proceeds
    assert_blocked(actions(client, plan))


def test_never_resolved_plan_is_unknown_and_blocked(client):
    plan = make_plan(client)
    resp = approve(client, plan)
    assert resp.status_code == 409 and "UNKNOWN" in resp.json()["detail"]["message"]
    legacy_approval(plan)
    assert_blocked(actions(client, plan))


def test_pre_m12_approval_is_blocked_even_when_now_compatible(client):
    service, _, _ = install_service()
    plan = make_plan(client)
    legacy_approval(plan)
    assert resolve(service, plan).status == ResolutionStatus.COMPATIBLE
    assert_blocked(actions(client, plan), "approve it again")


def test_npm_plan_is_unsupported_and_blocked(client):
    service, _, _ = install_service()
    plan = make_plan(client, content=PACKAGE_JSON, manifest_type="package.json")
    assert resolve(service, plan).status == ResolutionStatus.UNSUPPORTED
    assert approve(client, plan).status_code == 409
    legacy_approval(plan)
    assert_blocked(actions(client, plan))


def test_compatible_plan_can_be_approved_and_exported(client):
    service, _, _ = install_service()
    plan = make_plan(client)
    state = resolve(service, plan)
    resp = approve(client, plan)
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolution_fingerprint"] == state.proposed.fingerprint and body["resolution_status"] == "COMPATIBLE"
    assert client.get(f"/api/proposals/{plan['proposal_id']}/approval").json()["resolution_fingerprint"] == \
        state.proposed.fingerprint
    assert client.post("/api/export", json={"proposal_id": plan["proposal_id"]}).status_code == 200
    validate = client.post("/api/validation/execute", json={"proposal_id": plan["proposal_id"]})
    assert "ompatibility" not in validate.text


def test_client_supplied_resolution_fields_are_ignored(client):
    plan = make_plan(client)
    resp = approve(client, plan, resolution_status="COMPATIBLE", resolution_fingerprint="f" * 64,
                   compatibility="COMPATIBLE")
    assert resp.status_code in (409, 422)
    pid = plan["proposal_id"]
    export = client.post("/api/export", json={"proposal_id": pid, "resolution_fingerprint": "f" * 64,
                                              "resolution_status": "COMPATIBLE"})
    assert export.status_code in (409, 422)


def test_evidence_that_changes_after_approval_blocks(client):
    calls = {"n": 0}

    def second_run_conflicts(role, manifest):
        if role == "proposed":
            calls["n"] += 1
            if calls["n"] == 2:
                return RunResult("completed", 1, b"", "ERROR: boom\n", FACTS, "x")
        return None
    service, _, _ = install_service(second_run_conflicts)
    plan = make_plan(client)
    resolve(service, plan)
    assert approve(client, plan).status_code == 200
    assert resolve(service, plan).status == ResolutionStatus.RESOLUTION_FAILED  # newer evidence
    assert_blocked(actions(client, plan))


def test_re_resolution_with_new_fingerprint_requires_re_approval(client):
    service, _, _ = install_service()
    plan = make_plan(client)
    first = resolve(service, plan).proposed
    assert approve(client, plan).status_code == 200
    store = get_run_store()
    # Simulate an approval bound to different (older) evidence.
    record = store.get_approval(plan["proposal_id"])
    store.record_approval(plan["proposal_id"], record.diff_sha256, None, record.validation_fingerprint, "0" * 64)
    assert_blocked(actions(client, plan), "approve it again")
    assert first.fingerprint != "0" * 64


def test_evidence_replayed_from_another_plan_does_not_count(client):
    service, _, _ = install_service()
    plan_a, plan_b = make_plan(client), make_plan(client)
    evidence_a = resolve(service, plan_a).proposed
    get_run_store().record_resolution(plan_b["proposal_id"], None, evidence_a.report)
    state_b = resolution_state(plan_b["proposal_id"], service.store, service.registry)
    assert state_b.status == ResolutionStatus.UNKNOWN and "evidence_stale" in state_b.reason
    assert approve(client, plan_b).status_code == 409


def test_forged_compatible_row_in_the_database_does_not_count(client):
    service, _, _ = install_service(behave("failed"))
    plan = make_plan(client)
    failed = resolve(service, plan)
    original = service.store.latest_resolution(plan["proposal_id"], ResolutionRole.ORIGINAL).report
    forged = original.model_copy(update={"role": ResolutionRole.PROPOSED, "status": ResolutionStatus.COMPATIBLE})
    get_run_store().record_resolution(plan["proposal_id"], None, forged)
    assert failed.status == ResolutionStatus.RESOLUTION_FAILED
    assert resolution_state(plan["proposal_id"], service.store, service.registry).status != ResolutionStatus.COMPATIBLE
    assert approve(client, plan).status_code == 409


@pytest.mark.parametrize("change", [
    {"image": "python:3.11-slim@sha256:" + "1" * 64},
    {"pip_args": ("install", "--dry-run", "--pre")},
    {"allowlist_version": "egress-allowlist/2"},
    {"version_policy": (("prereleases", "allow"),)},
])
def test_stale_evidence_blocks(client, monkeypatch, change):
    """Criterion 6: a change to the image, pip policy or allow-list invalidates stored evidence."""
    service, _, _ = install_service()
    plan = make_plan(client)
    resolve(service, plan)
    assert approve(client, plan).status_code == 200
    real = contexts.resolution_environment

    def changed(machine):
        return dataclasses.replace(real(machine), **change)
    monkeypatch.setattr(contexts, "resolution_environment", changed)
    state = resolution_state(plan["proposal_id"], service.store, service.registry)
    assert state.status == ResolutionStatus.UNKNOWN and "evidence_stale" in state.reason
    assert_blocked(actions(client, plan))
    assert approve(client, plan).status_code == 409


def test_changed_constraints_file_invalidates_evidence(client, tmp_path):
    """Criterion 6: constraints are re-read from the run's workspace; editing one invalidates evidence."""
    from src.models.manifest import Ecosystem
    from src.models.plan import PatchIntegrityResult, ProposalStatus, UpgradeProposalResponse

    service, _, _ = install_service()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "constraints.txt").write_text("urllib3<3\n")
    original = "requests==2.28.0\n-c constraints.txt\n"
    proposed = "requests==2.32.3\n-c constraints.txt\n"
    diff = "--- a/requirements.txt\n+++ b/requirements.txt\n-requests==2.28.0\n+requests==2.32.3\n"
    proposal = UpgradeProposalResponse(
        manifest_type="requirements.txt", ecosystem=Ecosystem.PYTHON, file_path="requirements.txt",
        original_content=original, proposed_content=proposed, unified_diff=diff, total_planned=1,
        integrity=PatchIntegrityResult(is_valid=True, can_apply=True, can_reverse=True, syntax_valid=True,
                                       target_versions_match=True, unrelated_dependencies_unchanged=True,
                                       original_unchanged=True),
        status=ProposalStatus.READY_FOR_REVIEW,
    )
    pid = get_proposal_registry().store(proposal)
    proposal = get_proposal_registry().get(pid).proposal
    store = get_run_store()
    run_id = store.create_run(source="github", workspace_dir=str(workspace))
    store.bind_proposal(pid, run_id, 0, diff, proposal)
    plan = {"proposal_id": pid, "unified_diff": diff}
    assert resolve(service, plan).status == ResolutionStatus.COMPATIBLE
    assert approve(client, plan).status_code == 200
    (workspace / "constraints.txt").write_text("urllib3<2\n")
    state = resolution_state(pid, service.store, service.registry)
    assert state.status == ResolutionStatus.UNKNOWN and "evidence_stale" in state.reason
    assert client.post("/api/export", json={"proposal_id": pid}).status_code == 409
