"""Map a finished resolver run to supervisor evidence (FR-23; ADR-009, ADR-011).

Run contract (implemented by the runtime in P5): pip writes its report to a file on the container's
tmpfs; pip's own output is redirected to stderr; on exit 0 the report file is copied to stdout. So
stdout carries only the report and stderr carries pip's log, including the "The conflict is caused
by:" lines that pip prints at INFO level.

Trust rule: everything the resolver produced is untrusted. Only exit code 0 with a report that the
supervisor verifies can become COMPATIBLE. Log text can only ever lead to CONFLICT, UNSUPPORTED or
RESOLUTION_FAILED, all of which block approval, and the CONFLICT/UNSUPPORTED distinction is decided by
the supervisor from PyPI's index, not from the log.

pip's command arguments are the fixed ``policy.PIP_POLICY_ARGS``; ``--quiet`` is deliberately absent
because it suppresses the conflict causes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from src.resolution import target as target_facts
from src.resolution.closure import parse_input_lines
from src.resolution.index_analysis import satisfying, summarize
from src.resolution.metadata_client import Deadline, MetadataFetchError, PyPIMetadataClient
from src.resolution.models import (
    ConflictDiagnostic,
    ResolutionReport,
    ResolutionStatus,
    UnsupportedItem,
    UnsupportedReason,
)
from src.resolution.verifier import ResolutionContext, build_report, clean_text, failed_report, verify_success

MAX_LOG_CHARS = 1_000_000
MAX_CAUSES = 50
_CAUSES_HEADER = "The conflict is caused by:"
_CONFLICT_SUMMARY = re.compile(r"(Cannot install .{1,500}? because these package versions have conflicting dependencies\.)")
_USER_REQUESTED = re.compile(r"^The user requested (?:\(constraint\) )?(.+)$")
_DEPENDS_ON = re.compile(r"^(\S+) (\S+) depends on (.+)$")
_NO_MATCH = re.compile(r"Could not find a version that satisfies the requirement (.{1,500}?) \(from versions:")

# Statuses each kind of run may produce. Anything else is a supervisor bug and fails loudly.
_ALLOWED = {
    True: {ResolutionStatus.COMPATIBLE, ResolutionStatus.RESOLUTION_FAILED},
    False: {ResolutionStatus.CONFLICT, ResolutionStatus.UNSUPPORTED, ResolutionStatus.RESOLUTION_FAILED},
}


@dataclass(frozen=True)
class RunOutcome:
    exit_code: int
    stdout: bytes
    stderr: str


def evaluate_run(
    ctx: ResolutionContext,
    outcome: RunOutcome,
    client: PyPIMetadataClient,
    deadline: Deadline,
    now: Optional[float] = None,
) -> ResolutionReport:
    """The single entry point from a finished run to evidence. DeadlineExceeded propagates."""
    if not ctx.manifest.supported:
        raise ValueError("unsupported input must never be resolved")
    succeeded = outcome.exit_code == 0
    if succeeded:
        report = verify_success(ctx, outcome.stdout, client, deadline, now=now)
    elif outcome.stdout.strip():
        report = failed_report(ctx, "contradictory_output", "the resolver failed but wrote a report", now=now)
    else:
        report = classify_failure(ctx, outcome.stderr, client, deadline, now=now)
    if report.status not in _ALLOWED[succeeded]:
        raise RuntimeError(f"invalid status {report.status} for exit code {outcome.exit_code}")
    return report


def parse_conflict_causes(log: str) -> List[str]:
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == _CAUSES_HEADER:
            causes = []
            for follow in lines[i + 1:]:
                if not follow.strip():
                    break
                causes.append(clean_text(follow))
                if len(causes) >= MAX_CAUSES:
                    break
            return causes
    return []


def _requirement(text: str) -> Requirement:
    return Requirement(text.strip().replace(" and ", ","))


def _cause_constraints(causes: List[str]) -> Optional[Tuple[Dict[str, List[SpecifierSet]], set]]:
    specs: Dict[str, List[SpecifierSet]] = {}
    involved = set()
    for cause in causes:
        user = _USER_REQUESTED.match(cause)
        depends = _DEPENDS_ON.match(cause)
        try:
            if user:
                req = _requirement(user.group(1))
            elif depends:
                involved.add(canonicalize_name(depends.group(1)))
                req = _requirement(depends.group(3))
            else:
                return None
        except InvalidRequirement:
            return None
        name = canonicalize_name(req.name)
        involved.add(name)
        specs.setdefault(name, []).append(req.specifier)
    return specs, involved


def _input_specifiers(ctx: ResolutionContext) -> Dict[str, List[SpecifierSet]]:
    """Specifiers the user's own input puts on each package (direct requirements and constraints)."""
    specs: Dict[str, List[SpecifierSet]] = {}
    marker_env = target_facts.marker_environment(ctx.env)
    lines = list(ctx.manifest.requirements) + [
        line for c in ctx.manifest.constraints for line in c.content.splitlines() if line]
    for item in parse_input_lines(lines):
        req = item.requirement
        if req.marker is None or req.marker.evaluate({**marker_env, "extra": ""}):
            specs.setdefault(item.canonical, []).append(req.specifier)
    return specs


