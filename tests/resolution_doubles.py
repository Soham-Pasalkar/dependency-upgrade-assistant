"""Test-only resolver doubles whose output must pass the REAL supervisor (authorized at G3, event 304).

- ``SyntheticPyPI``: an in-memory PEP 691 index and PEP 658 metadata host for invented releases. It is
  served through the real ``PyPIMetadataClient`` (URL checks, size limits, hash checks all apply).
- ``FakeRuntime``: stands in for Docker. ``run`` emits a pip-format report (or a scripted outcome) for
  the supervisor's sanitized inputs; the real ``evaluate_run`` / ``verify_success`` / closure /
  fingerprint / binding code then decides the status.

Nothing here can produce COMPATIBLE on its own, and nothing here is evidence of container isolation;
the live tests in the CI Docker job are.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from src.resolution.fingerprint import resolution_environment
from src.resolution.metadata_client import PyPIMetadataClient
from src.resolution.models import JobStatus, ResolutionStatus
from src.resolution.resolver_runtime import JobNetwork, RunResult, RuntimeUnavailable
from src.resolution.target import TARGET_PYTHON_FULL_VERSION, marker_environment
from src.services.job_service import ResolutionJobService, get_job_service, resolution_state

CANDIDATES = ("1.0.0", "2.0.0", "3.0.0", "10.0.0", "99.0.0")
ARCH = "x86_64"


class SyntheticPyPI:
    def __init__(self) -> None:
        self.files: Dict[str, Dict[str, dict]] = {}  # project -> version -> file entry
        self.metadata: Dict[str, bytes] = {}

    def add(self, name: str, version: str, requires_dist: Tuple[str, ...] = ()) -> dict:
        project = canonicalize_name(name)
        if version in self.files.get(project, {}):
            return self.files[project][version]
        filename = f"{project.replace('-', '_')}-{version}-py3-none-any.whl"
        url = f"https://files.pythonhosted.org/packages/sy/nt/{filename}"
        meta = (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                + "".join(f"Requires-Dist: {r}\n" for r in requires_dist)).encode()
        self.metadata[url + ".metadata"] = meta
        entry = {"filename": filename, "url": url, "hashes": {"sha256": hashlib.sha256(url.encode()).hexdigest()},
                 "requires-python": None, "yanked": False,
                 "core-metadata": {"sha256": hashlib.sha256(meta).hexdigest()},
                 "upload-time": "2025-01-01T00:00:00.000000Z"}
        self.files.setdefault(project, {})[version] = entry
        return entry

    def __call__(self, url: str, headers, timeout: float, max_bytes: int):
        if url.startswith("https://pypi.org/simple/"):
            project = url.rstrip("/").rsplit("/", 1)[1]
            if project not in self.files:
                return 404, b""
            body = {"meta": {"api-version": "1.4"}, "name": project, "files": list(self.files[project].values())}
            return 200, json.dumps(body).encode()
        if url in self.metadata:
            return 200, self.metadata[url]
        return 404, b""

    def client(self) -> PyPIMetadataClient:
        return PyPIMetadataClient(http_get=self)


def _choose(req: Requirement) -> Optional[str]:
    pinned = [s.version for s in req.specifier if s.operator in ("==", "===") and "*" not in s.version]
    derived = [v for s in req.specifier if "*" not in s.version for v in (s.version, s.version + ".1", s.version + ".0.1")]
    for version in pinned + derived + list(CANDIDATES):
        if req.specifier.contains(version, prereleases=True):
            return version
    return None


Behaviour = Callable[[str, object], Optional[RunResult]]


class FakeRuntime:
    """Docker stand-in. ``behaviour(role, manifest)`` may return a RunResult or raise; None = resolve."""

    def __init__(self, pypi: SyntheticPyPI, behaviour: Optional[Behaviour] = None, arch: str = ARCH) -> None:
        self.pypi = pypi
        self.behaviour = behaviour
        self.arch = arch
        self.runs: List[str] = []
        self.stopped: List[str] = []

    def architecture(self) -> str:
        return self.arch

    def ensure_image(self, timeout: float = 0) -> None:
        return None

    def start_job(self, job_id: str, deadline_epoch: float) -> JobNetwork:
        return JobNetwork(job_id=job_id, network=f"fake-{job_id}", subnet="172.30.252.0/29",
                          proxy_ip="172.30.252.2", resolver_ip="172.30.252.3", proxy_container=f"fake-proxy-{job_id}",
                          token="t" * 43)

    def stop_job(self, net: JobNetwork) -> None:
        self.stopped.append(net.job_id)

    def proxy_events(self, net: JobNetwork) -> list:
        return []

    def sweep(self, active_job_ids) -> list:
        return []

    def facts(self) -> dict:
        return {"python_full_version": TARGET_PYTHON_FULL_VERSION, "machine": self.arch,
                "implementation": "cpython", "libc": ["glibc", "2.41"], "uid": 65534}

    def run(self, net, role, manifest, run_deadline, cancelled, max_stdout=None) -> RunResult:
        self.runs.append(role)
        if self.behaviour is not None:
            scripted = self.behaviour(role, manifest)
            if scripted is not None:
                return scripted
        return RunResult(state="completed", exit_code=0, stdout=self.report(manifest), stderr="",
                         facts=self.facts(), container=f"fake-{role}")

    def report(self, manifest) -> bytes:
        env = marker_environment(resolution_environment(self.arch))
        install = []
        for line in manifest.requirements:
            req = Requirement(line.split(" --hash=")[0])
            if req.marker is not None and not req.marker.evaluate({**env, "extra": ""}):
                continue
            version = _choose(req)
            entry = self.pypi.add(req.name, version)
            item = {"download_info": {"url": entry["url"], "archive_info": {"hashes": {
                "sha256": entry["hashes"]["sha256"]}}}, "is_direct": False, "is_yanked": False, "requested": True,
                "metadata": {"name": req.name, "version": version}}
            if req.extras:
                item["requested_extras"] = sorted(req.extras)
            install.append(item)
        return json.dumps({"version": "1", "pip_version": "24.0", "install": install,
                           "environment": {**env, "platform_release": "6.0", "platform_version": "#1"}}).encode()


def install_service(behaviour: Optional[Behaviour] = None, max_workers: int = 2):
    """A job service wired to the doubles. Returns (service, pypi, runtime)."""
    from src.services.job_service import set_job_service
    pypi = SyntheticPyPI()
    runtime = FakeRuntime(pypi, behaviour)
    service = ResolutionJobService(runtime_factory=lambda: runtime, client_factory=pypi.client,
                                   max_workers=max_workers)
    set_job_service(service)
    return service, pypi, runtime


def wait_for_job(job_id: str, timeout: float = 10.0):
    service = get_job_service()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = service.get(job_id)
        if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def resolve_compatible(proposal_id: str):
    """Test setup: run the real job pipeline for a plan (doubles as resolver) and require COMPATIBLE."""
    service = get_job_service()
    result = service.submit(proposal_id)
    assert result.job is not None, f"plan not resolvable: {result.report.unsupported if result.report else ''}"
    job = wait_for_job(result.job.id)
    state = resolution_state(proposal_id, service.store, service.registry)
    assert state.status == ResolutionStatus.COMPATIBLE, (job.status, job.error_code, job.error_message, state.reason)
    return state.proposed
