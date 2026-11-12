"""Trusted supervisor verification of a successful resolver run (FR-27; ADR-011).

The resolver's stdout is untrusted. A run becomes COMPATIBLE evidence only when the supervisor has:
parsed the report strictly (pip_report), checked it was produced for the trusted target, matched
every selected file by filename, URL and SHA-256 in PyPI's own index, fetched each wheel's metadata
itself (PEP 658 file checked against the index hash, else the per-version JSON API with a warning),
and proven the closure of the set against that metadata (closure). Everything that identifies the
evidence (role, fingerprint, inputs, image, policy, target) comes from the supervisor-held
``ResolutionContext``, never from the report. Any failure yields RESOLUTION_FAILED with a reason.
"""
from __future__ import annotations

import email.parser
import email.policy
import re
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from src.api.config import settings
from src.resolution import target as target_facts
from src.resolution.closure import ClosureError, SelectedPackage, check_closure, parse_input_lines
from src.resolution.fingerprint import ResolutionEnvironment, common_digest, compute_fingerprint, sha256_hex
from src.resolution.index_analysis import summarize
from src.resolution.metadata_client import Deadline, MetadataFetchError, PyPIMetadataClient
from src.resolution.models import (
    ConflictDiagnostic,
    ResolutionReport,
    ResolutionRole,
    ResolutionStatus,
    ResolvedPackage,
    UnsupportedItem,
)
from src.resolution.pip_report import ReportRejected, parse_report
from src.resolution.policy import COMPATIBILITY_DISCLOSURE
from src.resolution.python_input import ClassifiedManifest
from src.resolution.upload_dates import resolve_upload_date

MAX_REQUIRES_DIST = 1000


@dataclass(frozen=True)
class ResolutionContext:
    """Supervisor-held facts about one resolution role. The only source of evidence identity."""

    proposal_id: str
    role: ResolutionRole
    env: ResolutionEnvironment
    manifest: ClassifiedManifest
    original_content: str
    proposed_content: Optional[str] = None
    unified_diff: Optional[str] = None

    def fingerprint(self) -> str:
        return compute_fingerprint(self.role, self.env, self.proposal_id, self.original_content,
                                   self.manifest.constraints, self.proposed_content, self.unified_diff)

    def common_digest(self) -> str:
        return common_digest(self.env, self.proposal_id, self.manifest.constraints)

    def inputs(self) -> dict:
        data = {
            "proposal_id": self.proposal_id,
            "original_sha256": sha256_hex(self.original_content),
            "constraints": sorted(({"path": c.path, "sha256": c.sha256} for c in self.manifest.constraints),
                                  key=lambda c: c["path"]),
            "common_digest": self.common_digest(),
        }
        if ResolutionRole(self.role) == ResolutionRole.PROPOSED:
            data["proposed_sha256"] = sha256_hex(self.proposed_content or "")
            data["diff_sha256"] = sha256_hex(self.unified_diff or "")
        return data

    def target(self) -> dict:
        return {
            "python": target_facts.TARGET_PYTHON_FULL_VERSION,
            "implementation": self.env.target_python,
            "platform": self.env.target_platform,
            "machine": target_facts.target_machine(self.env),
        }


def build_report(
    ctx: ResolutionContext,
    status: ResolutionStatus,
    *,
    packages: Sequence[ResolvedPackage] = (),
    conflicts: Sequence[ConflictDiagnostic] = (),
    unsupported: Sequence[UnsupportedItem] = (),
    warnings: Sequence[str] = (),
    failure: Optional[str] = None,
    resolver: Optional[str] = None,
    now: Optional[float] = None,
) -> ResolutionReport:
    """The only constructor of resolution evidence."""
    return ResolutionReport(
        policy_version=ctx.env.policy_version,
        role=ctx.role,
        status=status,
        ecosystem=ctx.env.ecosystem,
        fingerprint=ctx.fingerprint(),
        resolver=resolver,
        image=ctx.env.image,
        target=ctx.target(),
        allowlist_version=ctx.env.allowlist_version,
        inputs=ctx.inputs(),
        packages=list(packages),
        conflicts=list(conflicts),
        unsupported=list(unsupported),
        warnings=list(warnings),
        disclosure=COMPATIBILITY_DISCLOSURE if status == ResolutionStatus.COMPATIBLE else None,
        failure=failure,
        created_at=now if now is not None else time.time(),
    )


def failed_report(ctx: ResolutionContext, code: str, detail: str, *, resolver: Optional[str] = None,
                  warnings: Sequence[str] = (), now: Optional[float] = None) -> ResolutionReport:
    return build_report(ctx, ResolutionStatus.RESOLUTION_FAILED, failure=f"{code}: {detail}"[:1000],
                        resolver=resolver, warnings=warnings, now=now)


