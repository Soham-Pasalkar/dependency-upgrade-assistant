"""Docker orchestration for isolated resolution runs (FR-24, FR-25, NFR-13; ADR-009, ADR-010, ADR-012).

Per job:
- a ``--internal`` bridge ``dua-res-<job>`` with a /29 from a reserved pool and
  ``com.docker.network.bridge.inhibit_ipv4`` so the host has no address on it (the resolver cannot
  reach host services through the bridge);
- one proxy container (``egress_proxy.py``, read-only mount) at ``.2:3128`` with a random per-job
  token, then attached to the dedicated ``dua-egress`` bridge, the only route out;
- one fresh resolver container per run at ``.3``: non-root, read-only root, all capabilities dropped,
  no-new-privileges, ``--init``, NFR-13 limits, bounded tmpfs, DNS disabled, only a read-only input
  mount, no writable host mount, no Docker socket, no application environment.

Run contract: the launcher prints runtime facts to stderr, runs pip with ``--report`` on the tmpfs and
pip's own output on stderr, and copies the report to stdout only when pip exits 0. stdout is the only
report channel and is read under a size cap; stderr is capped as well. At the run deadline or on
cancellation the container is killed (SIGKILL). Everything is labelled and removed afterwards; a
labelled sweep removes leftovers of jobs that are not active.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from src.api.config import settings
from src.resolution import egress_proxy
from src.resolution.policy import EGRESS_ALLOWLIST, PIP_POLICY_ARGS
from src.resolution.python_input import ClassifiedManifest

logger = logging.getLogger(__name__)

LABEL = "dua.resolution"
EGRESS_NETWORK = "dua-egress"
SUBNET_POOL = ipaddress.ip_network("172.30.252.0/22")
PROXY_PORT = 3128
CLEANUP_SECONDS = 30.0
PROXY_READY_SECONDS = 15.0
MAX_STDERR_BYTES = 1024 * 1024
FACTS_MARKER = "DUA_RUNTIME_FACTS "
REPORT_PATH = "/tmp/dua/report.json"
NOBODY = "65534:65534"
PROXY_SCRIPT = Path(egress_proxy.__file__).resolve()

# Runs inside the resolver container with ``python3 -I -c``. Emits runtime facts, runs pip with the
# report on the tmpfs and pip's output on stderr, and copies the report to stdout only on success.
LAUNCHER = (
    "import json,os,platform,subprocess,sys\n"
    "f={'python_full_version':platform.python_version(),'machine':platform.machine(),"
    "'implementation':sys.implementation.name,'libc':list(platform.libc_ver()),'uid':os.getuid()}\n"
    "sys.stderr.write('" + FACTS_MARKER + "'+json.dumps(f)+'\\n');sys.stderr.flush()\n"
    "os.makedirs('/tmp/dua',exist_ok=True)\n"
    "e=sys.stderr.fileno()\n"
    "rc=subprocess.run([sys.executable,'-m','pip',*sys.argv[1:],'--report','" + REPORT_PATH + "'],"
    "stdin=subprocess.DEVNULL,stdout=e,stderr=e).returncode\n"
    "if rc==0:\n"
    " d=open('" + REPORT_PATH + "','rb').read();sys.stdout.buffer.write(d);sys.stdout.flush()\n"
    "sys.exit(rc)\n"
)


class RuntimeUnavailable(Exception):
    """Docker, the pinned image, the networks or the proxy are not usable (RESOLVER_UNAVAILABLE)."""


@dataclass(frozen=True)
class JobNetwork:
    job_id: str
    network: str
    subnet: str
    proxy_ip: str
    resolver_ip: str
    proxy_container: str
    token: str


@dataclass(frozen=True)
class RunResult:
    state: str  # "completed" | "timed_out" | "cancelled"
    exit_code: Optional[int]
    stdout: bytes
    stderr: str
    facts: Optional[dict]
    container: str


# (args, timeout) -> CompletedProcess; injectable for unit tests.
Executor = Callable[[Sequence[str], float], subprocess.CompletedProcess]


def _short(job_id: str) -> str:
    return "".join(c for c in job_id if c.isalnum())[:24]


def parse_facts(stderr: str) -> Optional[dict]:
    for line in stderr.splitlines()[:5]:
        if line.startswith(FACTS_MARKER):
            try:
                facts = json.loads(line[len(FACTS_MARKER):])
            except ValueError:
                return None
            return facts if isinstance(facts, dict) else None
    return None


def strip_facts(stderr: str) -> str:
    return "\n".join(l for l in stderr.splitlines() if not l.startswith(FACTS_MARKER))


def write_inputs(manifest: ClassifiedManifest, root: Optional[str] = None) -> Path:
    """Write the supervisor's sanitized inputs to a fresh directory readable by the resolver user."""
    path = Path(tempfile.mkdtemp(prefix="dua-res-in-", dir=root))
    path.chmod(0o755)
    files = {"requirements.txt": manifest.sanitized_requirements}
    for n, c in enumerate(manifest.constraints):
        files[f"constraints-{n}.txt"] = c.content
    for name, content in files.items():
        target = path / name
        target.write_text(content, encoding="utf-8")
        target.chmod(0o644)
    return path


