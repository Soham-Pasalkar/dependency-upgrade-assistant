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


def test_transitive(rt):
    _, report, _ = live(rt, "Flask==3.0.3\n")
    assert report.status == ResolutionStatus.COMPATIBLE, report.failure
    transitive = {p.name.lower() for p in report.packages if not p.direct}
    assert {"werkzeug", "jinja2", "blinker", "click", "itsdangerous", "markupsafe"} <= transitive


def test_markers(rt):
    _, report, _ = live(rt, "Flask==3.0.3\n")
    assert report.status == ResolutionStatus.COMPATIBLE, report.failure
    assert "importlib-metadata" not in {p.name.lower() for p in report.packages}  # python_version < '3.10'


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


def test_constraints_file(rt, tmp_path):
    _, free, _ = live(rt, "Flask==3.0.3\n")
    (tmp_path / "constraints.txt").write_text("Werkzeug<3.1\n")
    ctx = context("Flask==3.0.3\n-c constraints.txt\n", env=env_for(rt), workspace_dir=str(tmp_path),
                  manifest_path="requirements.txt")
    _, constrained, _ = resolve(rt, ctx)
    version = {p.name.lower(): p.version for p in constrained.packages}["werkzeug"]
    free_version = {p.name.lower(): p.version for p in free.packages}["werkzeug"]
    assert constrained.status == ResolutionStatus.COMPATIBLE, constrained.failure
    assert version.startswith("3.0.") and not free_version.startswith("3.0.")  # the file changed the outcome


def test_transitive_conflict(rt):
    # boto3 1.34.0 needs botocore>=1.34.0,<1.35.0; awscli 1.29.0 needs botocore==1.31.0.
    _, report, _ = live(rt, "boto3==1.34.0\nawscli==1.29.0\n")
    assert report.status == ResolutionStatus.CONFLICT, report.failure
    assert any("botocore" in c for c in report.conflicts[0].causes)


def test_prerelease_policy(rt):
    # pytest 8.0.0rc1/rc2 (universal wheels) precede the final 8.0.0 on PyPI.
    _, ranged, _ = live(rt, "pytest>=7.4,<8.0.1\n")
    assert ranged.status == ResolutionStatus.COMPATIBLE, ranged.failure
    assert {p.name.lower(): p.version for p in ranged.packages}["pytest"] == "8.0.0"  # not 8.0.0rc2
    _, pinned, _ = live(rt, "pytest==8.0.0rc2\n")
    assert pinned.status == ResolutionStatus.COMPATIBLE, pinned.failure  # a pinned prerelease is allowed
    assert {p.name.lower(): p.version for p in pinned.packages}["pytest"] == "8.0.0rc2"


def test_yanked_pin_warning(rt):
    _, pinned, _ = live(rt, "requests==2.32.0\n")  # 2.32.0 was yanked on PyPI
    assert pinned.status == ResolutionStatus.COMPATIBLE, pinned.failure
    assert any(w.startswith("yanked: requests==2.32.0") for w in pinned.warnings)
    _, ranged, _ = live(rt, "requests>=2.32.0,<2.32.2\n")  # only yanked releases in range
    assert ranged.status != ResolutionStatus.COMPATIBLE
    assert all(not (p.name.lower() == "requests" and p.version in ("2.32.0", "2.32.1")) for p in ranged.packages)


# -- job level: the real API, job service, runtime, proxy, pip and supervisor ----------------------

import difflib  # noqa: E402
import io  # noqa: E402
import json as _json  # noqa: E402
import zipfile  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import src.services.run_store as run_store_module  # noqa: E402
from src.api.config import settings  # noqa: E402
from src.api.main import app  # noqa: E402
from src.models.manifest import Ecosystem  # noqa: E402
from src.models.plan import PatchIntegrityResult, ProposalStatus, UpgradeProposalResponse  # noqa: E402
from src.resolution import resolver_runtime  # noqa: E402
from src.services.job_service import ResolutionJobService, set_job_service  # noqa: E402
from src.services.proposal_registry import get_proposal_registry  # noqa: E402
from src.services.run_store import RunStore  # noqa: E402