def unsupported_report(ctx: ResolutionContext, now: Optional[float] = None) -> ResolutionReport:
    """Evidence for input that is never sent to the resolver (FR-23)."""
    if ctx.manifest.supported:
        raise ValueError("unsupported_report requires classified UNSUPPORTED items")
    return build_report(ctx, ResolutionStatus.UNSUPPORTED, unsupported=ctx.manifest.unsupported,
                        failure="unsupported_input", now=now)


def check_binding(report: ResolutionReport, ctx: ResolutionContext) -> Optional[str]:
    """Return why stored evidence does not belong to this context, or None if it does."""
    if report.role != ResolutionRole(ctx.role):
        return "role_mismatch"
    if report.policy_version != ctx.env.policy_version or report.ecosystem != ctx.env.ecosystem:
        return "policy_mismatch"
    if report.image != ctx.env.image:
        return "image_mismatch"
    if report.allowlist_version != ctx.env.allowlist_version:
        return "allowlist_mismatch"
    if report.target != ctx.target():
        return "target_mismatch"
    if report.inputs != ctx.inputs():
        return "inputs_mismatch"
    if report.fingerprint != ctx.fingerprint():
        return "fingerprint_mismatch"
    return None


@dataclass(frozen=True)
class _CoreMetadata:
    name: str
    version: Version
    requires_dist: Tuple[Requirement, ...]
    requires_python: Optional[SpecifierSet]


def _requirements(values: Iterable[str], where: str) -> Tuple[Requirement, ...]:
    values = list(values)
    if len(values) > MAX_REQUIRES_DIST:
        raise ClosureError("malformed_metadata", f"{where}: too many Requires-Dist entries")
    out = []
    for value in values:
        try:
            out.append(Requirement(str(value)))
        except InvalidRequirement as err:
            raise ClosureError("malformed_metadata", f"{where}: invalid Requires-Dist {str(value)[:100]!r}") from err
    return tuple(out)


def _specifier(value: Optional[str], where: str) -> Optional[SpecifierSet]:
    if value is None or not str(value).strip():
        return None
    try:
        return SpecifierSet(str(value))
    except InvalidSpecifier as err:
        raise ClosureError("malformed_metadata", f"{where}: invalid Requires-Python") from err


def parse_core_metadata(body: bytes, where: str) -> _CoreMetadata:
    message = email.parser.BytesParser(policy=email.policy.compat32).parsebytes(body, headersonly=True)
    name, version = message.get("Name"), message.get("Version")
    if not name or not version:
        raise ClosureError("malformed_metadata", f"{where}: Name or Version missing")
    try:
        parsed_version = Version(str(version).strip())
    except InvalidVersion as err:
        raise ClosureError("malformed_metadata", f"{where}: invalid Version") from err
    return _CoreMetadata(
        name=str(name).strip(),
        version=parsed_version,
        requires_dist=_requirements(message.get_all("Requires-Dist") or [], where),
        requires_python=_specifier(message.get("Requires-Python"), where),
    )


def _json_metadata(info: dict, where: str) -> _CoreMetadata:
    requires = info.get("requires_dist") or []
    if not isinstance(requires, list):
        raise ClosureError("malformed_metadata", f"{where}: requires_dist is not a list")
    try:
        version = Version(str(info.get("version")))
    except InvalidVersion as err:
        raise ClosureError("malformed_metadata", f"{where}: invalid version") from err
    return _CoreMetadata(name=str(info.get("name") or ""), version=version,
                         requires_dist=_requirements(requires, where),
                         requires_python=_specifier(info.get("requires_python"), where))


_ENV_KEYS_CHECKED = ("python_full_version", "python_version", "sys_platform", "platform_machine",
                     "platform_system", "implementation_name", "platform_python_implementation", "os_name")


