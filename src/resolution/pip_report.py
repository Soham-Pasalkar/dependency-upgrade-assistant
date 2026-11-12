"""Strict parser for the resolver's untrusted stdout: pip's installation report, version 1.

This module only establishes that the bytes form a well-formed, bounded report about wheels hosted on
``files.pythonhosted.org`` for the target. It trusts nothing semantic: package metadata, dependency
data, ``is_yanked`` and the ``environment`` block are re-derived or checked by the supervisor
(verifier.py) against PyPI and the trusted target facts. Any deviation raises ``ReportRejected``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, Tuple
from urllib.parse import unquote, urlsplit

from packaging.tags import Tag
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

FILES_HOST = "files.pythonhosted.org"
REPORT_KEYS = frozenset({"version", "pip_version", "install", "environment"})
ITEM_REQUIRED = frozenset({"download_info", "is_direct", "is_yanked", "requested", "metadata"})
ITEM_OPTIONAL = frozenset({"requested_extras"})
DOWNLOAD_KEYS = frozenset({"url", "archive_info"})
ARCHIVE_KEYS = frozenset({"hash", "hashes"})
MAX_DEPTH = 32
MAX_NAME = 200
MAX_VERSION = 100
MAX_URL = 2048
MAX_EXTRAS = 32
MAX_ENV_KEYS = 64
MAX_ENV_VALUE = 512
_PIP_VERSION = re.compile(r"^[0-9]+(\.[0-9]+){0,3}([a-z0-9.+-]{0,20})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReportRejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ReportItem:
    name: str
    canonical: str
    version: Version
    filename: str
    url: str
    sha256: str
    requested: bool
    requested_extras: Tuple[str, ...]
    wheel_tags: FrozenSet[Tag]


@dataclass(frozen=True)
class ParsedReport:
    pip_version: str
    environment: Dict[str, str]
    items: Tuple[ReportItem, ...]

    def by_name(self) -> Dict[str, ReportItem]:
        return {i.canonical: i for i in self.items}


def _no_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ReportRejected("malformed", f"duplicate JSON key {str(key)[:50]!r}")
        seen[key] = value
    return seen


def _check_depth(value, limit: int = MAX_DEPTH) -> None:
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            raise ReportRejected("too_deep", f"nesting deeper than {limit}")
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)


def _str(value, limit: int, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ReportRejected("malformed", f"{what} must be a non-empty string of at most {limit} characters")
    return value


def parse_report(raw: bytes, max_bytes: int, max_packages: int) -> ParsedReport:
    if not isinstance(raw, (bytes, bytearray)):
        raise ReportRejected("malformed", "report is not bytes")
    if len(raw) > max_bytes:
        raise ReportRejected("too_large", f"report exceeds {max_bytes} bytes")
    if not raw.strip():
        raise ReportRejected("empty", "no report on stdout")
    try:
        data = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except ReportRejected:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as err:
        raise ReportRejected("malformed", "stdout is not valid UTF-8 JSON") from err
    if not isinstance(data, dict):
        raise ReportRejected("malformed", "report is not a JSON object")
    _check_depth(data)
    keys = set(data)
    if keys != REPORT_KEYS:
        raise ReportRejected("schema", f"unexpected or missing top-level keys: {sorted(keys ^ REPORT_KEYS)[:10]}")
    if data["version"] != "1":
        raise ReportRejected("schema", "report version must be '1'")
    pip_version = _str(data["pip_version"], 32, "pip_version")
    if not _PIP_VERSION.match(pip_version):
        raise ReportRejected("schema", "invalid pip_version")
    environment = data["environment"]
    if not isinstance(environment, dict) or len(environment) > MAX_ENV_KEYS or not all(
        isinstance(k, str) and isinstance(v, str) and len(k) <= 64 and len(v) <= MAX_ENV_VALUE
        for k, v in environment.items()
    ):
        raise ReportRejected("schema", "environment must be a bounded string mapping")
    install = data["install"]
    if not isinstance(install, list):
        raise ReportRejected("schema", "install must be a list")
    if not install:
        raise ReportRejected("empty_resolution", "the report selects no packages")
    if len(install) > max_packages:
        raise ReportRejected("too_many_packages", f"more than {max_packages} packages")
    items = tuple(_parse_item(entry, n) for n, entry in enumerate(install))
    seen = set()
    for item in items:
        if item.canonical in seen:
            raise ReportRejected("duplicate_package", f"{item.canonical} appears more than once")
        seen.add(item.canonical)
    return ParsedReport(pip_version=pip_version, environment=dict(environment), items=items)


def _parse_item(entry, n: int) -> ReportItem:
    where = f"install[{n}]"
    if not isinstance(entry, dict):
        raise ReportRejected("schema", f"{where} is not an object")
    keys = set(entry)
    if not ITEM_REQUIRED <= keys or keys - ITEM_REQUIRED - ITEM_OPTIONAL:
        raise ReportRejected("schema", f"{where}: unexpected or missing keys")
    if entry["is_direct"] is not False:
        raise ReportRejected("direct_reference", f"{where}: direct URL installs are not allowed")
    if not isinstance(entry["is_yanked"], bool) or not isinstance(entry["requested"], bool):
        raise ReportRejected("schema", f"{where}: is_yanked and requested must be booleans")
    metadata = entry["metadata"]
    if not isinstance(metadata, dict):
        raise ReportRejected("schema", f"{where}: metadata is not an object")
    name = _str(metadata.get("name"), MAX_NAME, f"{where}.metadata.name")
    version_text = _str(metadata.get("version"), MAX_VERSION, f"{where}.metadata.version")
    try:
        version = Version(version_text)
    except InvalidVersion as err:
        raise ReportRejected("invalid_version", f"{where}: {version_text[:50]!r}") from err
    canonical = canonicalize_name(name)
    if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", canonical):
        raise ReportRejected("invalid_name", f"{where}: {name[:50]!r}")
    extras_raw = entry.get("requested_extras", [])
    if not isinstance(extras_raw, list) or len(extras_raw) > MAX_EXTRAS:
        raise ReportRejected("schema", f"{where}: requested_extras must be a short list")
    extras = tuple(sorted({canonicalize_name(_str(e, 100, f"{where}.requested_extras")) for e in extras_raw}))

    info = entry["download_info"]
    if not isinstance(info, dict) or set(info) != DOWNLOAD_KEYS:
        raise ReportRejected("schema", f"{where}: download_info must contain only url and archive_info")
    url = _str(info["url"], MAX_URL, f"{where}.download_info.url")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as err:
        raise ReportRejected("artifact_host", f"{where}: invalid URL") from err
    if (parts.scheme != "https" or parts.hostname != FILES_HOST or parts.username or parts.password
            or port not in (None, 443) or parts.query):
        raise ReportRejected("artifact_host", f"{where}: artifact is not on https://{FILES_HOST}")
    filename = unquote(parts.path.rsplit("/", 1)[-1])
    if not filename.endswith(".whl"):
        raise ReportRejected("not_a_wheel", f"{where}: {filename[:100]}")
    try:
        wheel_name, wheel_version, _build, tags = parse_wheel_filename(filename)
    except (InvalidWheelFilename, InvalidVersion) as err:
        raise ReportRejected("not_a_wheel", f"{where}: invalid wheel filename") from err
    if canonicalize_name(wheel_name) != canonical or wheel_version != version:
        raise ReportRejected("contradictory", f"{where}: wheel filename does not match metadata name/version")

    archive = info["archive_info"]
    if not isinstance(archive, dict) or not set(archive) <= ARCHIVE_KEYS:
        raise ReportRejected("schema", f"{where}: invalid archive_info")
    hashes = archive.get("hashes", {})
    if not isinstance(hashes, dict):
        raise ReportRejected("schema", f"{where}: invalid hashes")
    sha = hashes.get("sha256")
    legacy = archive.get("hash")
    if legacy is not None:
        if not isinstance(legacy, str) or not legacy.startswith("sha256="):
            raise ReportRejected("missing_hash", f"{where}: unsupported hash")
        if sha is not None and legacy[len("sha256="):] != sha:
            raise ReportRejected("contradictory", f"{where}: hash and hashes disagree")
        sha = sha or legacy[len("sha256="):]
    if not isinstance(sha, str) or not _SHA256.match(sha):
        raise ReportRejected("missing_hash", f"{where}: a lowercase SHA-256 is required")
    return ReportItem(
        name=name, canonical=canonical, version=version, filename=filename, url=f"https://{FILES_HOST}{parts.path}",
        sha256=sha, requested=entry["requested"], requested_extras=extras, wheel_tags=frozenset(tags),
    )
