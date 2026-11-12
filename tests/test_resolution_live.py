"""Live resolution: real pip in the pinned image, real proxy, real supervisor verification (criteria 1-4, 9, 10).

Runs in the CI Docker job against live PyPI. Results depend on PyPI at run time; the pinned sets use
long-published versions.
"""
from __future__ import annotations

import time
import uuid

import pytest

from src.resolution.models import ResolutionStatus, UnsupportedReason
from tests.live_resolution import docker, env_for, leftovers, probe, require_docker, resolve
from tests.resolution_fixtures import context

pytestmark = pytest.mark.live_resolution


@pytest.fixture(scope="module")
def rt():
    return require_docker()


def live(rt, content, **kw):
    return resolve(rt, context(content, env=env_for(rt)), **kw)


def test_numpy_scipy_compatible(rt):
    result, report, _ = live(rt, "numpy==1.24.0\nscipy==1.10.0\npandas==1.5.3\n")
    assert report.status == ResolutionStatus.COMPATIBLE, (report.failure, result.stderr[-2000:])
    names = {p.name.lower(): p for p in report.packages}
    assert {"numpy", "scipy", "pandas"} <= set(names) and names["numpy"].version == "1.24.0"
    assert any(not p.direct for p in report.packages)  # transitive dependencies verified
    assert all(p.upload.filename == p.filename for p in report.packages)


def test_numpy_scipy_conflict(rt):
    _, report, _ = live(rt, "numpy==2.1.0\nscipy==1.10.0\n")
    assert report.status == ResolutionStatus.CONFLICT, report.failure
    assert any("scipy 1.10.0 depends on numpy<1.27.0" in c for c in report.conflicts[0].causes)


def test_source_only_unsupported(rt):
    result, report, _ = live(rt, "pycrypto==2.6.1\n")
    assert report.status == ResolutionStatus.UNSUPPORTED, report.failure
    assert report.unsupported[0].reason == UnsupportedReason.SOURCE_BUILD_REQUIRED
    assert "setup.py" not in result.stderr and "Building wheel" not in result.stderr  # nothing was built


def test_requires_python_conflict(rt):
    _, report, _ = live(rt, "Django==6.0\n")
    assert report.status == ResolutionStatus.CONFLICT and "requires a different Python" in report.conflicts[0].summary


def test_transitive_and_markers(rt):
    _, report, _ = live(rt, "Flask==3.0.3\n")
    names = {p.name.lower() for p in report.packages}
    assert report.status == ResolutionStatus.COMPATIBLE, report.failure
    assert {"werkzeug", "jinja2", "blinker", "click", "itsdangerous", "markupsafe"} <= names
    assert "importlib-metadata" not in names  # marker python_version < '3.10' excluded


def test_extras(rt):
    _, report, _ = live(rt, "requests[socks]==2.32.3\n")
    assert report.status == ResolutionStatus.COMPATIBLE, report.failure
    assert "pysocks" in {p.name.lower() for p in report.packages}


def test_runtime_facts_and_report_format(rt):
    result, report, _ = live(rt, "six==1.16.0\n")
    assert result.facts["python_full_version"] == "3.11.18" and result.facts["uid"] == 65534
    assert result.facts["libc"] == ["glibc", "2.41"]
    assert report.status == ResolutionStatus.COMPATIBLE, report.failure  # the image's pip emits what P3 parses


def test_timeout_kills_containers(rt):
    started = time.monotonic()
    result, report, net = live(rt, "numpy==1.24.0\nscipy==1.10.0\npandas==1.5.3\n", seconds=2)
    assert result.state == "timed_out" and report is None and time.monotonic() - started < 60
    assert leftovers(net.job_id) == []


def test_cancel_cleans_up(rt):
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 5
    result, report, net = live(rt, "numpy==1.24.0\nscipy==1.10.0\n", cancelled=cancelled)
    assert result.state == "cancelled" and report is None
    assert leftovers(net.job_id) == []


def test_restart_sweep(rt):
    job = uuid.uuid4().hex
    rt.start_job(job, time.time() + 300)  # simulate a crash: never stopped
    assert leftovers(job)
    removed = rt.sweep(active_job_ids=set())
    assert leftovers(job) == [] and removed


def test_sweep_spares_active_jobs(rt):
    job = uuid.uuid4().hex
    net = rt.start_job(job, time.time() + 300)
    try:
        rt.sweep(active_job_ids={job})
        assert leftovers(job)
    finally:
        rt.stop_job(net)


INSPECT = r"""
import json, os
print(json.dumps({"uid": os.getuid(), "root_writable": os.access("/", os.W_OK), "in_writable": os.access("/in", os.W_OK),
                  "docker_sock": os.path.exists("/var/run/docker.sock"), "env": sorted(k for k in os.environ if k.startswith(("DUA", "AWS", "GITHUB", "DATABASE")))}))
"""


def test_container_has_no_writable_mount_socket_or_credentials(rt):
    job = uuid.uuid4().hex
    net = rt.start_job(job, time.time() + 300)
    try:
        facts = probe(rt, net, INSPECT)
    finally:
        rt.stop_job(net)
    assert facts == {"uid": 65534, "root_writable": False, "in_writable": False, "docker_sock": False, "env": []}
