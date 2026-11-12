"""Resolution job lifecycle (FR-29, NFR-15; ADR-012; M12 criterion 10 unit part, criterion 14b wiring).

The real job service, store, supervisor and comparison run; Docker is replaced by FakeRuntime
(tests/resolution_doubles.py). Container behaviour is proven by the live tests in the CI Docker job.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from src.api.config import settings
from src.api.main import app
from src.resolution.models import JobStatus, ResolutionRole, ResolutionStatus
from src.resolution.resolver_runtime import RunResult, RuntimeUnavailable
from src.services.job_service import JobServiceError, resolution_state
from tests.resolution_api_helpers import PACKAGE_JSON, make_plan
from tests.resolution_doubles import install_service, wait_for_job


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def state(service, pid):
    return resolution_state(pid, service.store, service.registry)


def run_plan(client, behaviour=None, content=None):
    service, pypi, runtime = install_service(behaviour)
    plan = make_plan(client, **({"content": content} if content else {}))
    result = service.submit(plan["proposal_id"])
    job = wait_for_job(result.job.id)
    return service, runtime, plan["proposal_id"], job


def scripted(role_results):
    """Behaviour returning a RunResult per role (None = resolve normally)."""
    def behave(role, manifest):
        value = role_results.get(role)
        return value() if callable(value) else value
    return behave


FACTS = {"python_full_version": "3.11.18", "machine": "x86_64", "implementation": "cpython",
         "libc": ["glibc", "2.41"], "uid": 65534}


def failed_run(stderr="ERROR: something went wrong\n"):
    return RunResult(state="completed", exit_code=1, stdout=b"", stderr=stderr, facts=FACTS, container="x")


def test_two_sequential_runs_with_separate_evidence(client):
    service, runtime, pid, job = run_plan(client)
    assert job.status == JobStatus.SUCCEEDED and runtime.runs == ["original", "proposed"]
    original = service.store.latest_resolution(pid, ResolutionRole.ORIGINAL)
    proposed = service.store.latest_resolution(pid, ResolutionRole.PROPOSED)
    assert original.status == proposed.status == ResolutionStatus.COMPATIBLE
    assert original.fingerprint != proposed.fingerprint and original.job_id == proposed.job_id == job.id
    assert proposed.report.comparison.available and runtime.stopped == [job.id]
    assert job.started_at and job.deadline_at == pytest.approx(job.started_at + 2 * settings.RESOLVER_TIMEOUT_SECONDS)
    assert state(service, pid).status == ResolutionStatus.COMPATIBLE


def test_failed_original_still_runs_proposed(client):
    service, runtime, pid, job = run_plan(client, scripted({"original": failed_run()}))
    assert job.status == JobStatus.SUCCEEDED and runtime.runs == ["original", "proposed"]
    assert service.store.latest_resolution(pid, ResolutionRole.ORIGINAL).status == ResolutionStatus.RESOLUTION_FAILED
    proposed = service.store.latest_resolution(pid, ResolutionRole.PROPOSED)
    assert proposed.status == ResolutionStatus.COMPATIBLE
    assert proposed.report.comparison.available is False
    assert proposed.report.comparison.reason == "original_resolution_failed"
    assert all(p.introduced is None for p in proposed.report.packages)


def test_proposed_conflict_is_stored_as_conflict(client):
    stderr = ("ERROR: Cannot install requests==2.32.3 and urllib3==1.26.5 because these package versions have "
              "conflicting dependencies.\n\nThe conflict is caused by:\n    The user requested urllib3==1.26.5\n"
              "    requests 2.32.3 depends on urllib3<3,>=1.27\n\n")

    def proposed():
        pypi.add("requests", "2.32.3")
        pypi.add("urllib3", "1.26.5")
        return failed_run(stderr)
    service, pypi, runtime = install_service(scripted({"proposed": proposed}))
    plan = make_plan(client)
    job = wait_for_job(service.submit(plan["proposal_id"]).job.id)
    assert job.status == JobStatus.SUCCEEDED
    assert state(service, plan["proposal_id"]).status == ResolutionStatus.CONFLICT


@pytest.mark.parametrize("role", ["original", "proposed"])
def test_timeout_is_not_a_conflict_and_stores_no_evidence_for_the_run(client, role):
    timed_out = RunResult(state="timed_out", exit_code=None, stdout=b"", stderr="", facts=None, container="x")
    service, runtime, pid, job = run_plan(client, scripted({role: timed_out}))
    assert job.status == JobStatus.TIMED_OUT and job.error_code == "timeout"
    assert service.store.latest_resolution(pid, ResolutionRole.PROPOSED) is None
    if role == "original":
        assert runtime.runs == ["original"]  # run 2 is not started
        assert service.store.latest_resolution(pid, ResolutionRole.ORIGINAL) is None
    else:
        assert service.store.latest_resolution(pid, ResolutionRole.ORIGINAL) is not None  # completed run kept
    s = state(service, pid)
    assert (s.status, s.reason) == (ResolutionStatus.RESOLUTION_FAILED, "timeout")
    assert runtime.stopped == [job.id]


def test_job_deadline_between_runs(client, monkeypatch):
    service, pypi, runtime = install_service()
    now = [0.0]
    service.monotonic = lambda: now[0]

    def original(role, manifest):
        now[0] += 2 * settings.RESOLVER_TIMEOUT_SECONDS  # the original run consumed the whole job budget
        return None
    runtime.behaviour = original
    plan = make_plan(client)
    job = wait_for_job(service.submit(plan["proposal_id"]).job.id)
    assert job.status == JobStatus.TIMED_OUT and runtime.runs == ["original"]


def test_cancel_queued_job(client):
    service, pypi, runtime = install_service()
    plan = make_plan(client)
    job = service.store.create_job("resolution", plan["proposal_id"])
    assert service.cancel(job.id).status == JobStatus.CANCELLED
    service.run_job(job.id)  # a worker picking it up later does nothing
    assert service.get(job.id).status == JobStatus.CANCELLED and runtime.runs == []


def test_cancel_during_a_run(client):
    holder = {}

    def original():
        service.cancel(holder["job"])
        return RunResult(state="cancelled", exit_code=None, stdout=b"", stderr="", facts=None, container="x")
    service, pypi, runtime = install_service(scripted({"original": original}))
    plan = make_plan(client)
    original_submit = service._pool.submit
    service._pool.submit = lambda fn, job_id: holder.setdefault("job", job_id) and original_submit(fn, job_id)
    job = wait_for_job(service.submit(plan["proposal_id"]).job.id)
    assert job.status == JobStatus.CANCELLED and runtime.runs == ["original"]
    assert service.store.latest_resolution(plan["proposal_id"], ResolutionRole.ORIGINAL) is None
    assert state(service, plan["proposal_id"]).status == ResolutionStatus.UNKNOWN


def test_cancel_after_the_proposed_run_but_before_commit_stores_nothing(client):
    holder = {}

    def proposed(role, manifest):
        if role == "proposed":
            service.store.request_job_cancel(holder["job"])
        return None
    service, pypi, runtime = install_service(proposed)
    plan = make_plan(client)
    real = service._pool.submit
    service._pool.submit = lambda fn, job_id: holder.setdefault("job", job_id) and real(fn, job_id)
    job = wait_for_job(service.submit(plan["proposal_id"]).job.id)
    assert job.status == JobStatus.CANCELLED
    assert service.store.latest_resolution(plan["proposal_id"], ResolutionRole.PROPOSED) is None


def test_terminal_states_are_immutable(client):
    service, runtime, pid, job = run_plan(client)
    assert service.cancel(job.id).status == JobStatus.SUCCEEDED


def test_resolver_unavailable(client):
    def unavailable(role, manifest):
        raise RuntimeUnavailable("docker: Error response from daemon: /secret/host/path not found")
    service, runtime, pid, job = run_plan(client, unavailable)
    assert job.status == JobStatus.FAILED and job.error_code == "resolver_unavailable"
    assert "/secret/host/path" not in job.error_message  # no host details reach the client
    assert state(service, pid).status == ResolutionStatus.RESOLVER_UNAVAILABLE
    assert service.store.latest_resolution(pid, ResolutionRole.PROPOSED) is None


def test_runtime_facts_mismatch_fails_closed(client):
    wrong = dict(FACTS, python_full_version="3.12.0")

    def facts(role, manifest):
        return RunResult(state="completed", exit_code=0, stdout=runtime.report(manifest), stderr="", facts=wrong,
                         container="x")
    service, pypi, runtime = install_service(facts)
    plan = make_plan(client)
    job = wait_for_job(service.submit(plan["proposal_id"]).job.id)
    proposed = service.store.latest_resolution(plan["proposal_id"], ResolutionRole.PROPOSED)
    assert job.status == JobStatus.SUCCEEDED and proposed.status == ResolutionStatus.RESOLUTION_FAILED
    assert proposed.report.failure.startswith("environment_mismatch")


def test_forged_report_is_never_compatible(client):
    def forged(role, manifest):
        return RunResult(state="completed", exit_code=0, stdout=b'{"status": "COMPATIBLE"}', stderr="",
                         facts=FACTS, container="x")
    service, runtime, pid, job = run_plan(client, forged)
    assert state(service, pid).status == ResolutionStatus.RESOLUTION_FAILED


def test_worker_crash_marks_the_job_failed_and_cleans_up(client):
    def crash(role, manifest):
        raise KeyError("unexpected")
    service, runtime, pid, job = run_plan(client, crash)
    assert job.status == JobStatus.FAILED and job.error_code == "internal_error"
    assert runtime.stopped == [job.id] and service.store.latest_resolution(pid, ResolutionRole.PROPOSED) is None


def test_one_active_job_per_plan_under_concurrent_submissions(client):
    gate = threading.Event()

    def slow(role, manifest):
        gate.wait(5)
        return None
    service, pypi, runtime = install_service(slow, max_workers=4)
    plan = make_plan(client)
    outcomes = []

    def submit():
        try:
            outcomes.append(("job", service.submit(plan["proposal_id"]).job.id))
        except JobServiceError as err:
            outcomes.append((err.code, None))
    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    gate.set()
    jobs = [o for o in outcomes if o[0] == "job"]
    assert len(jobs) == 1 and all(o[0] == "job_active" for o in outcomes if o[0] != "job")
    assert wait_for_job(jobs[0][1]).status == JobStatus.SUCCEEDED


def test_restart_interrupts_jobs_without_evidence(client):
    service, pypi, runtime = install_service()
    plan = make_plan(client)
    queued = service.store.create_job("resolution", plan["proposal_id"])
    service.store.transition_job(queued.id, JobStatus.QUEUED, JobStatus.RUNNING)
    service.recover()
    job = service.get(queued.id)
    assert job.status == JobStatus.INTERRUPTED
    assert service.store.latest_resolution(plan["proposal_id"], ResolutionRole.PROPOSED) is None
    assert state(service, plan["proposal_id"]).status == ResolutionStatus.UNKNOWN
    service.run_job(queued.id)  # a stale worker cannot resurrect it
    assert service.get(queued.id).status == JobStatus.INTERRUPTED


def test_stale_worker_after_restart_writes_nothing(client):
    holder = {}

    def restart_mid_run(role, manifest):
        if role == "original":
            service.recover()  # a new process starts and interrupts this job while its run is in flight
        return None
    service, pypi, runtime = install_service(restart_mid_run)
    plan = make_plan(client)
    real = service._pool.submit
    service._pool.submit = lambda fn, job_id: holder.setdefault("job", job_id) and real(fn, job_id)
    job = wait_for_job(service.submit(plan["proposal_id"]).job.id)
    assert job.status == JobStatus.INTERRUPTED and runtime.runs == ["original"]
    assert service.store.latest_resolution(plan["proposal_id"], ResolutionRole.ORIGINAL) is None
    assert service.store.latest_resolution(plan["proposal_id"], ResolutionRole.PROPOSED) is None


def test_unsupported_input_is_answered_without_a_job(client):
    service, pypi, runtime = install_service()
    plan = make_plan(client, content=PACKAGE_JSON, manifest_type="package.json")
    result = service.submit(plan["proposal_id"])
    assert result.job is None and result.report.status == ResolutionStatus.UNSUPPORTED
    assert runtime.runs == [] and state(service, plan["proposal_id"]).status == ResolutionStatus.UNSUPPORTED


def test_unknown_or_expired_plans(client, monkeypatch):
    service, pypi, runtime = install_service()
    with pytest.raises(JobServiceError) as err:
        service.submit("prop_missing")
    assert err.value.status_code == 404
    plan = make_plan(client)
    monkeypatch.setattr(settings, "PLAN_RETENTION_HOURS", -1)
    with pytest.raises(JobServiceError) as err:
        service.submit(plan["proposal_id"])
    assert err.value.status_code == 410


def test_proxy_denials_are_recorded_as_warnings(client):
    service, pypi, runtime = install_service()
    deny = {"decision": "deny", "host": "evil.example", "port": 443, "reason": "host_not_allowed"}
    # The proxy log is per job; a denial appears only once the proposed run has started.
    runtime.proxy_events = lambda net: [deny] if "proposed" in runtime.runs else []
    plan = make_plan(client)
    wait_for_job(service.submit(plan["proposal_id"]).job.id)
    proposed = service.store.latest_resolution(plan["proposal_id"], ResolutionRole.PROPOSED)
    original = service.store.latest_resolution(plan["proposal_id"], ResolutionRole.ORIGINAL)
    assert "egress_denied: evil.example:443 host_not_allowed" in proposed.report.warnings
    assert original.report.warnings == []
