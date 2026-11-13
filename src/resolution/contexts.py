"""Supervisor contexts from server-held plan state (shared by the job service and enforcement).

Every input comes from the stored proposal and the run's own workspace, never from a client. The same
function builds the contexts when a job runs and when evidence is checked later, so any change in the
plan, its constraints files, the pinned image or the resolver policy changes the fingerprint and
invalidates earlier evidence (FR-27c).
"""
from __future__ import annotations

import platform
from typing import Optional, Tuple

from src.models.plan import UpgradeProposalResponse
from src.resolution import target as target_facts
from src.resolution.fingerprint import ResolutionEnvironment, ResolutionEnvironmentError, resolution_environment
from src.resolution.models import ResolutionReport, ResolutionRole
from src.resolution.python_input import classify_plan
from src.resolution.verifier import ResolutionContext


def build_contexts(proposal_id: str, proposal: UpgradeProposalResponse, workspace_dir: Optional[str],
                   env: ResolutionEnvironment) -> Tuple[ResolutionContext, ResolutionContext]:
    plan = classify_plan(proposal.manifest_type, getattr(proposal.ecosystem, "value", proposal.ecosystem),
                         proposal.file_path, proposal.original_content, proposal.proposed_content, workspace_dir)
    original = ResolutionContext(proposal_id=proposal_id, role=ResolutionRole.ORIGINAL, env=env,
                                 manifest=plan.original, original_content=proposal.original_content)
    proposed = ResolutionContext(proposal_id=proposal_id, role=ResolutionRole.PROPOSED, env=env,
                                 manifest=plan.proposed, original_content=proposal.original_content,
                                 proposed_content=proposal.proposed_content, unified_diff=proposal.unified_diff)
    return original, proposed


def environment_from_report(report: ResolutionReport) -> ResolutionEnvironment:
    """The current trusted environment for the architecture recorded in supervisor-written evidence.

    The image, policy and allow-list are today's configuration, so evidence produced under anything
    else no longer binds.
    """
    machine = (report.target or {}).get("machine") or ""
    return resolution_environment(machine)


def host_environment() -> ResolutionEnvironment:
    """Environment for evidence that never runs a container (UNSUPPORTED input): the API host's arch."""
    try:
        return resolution_environment(platform.machine())
    except ResolutionEnvironmentError:
        return resolution_environment("amd64")


def check_runtime_facts(facts: Optional[dict], env: ResolutionEnvironment) -> Optional[str]:
    """Compare what the running container reported with the trusted target facts."""
    if not isinstance(facts, dict):
        return "the resolver did not report its runtime facts"
    expected = {
        "python_full_version": target_facts.TARGET_PYTHON_FULL_VERSION,
        "machine": target_facts.target_machine(env),
        "implementation": "cpython",
    }
    for key, value in expected.items():
        if facts.get(key) != value:
            return f"runtime {key} is {str(facts.get(key))[:50]!r}, expected {value!r}"
    libc = facts.get("libc")
    glibc = ".".join(str(x) for x in target_facts.TARGET_GLIBC)
    if not (isinstance(libc, list) and len(libc) == 2 and libc[0] == "glibc" and libc[1] == glibc):
        return f"runtime libc is {str(libc)[:50]!r}, expected glibc {glibc}"
    if facts.get("uid") == 0:
        return "the resolver ran as root"
    return None
