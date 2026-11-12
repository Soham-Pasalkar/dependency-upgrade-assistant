"""Resolver runtime argument construction and lifecycle with a fake Docker CLI (M12 criteria 4, 9, 10).

These prove what the runtime asks Docker to do. That Docker actually enforces it is proven only by the
live tests in the CI Docker job (tests/test_resolution_live.py, tests/test_egress_live.py).
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from src.api.config import settings
from src.resolution import resolver_runtime as rr
from src.resolution.python_input import classify_requirements

IMAGE = "python:3.11-slim@sha256:" + "a" * 64


class FakeDocker:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}

    def __call__(self, args, timeout):
        self.calls.append(list(args[1:]))
        key = " ".join(args[1:3])
        rc, err, out = self.fail.get(key, (0, "", ""))
        if args[1] == "logs" and not out:
            out = '{"decision": "ready", "job": "j"}\n{"decision": "deny", "host": "evil.example", "reason": "host_not_allowed"}\n'
        if args[1:3] == ["info", "--format"] and not out:
            out = "x86_64\n"
        return subprocess.CompletedProcess(args, rc, out, err)

    def cmds(self, prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]


def runtime(fake=None, **kw):
    return rr.ResolverRuntime(IMAGE, docker_bin="docker", executor=fake or FakeDocker(), **kw)


NET = rr.JobNetwork(job_id="job1", network="dua-res-job1", subnet="172.30.252.0/29", proxy_ip="172.30.252.2",
                    resolver_ip="172.30.252.3", proxy_container="dua-proxy-job1", token="T" * 43)


def test_image_must_be_pinned():
    with pytest.raises(rr.RuntimeUnavailable):
        rr.ResolverRuntime("python:3.11-slim", executor=FakeDocker())


def test_resolver_container_is_hardened(tmp_path):
    manifest = classify_requirements("numpy==1.24.0\n")
    args = runtime().resolver_args(NET, "proposed", tmp_path, manifest)
    joined = " ".join(args)
    for flag in ("--user 65534:65534", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--init", "--dns 127.0.0.1", "--network dua-res-job1", "--ip 172.30.252.3",
                 f"--memory {settings.RESOLVER_MEMORY_MB}m", f"--memory-swap {settings.RESOLVER_MEMORY_MB}m",
                 f"--cpus {settings.RESOLVER_CPUS}", f"--pids-limit {settings.RESOLVER_PIDS_LIMIT}",
                 f"size={settings.RESOLVER_TMPFS_MB}m", "--pull never"):
        assert flag in joined, flag
    mounts = [args[i + 1] for i, a in enumerate(args) if a in ("--mount", "-v", "--volume")]
    assert mounts == [f"type=bind,source={tmp_path},target=/in,readonly"]  # only the read-only input
    assert "docker.sock" not in joined and "--privileged" not in args and "--network=host" not in joined
    envs = [args[i + 1] for i, a in enumerate(args) if a in ("--env", "-e")]
    assert envs == ["HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"]  # no application environment
    assert args[args.index(IMAGE) + 1:args.index(IMAGE) + 4] == ["python3", "-I", "-c"]


def test_pip_args_binary_only(tmp_path):
    manifest = classify_requirements("numpy==1.24.0\n")
    args = runtime().resolver_args(NET, "original", tmp_path, manifest)
    pip = args[args.index(rr.LAUNCHER) + 1:]
    assert pip[:len(rr.PIP_POLICY_ARGS)] == list(rr.PIP_POLICY_ARGS)
    assert "--only-binary=:all:" in pip and "--dry-run" in pip and "--isolated" in pip
    assert pip[pip.index("--proxy") + 1] == f"http://dua:{NET.token}@172.30.252.2:3128"
    assert pip[-2:] == ["-r", "/in/requirements.txt"]
    for forbidden in ("--no-binary", "--pre", "--extra-index-url", "--find-links", "--trusted-host", "--quiet"):
        assert forbidden not in pip
    assert "--report" in rr.LAUNCHER and rr.REPORT_PATH in rr.LAUNCHER  # report written to the tmpfs


def test_constraints_are_passed_as_read_only_inputs(tmp_path):
    (tmp_path / "c.txt").write_text("numpy<2\n")
    manifest = classify_requirements("numpy\n-c c.txt\n", manifest_path="requirements.txt", workspace_dir=str(tmp_path))
    pip = rr.pip_arguments(manifest, "http://x")
    assert pip[-4:] == ["-r", "/in/requirements.txt", "-c", "/in/constraints-0.txt"]
    written = rr.write_inputs(manifest)
    try:
        assert (written / "requirements.txt").read_text() == "numpy\n"
        assert (written / "constraints-0.txt").read_text() == "numpy<2\n"
        assert oct(written.stat().st_mode & 0o777) == "0o755"
    finally:
        import shutil
        shutil.rmtree(written)


def test_proxy_container_is_hardened_and_job_scoped():
    args = runtime().proxy_args(NET, 1234.5)
    joined = " ".join(args)
    for flag in ("--user 65534:65534", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--network dua-res-job1", "--ip 172.30.252.2", f"DUA_PROXY_TOKEN={NET.token}",
                 "DUA_PROXY_BIND=172.30.252.2", "DUA_PROXY_DEADLINE=1234.500",
                 "DUA_PROXY_ALLOW=pypi.org,files.pythonhosted.org", "target=/opt/dua/egress_proxy.py,readonly"):
        assert flag in joined, flag


def test_start_job_creates_internal_network_without_host_address_then_proxy():
    fake = FakeDocker(fail={"network inspect": (1, "not found", "")})
    net = runtime(fake).start_job("job-1", 100.0)
    create = [c for c in fake.cmds(["network", "create"]) if "--internal" in c][0]
    assert "com.docker.network.bridge.inhibit_ipv4=true" in create and "--subnet" in create
    egress = [c for c in fake.cmds(["network", "create"]) if "--internal" not in c][0]
    assert egress[-1] == rr.EGRESS_NETWORK
    assert fake.cmds(["network", "connect"]) == [["network", "connect", rr.EGRESS_NETWORK, net.proxy_container]]
    assert len(net.token) >= 32 and net.proxy_ip.endswith(".2") and net.resolver_ip.endswith(".3")


def test_subnet_overlap_tries_the_next_subnet():
    fake = FakeDocker()
    attempts = []

    def executor(args, timeout):
        if args[1:3] == ["network", "create"] and "--internal" in args:
            attempts.append(args)
            if len(attempts) == 1:
                return subprocess.CompletedProcess(args, 1, "", "Pool overlaps with other one on this address space")
        return fake(args, timeout)
    net = rr.ResolverRuntime(IMAGE, executor=executor).start_job("job-1", 1.0)
    assert len(attempts) == 2 and net.subnet == "172.30.252.8/29"


def test_network_option_failure_is_resolver_unavailable():
    fake = FakeDocker()

    def executor(args, timeout):
        if args[1:3] == ["network", "create"] and "--internal" in args:
            return subprocess.CompletedProcess(args, 1, "", "invalid option inhibit_ipv4")
        return fake(args, timeout)
    with pytest.raises(rr.RuntimeUnavailable):
        rr.ResolverRuntime(IMAGE, executor=executor).start_job("job-1", 1.0)


def test_proxy_failure_cleans_up():
    fake = FakeDocker(fail={"network connect": (1, "boom", "")})
    with pytest.raises(rr.RuntimeUnavailable):
        runtime(fake).start_job("job-1", 1.0)
    assert ["rm", "-f", "dua-proxy-job1"] in fake.calls
    assert any(c[:2] == ["network", "rm"] for c in fake.calls)


def test_docker_unavailable():
    def executor(args, timeout):
        raise FileNotFoundError("docker")
    with pytest.raises(rr.RuntimeUnavailable):
        rr.ResolverRuntime(IMAGE, executor=executor).architecture()


def test_sweep_spares_active_jobs():
    fake = FakeDocker(fail={
        "ps -a": (0, "", "dua-res-a-proposed\tjob-a\ndua-proxy-b\tjob-b\nunrelated\t\n"),
        "network ls": (0, "", "dua-res-a\tjob-a\ndua-res-b\tjob-b\n"),
    })
    removed = runtime(fake).sweep({"job-b"})
    assert removed == ["dua-res-a-proposed", "dua-res-a"]
    assert ["rm", "-f", "dua-proxy-b"] not in fake.calls


def test_proxy_events_are_parsed():
    events = runtime().proxy_events(NET)
    assert events == [{"decision": "deny", "host": "evil.example", "reason": "host_not_allowed"}]


# -- a run with a fake docker process ---------------------------------------------------------


class FakeProc:
    def __init__(self, stdout=b"", stderr=b"", rc=0, hang=False):
        self.stdout, self.stderr = io.BytesIO(stdout), io.BytesIO(stderr)
        self.rc, self.hang, self.returncode, self.killed = rc, hang, None, False

    def poll(self):
        if self.hang and not self.killed:
            return None
        self.returncode = self.rc if not self.killed else 137
        return self.returncode

    def wait(self, timeout=None):
        return self.poll()

    def kill(self):
        self.killed = True


def run_with(proc, deadline=1e12, cancelled=lambda: False, clock=None, max_stdout=None):
    fake = FakeDocker()

    def executor(args, timeout):
        if args[1] == "kill":
            proc.killed = True
        return fake(args, timeout)
    rt = rr.ResolverRuntime(IMAGE, executor=executor, popen=lambda *a, **k: proc, **({"clock": clock} if clock else {}))
    result = rt.run(NET, "proposed", classify_requirements("numpy==1.24.0\n"), deadline, cancelled, max_stdout)
    return result, fake


FACTS = b'DUA_RUNTIME_FACTS {"python_full_version": "3.11.18", "machine": "x86_64"}\n'


def test_completed_run_separates_report_logs_and_facts():
    result, fake = run_with(FakeProc(stdout=b'{"version":"1"}', stderr=FACTS + b"Collecting numpy\n"))
    assert (result.state, result.exit_code, result.stdout) == ("completed", 0, b'{"version":"1"}')
    assert result.facts == {"python_full_version": "3.11.18", "machine": "x86_64"}
    assert "DUA_RUNTIME_FACTS" not in result.stderr and "Collecting numpy" in result.stderr
    assert ["rm", "-f", "dua-res-job1-proposed"] in fake.calls


def test_failed_pip_is_reported_with_its_exit_code():
    result, _ = run_with(FakeProc(stderr=FACTS + b"ERROR: ResolutionImpossible\n", rc=1))
    assert (result.state, result.exit_code, result.stdout) == ("completed", 1, b"")


def test_deadline_kills_the_container():
    now = [0.0]

    def clock():
        now[0] += 1
        return now[0]
    result, fake = run_with(FakeProc(hang=True), deadline=3.0, clock=clock)
    assert result.state == "timed_out" and result.exit_code is None
    assert ["kill", "--signal", "KILL", "dua-res-job1-proposed"] in fake.calls
    assert ["rm", "-f", "dua-res-job1-proposed"] in fake.calls


def test_cancellation_kills_the_container():
    result, fake = run_with(FakeProc(hang=True), cancelled=lambda: True)
    assert result.state == "cancelled" and ["kill", "--signal", "KILL", "dua-res-job1-proposed"] in fake.calls


def test_oversized_report_is_capped():
    result, _ = run_with(FakeProc(stdout=b"x" * 5000, stderr=FACTS), max_stdout=1000)
    assert len(result.stdout) == 1001  # one byte over the cap, so verification rejects it


def test_docker_failure_without_launcher_output_is_unavailable():
    with pytest.raises(rr.RuntimeUnavailable):
        run_with(FakeProc(stderr=b"docker: Error response from daemon: No such image\n", rc=125))


def test_input_directory_is_removed_after_the_run(monkeypatch):
    created = []
    real = rr.write_inputs

    def spy(manifest, root=None):
        path = real(manifest, root)
        created.append(path)
        return path
    monkeypatch.setattr(rr, "write_inputs", spy)
    run_with(FakeProc(stderr=FACTS))
    assert created and not Path(created[0]).exists()


def test_facts_parsing():
    assert rr.parse_facts("x\n" + FACTS.decode()) == {"python_full_version": "3.11.18", "machine": "x86_64"}
    assert rr.parse_facts("DUA_RUNTIME_FACTS not-json\n") is None
    assert rr.parse_facts("nothing") is None