def pip_arguments(manifest: ClassifiedManifest, proxy_url: str) -> List[str]:
    args = list(PIP_POLICY_ARGS) + ["--proxy", proxy_url, "-r", "/in/requirements.txt"]
    for n, _ in enumerate(manifest.constraints):
        args += ["-c", f"/in/constraints-{n}.txt"]
    return args


class ResolverRuntime:
    def __init__(self, image: str, docker_bin: Optional[str] = None, executor: Optional[Executor] = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen, clock=time.monotonic,
                 proxy_script: Path = PROXY_SCRIPT) -> None:
        if "@sha256:" not in image:
            raise RuntimeUnavailable("the resolver image must be pinned by digest")
        self.image = image
        self.docker = docker_bin or shutil.which("docker") or "docker"
        self._exec = executor or self._subprocess
        self._popen = popen
        self._clock = clock
        self.proxy_script = proxy_script

    # -- docker plumbing ----------------------------------------------------------------------------
    @staticmethod
    def _subprocess(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def _docker(self, *args: str, timeout: float = 30.0, check: bool = True) -> subprocess.CompletedProcess:
        try:
            result = self._exec([self.docker, *args], timeout)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise RuntimeUnavailable(f"docker {args[0]} failed: {type(err).__name__}") from err
        if check and result.returncode != 0:
            raise RuntimeUnavailable(f"docker {args[0]} failed: {(result.stderr or '').strip()[:300]}")
        return result

    def architecture(self) -> str:
        """Docker host architecture; raises RuntimeUnavailable when Docker is not reachable."""
        result = self._docker("info", "--format", "{{.Architecture}}", timeout=10.0)
        arch = (result.stdout or "").strip()
        if not arch:
            raise RuntimeUnavailable("docker info returned no architecture")
        return arch

    def ensure_image(self, timeout: float = 300.0) -> None:
        if self._docker("image", "inspect", self.image, check=False).returncode == 0:
            return
        self._docker("pull", self.image, timeout=timeout)  # content-addressed: the digest is verified

    # -- per-job network and proxy ------------------------------------------------------------------
    def _ensure_egress(self) -> None:
        if self._docker("network", "inspect", EGRESS_NETWORK, check=False).returncode == 0:
            return
        result = self._docker("network", "create", "--driver", "bridge", "--label", f"{LABEL}=egress",
                              EGRESS_NETWORK, check=False)
        if result.returncode != 0 and "already exists" not in (result.stderr or ""):
            raise RuntimeUnavailable(f"cannot create {EGRESS_NETWORK}: {(result.stderr or '').strip()[:300]}")

    def _create_job_network(self, job_id: str) -> tuple:
        name = f"dua-res-{_short(job_id)}"
        last = ""
        for subnet in SUBNET_POOL.subnets(new_prefix=29):
            hosts = list(subnet.hosts())
            result = self._docker(
                "network", "create", "--driver", "bridge", "--internal",
                "--subnet", str(subnet), "--gateway", str(hosts[0]),
                "-o", "com.docker.network.bridge.inhibit_ipv4=true",
                "--label", f"{LABEL}=job", "--label", f"dua.job={job_id}",
                name, check=False,
            )
            if result.returncode == 0:
                return name, str(subnet), str(hosts[1]), str(hosts[2])
            last = (result.stderr or "").strip()
            if "overlap" not in last.lower() and "pool" not in last.lower():
                break
        raise RuntimeUnavailable(f"cannot create the job network: {last[:300]}")

    def proxy_args(self, net: JobNetwork, deadline_epoch: float) -> List[str]:
        return [
            "run", "-d", "--name", net.proxy_container,
            "--label", f"{LABEL}=proxy", "--label", f"dua.job={net.job_id}",
            "--network", net.network, "--ip", net.proxy_ip,
            "--user", NOBODY, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--init", "--memory", "128m", "--memory-swap", "128m", "--cpus", "0.5", "--pids-limit", "64",
            "--env", f"DUA_PROXY_TOKEN={net.token}", "--env", f"DUA_PROXY_BIND={net.proxy_ip}",
            "--env", f"DUA_PROXY_PORT={PROXY_PORT}", "--env", f"DUA_PROXY_JOB={net.job_id}",
            "--env", f"DUA_PROXY_DEADLINE={deadline_epoch:.3f}", "--env", f"DUA_PROXY_ALLOW={','.join(EGRESS_ALLOWLIST)}",
            "--mount", f"type=bind,source={self.proxy_script},target=/opt/dua/egress_proxy.py,readonly",
            self.image, "python3", "-I", "/opt/dua/egress_proxy.py",
        ]

    def start_job(self, job_id: str, deadline_epoch: float) -> JobNetwork:
        """Create the job network and a ready proxy. Cleans up on any failure."""
        self.ensure_image()
        self._ensure_egress()
        name, subnet, proxy_ip, resolver_ip = self._create_job_network(job_id)
        net = JobNetwork(job_id=job_id, network=name, subnet=subnet, proxy_ip=proxy_ip, resolver_ip=resolver_ip,
                         proxy_container=f"dua-proxy-{_short(job_id)}", token=secrets.token_urlsafe(32))
        try:
            self._docker(*self.proxy_args(net, deadline_epoch))
            self._docker("network", "connect", EGRESS_NETWORK, net.proxy_container)
            self._wait_proxy_ready(net)
        except Exception:
            self.stop_job(net)
            raise
        return net

    def _wait_proxy_ready(self, net: JobNetwork) -> None:
        end = self._clock() + PROXY_READY_SECONDS
        while self._clock() < end:
            logs = self._docker("logs", net.proxy_container, check=False)
            if '"decision": "ready"' in (logs.stdout or ""):
                return
            time.sleep(0.2)
        raise RuntimeUnavailable("the egress proxy did not become ready")

    def proxy_events(self, net: JobNetwork) -> List[dict]:
        logs = self._docker("logs", net.proxy_container, check=False, timeout=10.0)
        events = []
        for line in (logs.stdout or "").splitlines()[-500:]:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("decision") in ("deny", "close"):
                events.append(event)
        return events

    # -- one resolver run ---------------------------------------------------------------------------
    def resolver_args(self, net: JobNetwork, role: str, input_dir: Path, manifest: ClassifiedManifest) -> List[str]:
        mem = settings.RESOLVER_MEMORY_MB
        proxy_url = f"http://dua:{net.token}@{net.proxy_ip}:{PROXY_PORT}"
        return [
            "run", "--name", self.container_name(net, role),
            "--label", f"{LABEL}=resolver", "--label", f"dua.job={net.job_id}", "--label", f"dua.role={role}",
            "--network", net.network, "--ip", net.resolver_ip, "--dns", "127.0.0.1", "--hostname", "resolver",
            "--user", NOBODY, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--init",
            "--memory", f"{mem}m", "--memory-swap", f"{mem}m", "--cpus", str(settings.RESOLVER_CPUS),
            "--pids-limit", str(settings.RESOLVER_PIDS_LIMIT),
            "--tmpfs", f"/tmp:rw,nosuid,nodev,noexec,size={settings.RESOLVER_TMPFS_MB}m",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--mount", f"type=bind,source={input_dir},target=/in,readonly",
            "--pull", "never", "--stop-timeout", "1",
            self.image, "python3", "-I", "-c", LAUNCHER, *pip_arguments(manifest, proxy_url),
        ]

    @staticmethod
    def container_name(net: JobNetwork, role: str) -> str:
        return f"dua-res-{_short(net.job_id)}-{role}"

    def run(self, net: JobNetwork, role: str, manifest: ClassifiedManifest, run_deadline: float,
            cancelled: Callable[[], bool], max_stdout: Optional[int] = None) -> RunResult:
        """Run one fresh resolver container until it exits, the deadline passes or the job is cancelled."""
        max_stdout = max_stdout or settings.RESOLVER_MAX_REPORT_MB * 1024 * 1024
        name = self.container_name(net, role)
        input_dir = write_inputs(manifest)
        try:
            args = [self.docker, *self.resolver_args(net, role, input_dir, manifest)]
            try:
                proc = self._popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as err:
                raise RuntimeUnavailable(f"cannot start docker: {err}") from err
            out, err = bytearray(), bytearray()
            overflow = threading.Event()
            readers = [
                threading.Thread(target=_drain, args=(proc.stdout, out, max_stdout + 1, overflow), daemon=True),
                threading.Thread(target=_drain, args=(proc.stderr, err, MAX_STDERR_BYTES, None), daemon=True),
            ]
            for r in readers:
                r.start()
            state = "completed"
            while proc.poll() is None:
                if cancelled():
                    state = "cancelled"
                elif self._clock() >= run_deadline:
                    state = "timed_out"
                elif overflow.is_set():
                    state = "completed"  # oversized report: stop now, verification rejects it
                else:
                    time.sleep(0.1)
                    continue
                self._docker("kill", "--signal", "KILL", name, check=False, timeout=10.0)
                break
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            for r in readers:
                r.join(timeout=5)
            exit_code = proc.returncode if state == "completed" and not overflow.is_set() else None
            if exit_code in (125, 126, 127):
                stderr_text = bytes(err).decode("utf-8", "replace")
                if parse_facts(stderr_text) is None:
                    raise RuntimeUnavailable(f"docker run failed: {stderr_text.strip()[:300]}")
            stderr_text = bytes(err).decode("utf-8", "replace")
            return RunResult(state=state, exit_code=exit_code if not overflow.is_set() else 0,
                             stdout=bytes(out), stderr=strip_facts(stderr_text), facts=parse_facts(stderr_text),
                             container=name)
        finally:
            self._docker("rm", "-f", name, check=False, timeout=CLEANUP_SECONDS)
            shutil.rmtree(input_dir, ignore_errors=True)

    # -- cleanup ------------------------------------------------------------------------------------
    def stop_job(self, net: JobNetwork) -> None:
        """Remove the job's containers and network within the cleanup bound; failures are logged."""
        end = self._clock() + CLEANUP_SECONDS
        for name in (self.container_name(net, "original"), self.container_name(net, "proposed"), net.proxy_container):
            self._docker("rm", "-f", name, check=False, timeout=max(1.0, end - self._clock()))
        result = self._docker("network", "rm", net.network, check=False, timeout=max(1.0, end - self._clock()))
        if result.returncode != 0 and "not found" not in (result.stderr or "").lower():
            logger.warning("resolver cleanup for job %s left network %s: %s", net.job_id, net.network,
                           (result.stderr or "").strip()[:200])

    def sweep(self, active_job_ids: Iterable[str]) -> List[str]:
        """Remove labelled containers and networks of jobs that are not QUEUED or RUNNING."""
        active = set(active_job_ids)
        removed = []
        ps = self._docker("ps", "-a", "--filter", f"label={LABEL}", "--format",
                          '{{.Names}}\t{{.Label "dua.job"}}', check=False)
        for line in (ps.stdout or "").splitlines():
            name, _, job = line.partition("\t")
            if name and job and job not in active:
                self._docker("rm", "-f", name, check=False)
                removed.append(name)
        nets = self._docker("network", "ls", "--filter", f"label={LABEL}=job", "--format",
                            '{{.Name}}\t{{.Label "dua.job"}}', check=False)
        for line in (nets.stdout or "").splitlines():
            name, _, job = line.partition("\t")
            if name and job and job not in active:
                self._docker("network", "rm", name, check=False)
                removed.append(name)
        return removed


def _drain(stream, buffer: bytearray, limit: int, overflow: Optional[threading.Event]) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            room = limit - len(buffer)
            if room > 0:
                buffer.extend(chunk[:room])
            if len(buffer) >= limit and overflow is not None:
                overflow.set()
    finally:
        stream.close()


def default_runtime() -> ResolverRuntime:
    from src.sandbox.image_catalog import DEFAULT_PYTHON_IMAGE, get_pinned_image_reference
    try:
        image = get_pinned_image_reference(DEFAULT_PYTHON_IMAGE)
    except ValueError as err:
        raise RuntimeUnavailable("no pinned image is configured for resolution") from err
    return ResolverRuntime(image=image)