def verify_success(
    ctx: ResolutionContext,
    raw: bytes,
    client: PyPIMetadataClient,
    deadline: Deadline,
    *,
    max_report_bytes: Optional[int] = None,
    max_packages: Optional[int] = None,
    now: Optional[float] = None,
) -> ResolutionReport:
    """Turn a successful run's untrusted stdout into COMPATIBLE or RESOLUTION_FAILED evidence."""
    if not ctx.manifest.supported:
        raise ValueError("unsupported input must never be resolved")
    max_report_bytes = max_report_bytes or settings.RESOLVER_MAX_REPORT_MB * 1024 * 1024
    max_packages = max_packages or settings.RESOLVER_MAX_PACKAGES
    try:
        parsed = parse_report(raw, max_report_bytes, max_packages)
    except ReportRejected as err:
        return failed_report(ctx, f"report_{err.code}", err.detail, now=now)
    resolver = f"pip {parsed.pip_version}"

    def fail(code: str, detail: str, warnings: Sequence[str] = ()) -> ResolutionReport:
        return failed_report(ctx, code, detail, resolver=resolver, warnings=warnings, now=now)

    marker_env = target_facts.marker_environment(ctx.env)
    for key in _ENV_KEYS_CHECKED:
        if parsed.environment.get(key) != marker_env[key]:
            return fail("environment_mismatch", f"report environment {key} is not the trusted target")
    tags = target_facts.supported_tags(ctx.env)
    target_python = target_facts.target_python()

    warnings: List[str] = []
    selected: Dict[str, SelectedPackage] = {}
    display: Dict[str, str] = {}
    entries = {}
    indexes = {}
    try:
        for item in parsed.items:
            if not item.wheel_tags & tags:
                return fail("incompatible_wheel", f"{item.filename} is not installable on the target")
            index = client.project_index(item.canonical, deadline)
            matches = index.entries_named(item.filename)
            if not matches:
                return fail("file_not_in_index", f"{item.filename} is not listed by PyPI for {item.canonical}")
            if len(matches) > 1:
                return fail("ambiguous_index_entry", f"{item.filename} is listed more than once")
            entry = matches[0]
            if entry.sha256 is None or entry.sha256 != item.sha256:
                return fail("hash_mismatch", f"{item.filename}: SHA-256 differs from PyPI's")
            if entry.url != item.url:
                return fail("url_mismatch", f"{item.filename}: URL differs from PyPI's")
            where = f"{item.canonical} {item.version}"
            if entry.metadata_sha256:
                meta = parse_core_metadata(client.metadata_file(entry.url, entry.metadata_sha256, deadline), where)
            else:
                vj = client.version_json(item.canonical, str(item.version), deadline)
                meta = _json_metadata(vj.data["info"], where)
                warnings.append(f"release_level_metadata: {where}")
            if canonicalize_name(meta.name) != item.canonical or meta.version != item.version:
                return fail("metadata_mismatch", f"{where}: PyPI metadata names a different project or version")
            if entry.requires_python is not None:
                rp = _specifier(entry.requires_python, where)
                if rp is not None and not rp.contains(target_python, prereleases=True):
                    return fail("requires_python", f"{where} requires Python {rp}")
            selected[item.canonical] = SelectedPackage(
                canonical=item.canonical, version=item.version, sha256=item.sha256,
                requires_dist=meta.requires_dist, requires_python=meta.requires_python,
            )
            display[item.canonical] = meta.name
            entries[item.canonical] = entry
            indexes[item.canonical] = index
    except MetadataFetchError as err:
        return fail("fetch_failed", str(err), warnings)
    except ClosureError as err:
        return fail(err.code, err.detail, warnings)

    try:
        closure = check_closure(
            parse_input_lines(ctx.manifest.requirements),
            parse_input_lines(line for c in ctx.manifest.constraints for line in c.content.splitlines() if line),
            selected, marker_env, target_python,
        )
    except ClosureError as err:
        return fail(err.code, err.detail, warnings)

    requested = {i.canonical for i in parsed.items if i.requested}
    if requested != closure.direct:
        return fail("requested_mismatch", "the report's requested packages differ from the applicable direct requirements",
                    warnings)
    for item in parsed.items:
        if set(item.requested_extras) != closure.direct_extras.get(item.canonical, set()):
            return fail("extras_mismatch", f"{item.canonical}: requested extras differ from the input", warnings)

    for name, entry in entries.items():
        if entry.yanked:
            if name not in closure.exact_pins:
                return fail("yanked_not_permitted", f"{name} {selected[name].version} is yanked and not pinned with ==",
                            warnings)
            reason = f" ({entry.yanked_reason[:200]})" if entry.yanked_reason else ""
            warnings.append(f"yanked: {name}=={selected[name].version}{reason}")

    for name, package in selected.items():
        if not package.version.is_prerelease:
            continue
        specs = closure.specifiers.get(name, [])
        if any(spec.prereleases for spec in specs):
            continue
        combined = SpecifierSet(",".join(str(s) for s in specs if str(s)))
        versions = summarize(indexes[name], tags, target_python)
        if any(not v.is_prerelease and info.compatible_wheel and combined.contains(v)
               for v, info in versions.items()):
            return fail("prerelease_not_permitted",
                        f"{name} {package.version} is a prerelease and no requirement pins a prerelease", warnings)

    try:
        packages = []
        for name in sorted(selected):
            package, entry = selected[name], entries[name]
            packages.append(ResolvedPackage(
                name=display[name], version=str(package.version), direct=name in closure.direct,
                requested_extras=sorted(closure.direct_extras.get(name, set())),
                constraints=sorted({str(s) for s in closure.specifiers.get(name, []) if str(s)}),
                filename=entry.filename,
                sha256=package.sha256,
                upload=resolve_upload_date(client, indexes[name], entry, name, str(package.version),
                                           package.sha256, deadline),
            ))
    except MetadataFetchError as err:  # pragma: no cover - resolve_upload_date absorbs fetch errors
        return fail("fetch_failed", str(err), warnings)
    return build_report(ctx, ResolutionStatus.COMPATIBLE, packages=packages, warnings=warnings, resolver=resolver,
                        now=now)


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(value: str, limit: int = 300) -> str:
    """Untrusted resolver text, made safe to store and display."""
    return _CONTROL.sub("", value).strip()[:limit]
