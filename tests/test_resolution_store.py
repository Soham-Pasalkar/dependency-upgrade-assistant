"""M12 storage: job compare-and-set transitions and evidence persistence.

Covers the storage part of criterion 10 (job lifecycle). The migration part of criterion 13 is in
tests/test_resolution_migration.py.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from src.resolution.models import JobStatus, ResolutionReport, ResolutionRole, ResolutionStatus
from src.services.run_store import RunStore


def _report(role=ResolutionRole.PROPOSED, status=ResolutionStatus.COMPATIBLE, fingerprint="f" * 64):
    return ResolutionReport(policy_version="dua-resolution/1", role=role, status=status,
                            fingerprint=fingerprint, created_at=time.time())


@pytest.fixture
def store(tmp_path):
    return RunStore(db_path=str(tmp_path / "m12.db"))


@pytest.fixture
def proposal(store):
    run_id = store.create_run(source="manifest")
    store.bind_proposal("prop_1", run_id, 0, "--- a\n+++ b\n")
    return "prop_1"


def _tables(db):
    with sqlite3.connect(db) as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}


def test_approval_records_resolution_fingerprint(store, proposal):
    store.record_approval(proposal, "d", None, validation_fingerprint="v", resolution_fingerprint="r" * 64)
    assert store.get_approval(proposal).resolution_fingerprint == "r" * 64


def test_one_active_job_per_plan(store, proposal):
    first = store.create_job("resolution", proposal)
    assert first is not None and first.status == JobStatus.QUEUED
    assert store.create_job("resolution", proposal) is None
    assert store.transition_job(first.id, JobStatus.QUEUED, JobStatus.CANCELLED, finished_at=time.time())
    assert store.create_job("resolution", proposal) is not None


def test_transition_is_compare_and_set(store, proposal):
    job = store.create_job("resolution", proposal)
    assert store.transition_job(job.id, JobStatus.QUEUED, JobStatus.RUNNING, started_at=1.0, deadline_at=361.0)
    assert not store.transition_job(job.id, JobStatus.QUEUED, JobStatus.RUNNING)
    assert store.transition_job(job.id, JobStatus.RUNNING, JobStatus.TIMED_OUT, finished_at=2.0)
    # Terminal states never change.
    for target in JobStatus:
        assert not store.transition_job(job.id, (JobStatus.QUEUED, JobStatus.RUNNING), target)
    assert store.get_job(job.id).status == JobStatus.TIMED_OUT


def test_concurrent_transitions_only_one_wins(store, proposal):
    job = store.create_job("resolution", proposal)
    results = []
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        results.append(store.transition_job(job.id, JobStatus.QUEUED, JobStatus.RUNNING))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_transition_rejects_unknown_columns(store, proposal):
    job = store.create_job("resolution", proposal)
    with pytest.raises(ValueError):
        store.transition_job(job.id, JobStatus.QUEUED, JobStatus.RUNNING, status="SUCCEEDED")


def test_success_and_evidence_are_atomic(store, proposal):
    job = store.create_job("resolution", proposal)
    store.transition_job(job.id, JobStatus.QUEUED, JobStatus.RUNNING)
    assert store.complete_job_with_evidence(job.id, proposal, _report())
    assert store.get_job(job.id).status == JobStatus.SUCCEEDED
    assert store.latest_resolution(proposal, ResolutionRole.PROPOSED).status == ResolutionStatus.COMPATIBLE


@pytest.mark.parametrize("end_state", [JobStatus.CANCELLED, JobStatus.TIMED_OUT, JobStatus.INTERRUPTED])
def test_no_evidence_when_job_already_ended(store, proposal, end_state):
    job = store.create_job("resolution", proposal)
    store.transition_job(job.id, JobStatus.QUEUED, JobStatus.RUNNING)
    store.transition_job(job.id, JobStatus.RUNNING, end_state)
    assert not store.complete_job_with_evidence(job.id, proposal, _report())
    assert store.get_job(job.id).status == end_state
    assert store.latest_resolution(proposal, ResolutionRole.PROPOSED) is None


def test_no_evidence_when_cancel_requested(store, proposal):
    job = store.create_job("resolution", proposal)
    store.transition_job(job.id, JobStatus.QUEUED, JobStatus.RUNNING)
    assert store.request_job_cancel(job.id)
    assert not store.complete_job_with_evidence(job.id, proposal, _report())
    assert store.latest_resolution(proposal, ResolutionRole.PROPOSED) is None


@pytest.mark.parametrize("end_state", [JobStatus.CANCELLED, JobStatus.TIMED_OUT, JobStatus.INTERRUPTED])
def test_run_evidence_requires_a_running_job(store, proposal, end_state):
    job = store.create_job("resolution", proposal)
    store.transition_job(job.id, JobStatus.QUEUED, JobStatus.RUNNING)
    original = _report(role=ResolutionRole.ORIGINAL)
    assert store.record_job_evidence(job.id, proposal, original)
    store.transition_job(job.id, JobStatus.RUNNING, end_state)
    assert not store.record_job_evidence(job.id, proposal, original)
    rows = [store.latest_resolution(proposal, ResolutionRole.ORIGINAL)]
    assert len([r for r in rows if r]) == 1


def test_run_evidence_refused_after_cancel_request(store, proposal):
    job = store.create_job("resolution", proposal)
    store.transition_job(job.id, JobStatus.QUEUED, JobStatus.RUNNING)
    store.request_job_cancel(job.id)
    assert not store.record_job_evidence(job.id, proposal, _report(role=ResolutionRole.ORIGINAL))
    assert store.latest_resolution(proposal, ResolutionRole.ORIGINAL) is None


def test_only_proposed_evidence_completes_a_job(store, proposal):
    job = store.create_job("resolution", proposal)
    store.transition_job(job.id, JobStatus.QUEUED, JobStatus.RUNNING)
    with pytest.raises(ValueError):
        store.complete_job_with_evidence(job.id, proposal, _report(role=ResolutionRole.ORIGINAL))
    assert store.get_job(job.id).status == JobStatus.RUNNING


def test_cancel_request_only_for_active_jobs(store, proposal):
    job = store.create_job("resolution", proposal)
    store.transition_job(job.id, JobStatus.QUEUED, JobStatus.CANCELLED)
    assert not store.request_job_cancel(job.id)


def test_restart_interrupts_active_jobs_without_evidence(store, proposal, tmp_path):
    run2 = store.create_run(source="manifest")
    store.bind_proposal("prop_2", run2, 0, "x")
    queued = store.create_job("resolution", proposal)
    running = store.create_job("resolution", "prop_2")
    store.transition_job(running.id, JobStatus.QUEUED, JobStatus.RUNNING)
    interrupted = store.interrupt_active_jobs()
    assert set(interrupted) == {queued.id, running.id}
    for job_id in interrupted:
        job = store.get_job(job_id)
        assert job.status == JobStatus.INTERRUPTED and job.error_code == "interrupted"
    assert store.latest_resolution(proposal, ResolutionRole.PROPOSED) is None
    assert not store.complete_job_with_evidence(running.id, "prop_2", _report())


def test_latest_resolution_is_per_role(store, proposal):
    store.record_resolution(proposal, None, _report(role=ResolutionRole.ORIGINAL, status=ResolutionStatus.CONFLICT,
                                                    fingerprint="o" * 64))
    store.record_resolution(proposal, None, _report(status=ResolutionStatus.CONFLICT, fingerprint="a" * 64))
    store.record_resolution(proposal, None, _report(status=ResolutionStatus.COMPATIBLE, fingerprint="b" * 64))
    latest = store.latest_resolution(proposal, ResolutionRole.PROPOSED)
    assert (latest.fingerprint, latest.status) == ("b" * 64, ResolutionStatus.COMPATIBLE)
    assert store.latest_resolution(proposal, ResolutionRole.ORIGINAL).fingerprint == "o" * 64


def test_jobs_and_evidence_cascade_with_the_run(store, proposal):
    job = store.create_job("resolution", proposal)
    store.transition_job(job.id, JobStatus.QUEUED, JobStatus.RUNNING)
    store.complete_job_with_evidence(job.id, proposal, _report())
    removed = store.prune_expired(run_cutoff=time.time() + 10, plan_cutoff=time.time() + 10)
    assert removed == 1
    assert store.get_job(job.id) is None
    assert store.latest_resolution(proposal, ResolutionRole.PROPOSED) is None


def test_job_requires_existing_plan(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.create_job("resolution", "no_such_plan")
