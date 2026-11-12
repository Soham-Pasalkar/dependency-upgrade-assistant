"""Shared helpers for the live M12 tests (real Docker, real pip in the pinned image, real PyPI).

Outside the CI Docker job these skip when Docker is unavailable; with DUA_REQUIRE_DOCKER=1 they fail.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import List, Optional

import pytest

from src.resolution.contexts import check_runtime_facts
from src.resolution.fingerprint import resolution_environment
from src.resolution.metadata_client import Deadline, PyPIMetadataClient
from src.resolution.python_resolver import RunOutcome, evaluate_run
from src.resolution.resolver_runtime import ResolverRuntime, RuntimeUnavailable, default_runtime


def require_docker() -> ResolverRuntime:
    try:
        rt = default_runtime()
        rt.architecture()
        return rt
    except RuntimeUnavailable as err:
        if os.environ.get("DUA_REQUIRE_DOCKER") == "1":
            pytest.fail(f"Docker is required in this job but unavailable: {err}")
        pytest.skip(f"Live Docker daemon unavailable: {err}")


def docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def env_for(rt: ResolverRuntime):
    return resolution_environment(rt.architecture())


def resolve(rt: ResolverRuntime, ctx, seconds: float = 180, cancelled=lambda: False):
    """One real run through the production runtime and the real supervisor."""
    job = uuid.uuid4().hex
    net = rt.start_job(job, time.time() + 2 * seconds)
    try:
        deadline = time.monotonic() + seconds
        result = rt.run(net, ctx.role.value, ctx.manifest, deadline, cancelled)
        report = None
        if result.state == "completed":
            problem = check_runtime_facts(result.facts, ctx.env)
            assert problem is None, problem
            report = evaluate_run(ctx, RunOutcome(result.exit_code, result.stdout, result.stderr),
                                  PyPIMetadataClient(), Deadline(at=deadline))
        return result, report, net
    finally:
        rt.stop_job(net)


def leftovers(job_id: str) -> List[str]:
    ps = docker("ps", "-a", "--filter", f"label=dua.job={job_id}", "--format", "{{.Names}}").stdout.split()
    nets = docker("network", "ls", "--filter", f"label=dua.job={job_id}", "--format", "{{.Name}}").stdout.split()
    return ps + nets


def probe(rt: ResolverRuntime, net, code: str, *argv: str, network: Optional[str] = None) -> dict:
    """Run Python code in a container built with the production resolver arguments."""
    from src.resolution.python_input import classify_requirements
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    tmp.chmod(0o755)
    args = rt.resolver_args(net, "probe", tmp, classify_requirements("x==1\n"))
    image_at = args.index(rt.image)
    args = args[:image_at + 1] + ["python3", "-I", "-c", code, *argv]
    if network:
        args[args.index("--network") + 1] = network
        del args[args.index("--ip"):args.index("--ip") + 2]
    name = args[args.index("--name") + 1]
    try:
        out = docker(*args, timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        docker("rm", "-f", name)