@pytest.fixture
def api(rt, tmp_path):
    previous = run_store_module._store
    run_store_module._store = RunStore(db_path=str(tmp_path / "live.db"))
    service = ResolutionJobService()
    set_job_service(service)
    with TestClient(app) as client:
        yield client, service
    set_job_service(None)
    run_store_module._store = previous


def direct_plan(original: str, proposed: str, workspace=None) -> dict:
    diff = "".join(difflib.unified_diff(original.splitlines(True), proposed.splitlines(True),
                                        "a/requirements.txt", "b/requirements.txt"))
    proposal = UpgradeProposalResponse(
        manifest_type="requirements.txt", ecosystem=Ecosystem.PYTHON, file_path="requirements.txt",
        original_content=original, proposed_content=proposed, unified_diff=diff, total_planned=1,
        integrity=PatchIntegrityResult(is_valid=True, can_apply=True, can_reverse=True, syntax_valid=True,
                                       target_versions_match=True, unrelated_dependencies_unchanged=True,
                                       original_unchanged=True),
        status=ProposalStatus.READY_FOR_REVIEW,
    )
    registry = get_proposal_registry()
    pid = registry.store(proposal)
    store = run_store_module.get_run_store()
    run_id = store.create_run(source="github" if workspace else "manifest", workspace_dir=workspace)
    store.bind_proposal(pid, run_id, 0, diff, registry.get(pid).proposal)
    import hashlib
    return {"proposal_id": pid, "diff_sha256": hashlib.sha256(diff.encode()).hexdigest()}


def run_job(client, plan, timeout=400):
    resp = client.post(f"/api/proposals/{plan['proposal_id']}/resolve")
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job"]["id"]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("QUEUED", "RUNNING"):
            break
        time.sleep(1)
    view = client.get(f"/api/proposals/{plan['proposal_id']}/resolution").json()
    return job, view


def test_original_and_proposed_runs(api):
    client, _ = api
    plan = direct_plan("Flask==2.0.3\n", "Flask==3.0.3\n")
    job, view = run_job(client, plan)
    assert job["status"] == "SUCCEEDED", job
    assert view["original"]["status"] == view["proposed"]["status"] == "COMPATIBLE"
    assert view["original"]["fingerprint"] != view["proposed"]["fingerprint"]
    assert view["comparison"]["available"] and view["comparison"]["introduced"] == ["blinker"]
    assert all(p["upload"]["available"] and p["upload"]["filename"] == p["filename"] for p in view["proposed"]["packages"])
    approval = client.post(f"/api/proposals/{plan['proposal_id']}/approve", json={"diff_sha256": plan["diff_sha256"]})
    assert approval.status_code == 200 and approval.json()["resolution_fingerprint"] == view["proposed"]["fingerprint"]
    export = client.post("/api/export", json={"proposal_id": plan["proposal_id"]}).json()
    zip_url = next(i["download_url"] for i in export["items"] if i["artifact_type"] == "bundle_zip")
    archive = zipfile.ZipFile(io.BytesIO(client.get(zip_url).content))
    report = archive.read("compatibility-report.md").decode()
    summary = _json.loads(archive.read("summary.json"))
    assert "| blinker |" in report and "transitive | yes |" in report
    assert summary["compatibility"]["comparison"]["introduced"] == ["blinker"]
    assert summary["compatibility"]["proposed_fingerprint"] == view["proposed"]["fingerprint"]


def test_failed_original_successful_proposed(api):
    client, _ = api
    plan = direct_plan("numpy==2.1.0\nscipy==1.10.0\n", "numpy==1.24.0\nscipy==1.10.0\n")
    job, view = run_job(client, plan)
    assert job["status"] == "SUCCEEDED"
    assert view["original"]["status"] == "CONFLICT" and view["proposed"]["status"] == "COMPATIBLE"
    assert view["comparison"] == {"available": False, "reason": "original_conflict", "introduced": []}
    assert client.post(f"/api/proposals/{plan['proposal_id']}/approve",
                       json={"diff_sha256": plan["diff_sha256"]}).status_code == 200