def _combine(specs: List[SpecifierSet]) -> SpecifierSet:
    return SpecifierSet(",".join(str(s) for s in specs if str(s)))


def classify_failure(
    ctx: ResolutionContext,
    log: str,
    client: PyPIMetadataClient,
    deadline: Deadline,
    now: Optional[float] = None,
) -> ResolutionReport:
    log = (log or "")[:MAX_LOG_CHARS]
    tags = target_facts.supported_tags(ctx.env)
    target_python = target_facts.target_python()
    user_specs = _input_specifiers(ctx)
    try:
        if "ResolutionImpossible" in log or _CAUSES_HEADER in log:
            return _classify_conflict(ctx, log, client, deadline, tags, target_python, user_specs, now)
        unmatched = _NO_MATCH.findall(log)
        if unmatched:
            return _classify_no_match(ctx, unmatched, client, deadline, tags, target_python, user_specs, now)
    except MetadataFetchError as err:
        return failed_report(ctx, "fetch_failed", str(err), now=now)
    first_error = next((clean_text(l) for l in log.splitlines() if l.startswith("ERROR:")), "no error message")
    return failed_report(ctx, "resolver_error", first_error, now=now)


def _classify_conflict(ctx, log, client, deadline, tags, target_python, user_specs, now) -> ResolutionReport:
    causes = parse_conflict_causes(log)
    parsed = _cause_constraints(causes) if causes else None
    if not parsed:
        return failed_report(ctx, "unparsed_conflict", "pip reported a conflict without parseable causes", now=now)
    cause_specs, involved = parsed
    source_only = []
    for name in sorted(involved):
        combined = _combine(cause_specs.get(name, []) + user_specs.get(name, []))
        versions = summarize(client.project_index(name, deadline), tags, target_python)
        candidates = satisfying(versions, combined)
        if any(versions[v].admitted_sdist and not versions[v].compatible_wheel for v in candidates):
            source_only.append(name)
    summary_match = _CONFLICT_SUMMARY.search(log)
    summary = clean_text(summary_match.group(1)) if summary_match else "The requirements conflict."
    if source_only:
        return build_report(ctx, ResolutionStatus.UNSUPPORTED, unsupported=[UnsupportedItem(
            reason=UnsupportedReason.SOURCE_ONLY_CONFLICT,
            message=(f"The conflict involves {', '.join(source_only)}, which has releases available only as source "
                     "distributions for the target. M12 does not build from source, so the conflict cannot be "
                     "confirmed."),
        )], conflicts=[ConflictDiagnostic(summary=summary, causes=causes)], failure="source_only_conflict", now=now)
    return build_report(ctx, ResolutionStatus.CONFLICT, conflicts=[ConflictDiagnostic(summary=summary, causes=causes)],
                        failure="conflict", now=now)


def _classify_no_match(ctx, unmatched, client, deadline, tags, target_python, user_specs, now) -> ResolutionReport:
    conflicts, unsupported, failures = [], [], []
    for text in unmatched[:20]:
        text = clean_text(text, 500)
        try:
            req = _requirement(text)
        except InvalidRequirement:
            failures.append(f"unparseable requirement in resolver output: {text[:100]}")
            continue
        name = canonicalize_name(req.name)
        spec = _combine([req.specifier] + user_specs.get(name, []))
        try:
            versions = summarize(client.project_index(name, deadline), tags, target_python)
        except MetadataFetchError as err:
            if err.code == "not_found":
                failures.append(f"{name} is not on PyPI")
                continue
            raise
        candidates = [v for v in satisfying(versions, spec) if not versions[v].all_yanked] or \
                     [v for v in satisfying(versions, spec, allow_prereleases=True) if not versions[v].all_yanked]
        if not candidates:
            failures.append(f"no release of {name} satisfies {spec or 'the requirement'}")
            continue
        admitted = [v for v in candidates if versions[v].any_admitted]
        if not admitted:
            needs = sorted({rp for v in candidates for rp in versions[v].excluded_by_python})
            conflicts.append(ConflictDiagnostic(
                summary=f"{text} requires a different Python version than the target "
                        f"({target_facts.TARGET_PYTHON_FULL_VERSION}).",
                causes=[f"{name} requires Python {rp}" for rp in needs[:10]],
            ))
        elif any(versions[v].admitted_sdist and not versions[v].compatible_wheel for v in admitted):
            unsupported.append(UnsupportedItem(
                reason=UnsupportedReason.SOURCE_BUILD_REQUIRED,
                message=f"{text} is available only as a source distribution for the target. "
                        "M12 does not build packages from source.",
                text=text,
            ))
        else:
            failures.append(f"no wheel of {name} matching {spec or 'the requirement'} is installable on the target")
    if unsupported:
        return build_report(ctx, ResolutionStatus.UNSUPPORTED, unsupported=unsupported, conflicts=conflicts,
                            failure="source_build_required", now=now)
    if conflicts:
        return build_report(ctx, ResolutionStatus.CONFLICT, conflicts=conflicts, failure="requires_python", now=now)
    return failed_report(ctx, "no_matching_distribution", "; ".join(failures)[:900] or "no matching distribution",
                         now=now)
