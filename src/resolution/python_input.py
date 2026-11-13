"""Supervisor-side classification of Python resolution inputs (FR-23, FR-26; ADR-009).

The classifier decides what the resolver is allowed to see. It accepts only plain requirement lines
(PEP 508 names, PEP 440 specifiers, markers, extras and per-line ``--hash``) and repository-local
``-c`` constraints files. Everything else is reported as an UNSUPPORTED item with a reason, so the
resolver never receives nested requirement files, editable or direct references, index options or
environment-variable expansion. The sanitized output is what the supervisor writes to the resolver's
read-only input directory.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from src.resolution.models import UnsupportedItem, UnsupportedReason

MAX_INPUT_BYTES = 1024 * 1024
MAX_CONSTRAINTS_BYTES = 256 * 1024
MAX_CONSTRAINT_FILES = 8
MAX_LINE_CHARS = 4096

_COMMENT = re.compile(r"(^|\s+)#.*$")
_ENV_VAR = re.compile(r"\$\{[^}]*\}")
_OPTION_SPLIT = re.compile(r"\s+(?=--?[A-Za-z])")
_HASH_VALUE = re.compile(r"^(sha256|sha384|sha512):[0-9a-fA-F]+$")
_HASH_LENGTHS = {"sha256": 64, "sha384": 96, "sha512": 128}
_VCS_PREFIXES = ("git+", "hg+", "svn+", "bzr+")
_ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")
_NESTED = ("-r", "--requirement")
_CONSTRAINT = ("-c", "--constraint")
_EDITABLE = ("-e", "--editable")


@dataclass(frozen=True)
class ConstraintsFile:
    path: str  # repository-relative POSIX path
    sha256: str  # digest of the file bytes as read (fingerprint input)
    content: str  # sanitized constraint lines given to the resolver


@dataclass(frozen=True)
class ClassifiedManifest:
    requirements: Tuple[str, ...] = ()
    constraints: Tuple[ConstraintsFile, ...] = ()
    unsupported: Tuple[UnsupportedItem, ...] = ()
    direct_names: Tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return not self.unsupported

    @property
    def sanitized_requirements(self) -> str:
        return "".join(line + "\n" for line in self.requirements)


@dataclass(frozen=True)
class PlanInput:
    original: ClassifiedManifest
    proposed: ClassifiedManifest


@dataclass
class _Context:
    source: str
    allow_constraints: bool
    manifest_path: Optional[str]
    workspace_dir: Optional[str]
    requirements: List[str] = field(default_factory=list)
    constraints: Dict[str, ConstraintsFile] = field(default_factory=dict)
    unsupported: List[UnsupportedItem] = field(default_factory=list)
    direct_names: List[str] = field(default_factory=list)

    def reject(self, reason: UnsupportedReason, message: str, line_number: Optional[int], text: Optional[str]) -> None:
        self.unsupported.append(
            UnsupportedItem(
                reason=reason,
                message=message,
                line_number=line_number,
                source=self.source,
                text=(text[:200] if text else None),
            )
        )


def classify_plan(
    manifest_type: Optional[str],
    ecosystem: Optional[str],
    file_path: Optional[str],
    original_content: str,
    proposed_content: str,
    workspace_dir: Optional[str] = None,
) -> PlanInput:
    """Classify both resolution roles of a plan. Each role is classified independently."""
    reason = _unsupported_manifest_kind(manifest_type, ecosystem, file_path)
    if reason is not None:
        item = UnsupportedItem(reason=reason[0], message=reason[1], source="manifest")
        blocked = ClassifiedManifest(unsupported=(item,))
        return PlanInput(original=blocked, proposed=blocked)
    return PlanInput(
        original=classify_requirements(original_content, manifest_path=file_path, workspace_dir=workspace_dir),
        proposed=classify_requirements(proposed_content, manifest_path=file_path, workspace_dir=workspace_dir),
    )


def classify_requirements(
    content: str,
    manifest_path: Optional[str] = None,
    workspace_dir: Optional[str] = None,
) -> ClassifiedManifest:
    ctx = _Context(source="manifest", allow_constraints=True, manifest_path=manifest_path, workspace_dir=workspace_dir)
    if len((content or "").encode("utf-8")) > MAX_INPUT_BYTES:
        ctx.reject(UnsupportedReason.INPUT_TOO_LARGE, f"The manifest is larger than {MAX_INPUT_BYTES} bytes.", None, None)
    else:
        _classify_lines(content or "", ctx)
    return ClassifiedManifest(
        requirements=tuple(ctx.requirements),
        constraints=tuple(ctx.constraints.values()),
        unsupported=tuple(ctx.unsupported),
        direct_names=tuple(dict.fromkeys(ctx.direct_names)),
    )


def _unsupported_manifest_kind(
    manifest_type: Optional[str], ecosystem: Optional[str], file_path: Optional[str]
) -> Optional[Tuple[UnsupportedReason, str]]:
    eco = str(getattr(ecosystem, "value", ecosystem) or "").lower()
    mtype = (manifest_type or "").lower()
    name = PurePosixPath(file_path or "").name.lower()
    if "npm" in eco or "node" in eco or mtype == "package.json" or name == "package.json":
        return (
            UnsupportedReason.NON_PYTHON,
            "npm compatibility verification is not yet supported. It is planned for a later milestone (M13).",
        )
    if mtype not in ("", "requirements.txt") and "requirements" not in mtype:
        if "pyproject" in mtype:
            return UnsupportedReason.PYPROJECT_ONLY, "pyproject.toml resolution is not supported."
        return UnsupportedReason.NON_PYTHON, f"Manifest type '{manifest_type}' is not supported for resolution."
    if name == "pyproject.toml":
        return UnsupportedReason.PYPROJECT_ONLY, "pyproject.toml resolution is not supported."
    return None


def _logical_lines(content: str):
    """Yield (first_line_number, text) with pip-style backslash continuations joined."""
    buffer: List[str] = []
    start = None
    for number, raw in enumerate(content.splitlines(), start=1):
        if start is None:
            start = number
        if raw.endswith("\\"):
            buffer.append(raw[:-1])
            continue
        buffer.append(raw)
        yield start, "".join(buffer)
        buffer, start = [], None
    if buffer:
        yield start, "".join(buffer)


def _classify_lines(content: str, ctx: _Context) -> None:
    for number, raw in _logical_lines(content):
        line = _COMMENT.sub("", raw).strip()
        if not line:
            continue
        if len(line) > MAX_LINE_CHARS:
            ctx.reject(UnsupportedReason.UNPARSEABLE, "The line is too long.", number, line)
            continue
        if _ENV_VAR.search(line):
            ctx.reject(UnsupportedReason.ENV_VARIABLE, "Environment variables are not expanded for resolution.",
                       number, line)
            continue
        if line.startswith("-"):
            _classify_option_line(line, number, ctx)
            continue
        _classify_requirement_line(line, number, ctx)


def _option_value(line: str, names: Tuple[str, ...]) -> Optional[str]:
    """Return the value if the line is one of the given options (-x v, -xv, --long v, --long=v)."""
    for name in names:
        if line == name:
            return ""
        if name.startswith("--"):
            if line.startswith(name + "=") or line.startswith(name + " "):
                return line[len(name) + 1:].strip()
        elif line.startswith(name):
            return line[len(name):].strip()
    return None


def _classify_option_line(line: str, number: int, ctx: _Context) -> None:
    if _option_value(line, _NESTED) is not None:
        ctx.reject(UnsupportedReason.NESTED_REQUIREMENTS, "Nested requirement files (-r) are not supported.",
                   number, line)
    elif _option_value(line, _EDITABLE) is not None:
        ctx.reject(UnsupportedReason.EDITABLE, "Editable requirements (-e) are not supported.", number, line)
    elif (value := _option_value(line, _CONSTRAINT)) is not None:
        if not ctx.allow_constraints:
            ctx.reject(UnsupportedReason.CONSTRAINTS_INVALID,
                       "Constraints files may not reference further files.", number, line)
        else:
            _add_constraints_file(value, number, line, ctx)
    else:
        ctx.reject(UnsupportedReason.PIP_OPTION, "This pip option is not supported for resolution.", number, line)


def _is_direct_reference(text: str) -> bool:
    lowered = text.lower()
    if "://" in lowered or lowered.startswith(_VCS_PREFIXES) or lowered.startswith("file:"):
        return True
    if text.startswith((".", "/", "~", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    head = re.split(r"[\s;\[<>=!~]", text, maxsplit=1)[0]
    return "/" in head or "\\" in head or head.lower().endswith(_ARCHIVE_SUFFIXES)


def _classify_requirement_line(line: str, number: int, ctx: _Context) -> None:
    parts = _OPTION_SPLIT.split(line, maxsplit=1)
    requirement_text = parts[0].strip()
    hashes: List[str] = []
    if len(parts) > 1:
        tokens = parts[1].split()
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.startswith("--hash="):
                value = token[len("--hash="):]
            elif token == "--hash" and i + 1 < len(tokens):
                i += 1
                value = tokens[i]
            else:
                ctx.reject(UnsupportedReason.PIP_OPTION, "Only --hash is supported after a requirement.", number, line)
                return
            if not _valid_hash(value):
                ctx.reject(UnsupportedReason.UNPARSEABLE, "The --hash value is not a valid digest.", number, line)
                return
            hashes.append(value.lower())
            i += 1
    if _is_direct_reference(requirement_text):
        ctx.reject(UnsupportedReason.DIRECT_REFERENCE, "URL, VCS and local-path requirements are not supported.",
                   number, line)
        return
    try:
        requirement = Requirement(requirement_text)
    except InvalidRequirement:
        ctx.reject(UnsupportedReason.UNPARSEABLE, "This line is not a valid requirement.", number, line)
        return
    if requirement.url:
        ctx.reject(UnsupportedReason.DIRECT_REFERENCE, "URL, VCS and local-path requirements are not supported.",
                   number, line)
        return
    sanitized = str(requirement)
    if hashes:
        sanitized += " " + " ".join(f"--hash={h}" for h in hashes)
    ctx.requirements.append(sanitized)
    ctx.direct_names.append(canonicalize_name(requirement.name))


def _valid_hash(value: str) -> bool:
    if not _HASH_VALUE.match(value):
        return False
    algorithm, digest = value.split(":", 1)
    return len(digest) == _HASH_LENGTHS[algorithm.lower()]


def _add_constraints_file(value: str, number: int, line: str, ctx: _Context) -> None:
    if not value or "://" in value or value.lower().startswith("file:"):
        ctx.reject(UnsupportedReason.CONSTRAINTS_OUTSIDE_WORKSPACE,
                   "Only constraints files inside the repository are supported.", number, line)
        return
    if not ctx.workspace_dir:
        ctx.reject(UnsupportedReason.CONSTRAINTS_OUTSIDE_WORKSPACE,
                   "Constraints files are supported only for repository analyses.", number, line)
        return
    workspace = Path(ctx.workspace_dir).resolve()
    manifest_dir = PurePosixPath(ctx.manifest_path or "").parent
    try:
        target = (workspace / manifest_dir / value).resolve()
        relative = target.relative_to(workspace)
    except (ValueError, OSError):
        ctx.reject(UnsupportedReason.CONSTRAINTS_OUTSIDE_WORKSPACE,
                   "The constraints file is outside the repository.", number, line)
        return
    rel = relative.as_posix()
    if rel in ctx.constraints:
        return
    if len(ctx.constraints) >= MAX_CONSTRAINT_FILES:
        ctx.reject(UnsupportedReason.CONSTRAINTS_INVALID,
                   f"At most {MAX_CONSTRAINT_FILES} constraints files are supported.", number, line)
        return
    if not target.is_file():
        ctx.reject(UnsupportedReason.CONSTRAINTS_INVALID, "The constraints file does not exist.", number, line)
        return
    try:
        if target.stat().st_size > MAX_CONSTRAINTS_BYTES:
            raise ValueError("too large")
        raw = target.read_bytes()
        if len(raw) > MAX_CONSTRAINTS_BYTES:
            raise ValueError("too large")
        text = raw.decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        ctx.reject(UnsupportedReason.CONSTRAINTS_INVALID,
                   f"The constraints file is unreadable or larger than {MAX_CONSTRAINTS_BYTES} bytes.", number, line)
        return
    inner = _Context(source=rel, allow_constraints=False, manifest_path=None, workspace_dir=None)
    _classify_lines(text, inner)
    ctx.unsupported.extend(inner.unsupported)
    if inner.unsupported:
        return
    ctx.constraints[rel] = ConstraintsFile(
        path=rel,
        sha256=hashlib.sha256(raw).hexdigest(),
        content="".join(r + "\n" for r in inner.requirements),
    )