def test_job_timeout_is_not_a_conflict(api, monkeypatch):
    client, service = api
    monkeypatch.setattr(settings, "RESOLVER_TIMEOUT_SECONDS", 3)
    plan = direct_plan("numpy==1.24.0\nscipy==1.10.0\npandas==1.5.3\n", "numpy==1.24.0\nscipy==1.10.0\npandas==1.5.3\n")
    job, view = run_job(client, plan, timeout=120)
    assert job["status"] == "TIMED_OUT" and view["status"] == "RESOLUTION_FAILED" and view["reason"] == "timeout"
    assert view["proposed"] is None
    assert client.post(f"/api/proposals/{plan['proposal_id']}/approve",
                       json={"diff_sha256": plan["diff_sha256"]}).status_code == 409
    assert leftovers(job["id"]) == []


def test_restart_interrupts_a_running_job(api):
    client, service = api
    plan = direct_plan("numpy==1.24.0\nscipy==1.10.0\npandas==1.5.3\n", "numpy==1.24.0\nscipy==1.10.0\n")
    job_id = client.post(f"/api/proposals/{plan['proposal_id']}/resolve").json()["job"]["id"]
    end = time.monotonic() + 120
    while time.monotonic() < end and client.get(f"/api/jobs/{job_id}").json().get("current_role") is None:
        time.sleep(0.5)
    ResolutionJobService().recover()  # a new process starts: interrupts the job and sweeps its containers
    service.shutdown(wait=True)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "INTERRUPTED"
    view = client.get(f"/api/proposals/{plan['proposal_id']}/resolution").json()
    assert view["status"] == "UNKNOWN" and view["proposed"] is None and view["original"] is None
    time.sleep(2)
    assert leftovers(job_id) == []


def test_forged_report_from_inside_the_container_is_rejected(api, monkeypatch):
    """Resolver-side code prints a well-formed report claiming the real numpy/scipy conflict resolved."""
    from src.resolution.metadata_client import Deadline, PyPIMetadataClient
    client, _ = api
    pypi = PyPIMetadataClient()
    items = []
    for name, version in (("numpy", "2.1.0"), ("scipy", "1.10.0")):
        index = pypi.project_index(name, Deadline())
        entry = next(f for f in index.files if f.filename.startswith(f"{name}-{version}-cp311-cp311-manylinux")
                     and "x86_64" in f.filename)
        items.append({"download_info": {"url": entry.url, "archive_info": {"hashes": {"sha256": entry.sha256}}},
                      "is_direct": False, "is_yanked": False, "requested": True,
                      "metadata": {"name": name, "version": version}})
    from src.resolution.target import marker_environment
    from src.resolution.fingerprint import resolution_environment
    env = {**marker_environment(resolution_environment("amd64")), "platform_release": "x", "platform_version": "x"}
    forged = _json.dumps({"version": "1", "pip_version": "24.0", "install": items, "environment": env})
    launcher = resolver_runtime.LAUNCHER.split("os.makedirs")[0] + f"sys.stdout.write({forged!r});sys.exit(0)\n"
    monkeypatch.setattr(resolver_runtime, "LAUNCHER", launcher)
    plan = direct_plan("numpy==2.1.0\nscipy==1.10.0\n", "numpy==2.1.0\nscipy==1.10.0\n")
    job, view = run_job(client, plan)
    if view["proposed"]["target"]["machine"] != "x86_64":
        pytest.skip("forged wheel selection is for x86_64 runners")
    assert job["status"] == "SUCCEEDED"
    assert view["status"] == "RESOLUTION_FAILED" and view["proposed"]["failure"].startswith("unsatisfied")
    assert client.post(f"/api/proposals/{plan['proposal_id']}/approve",
                       json={"diff_sha256": plan["diff_sha256"]}).status_code == 409
