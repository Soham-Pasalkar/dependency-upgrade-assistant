"""The supervisor's own, independent PyPI metadata source (ADR-011, TB-R4).

Only two fixed hosts are ever contacted: ``pypi.org`` (PEP 691 JSON index and the per-version JSON
API) and ``files.pythonhosted.org`` (PEP 658 ``.metadata`` files). URLs are built by this module or
taken from the index response and re-checked; nothing resolver-supplied is fetched. Every request is
HTTPS, refuses redirects, is size-bounded before parsing and gets ``min(8 s, time left in the run)``.
Wheel archives are never downloaded.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from src.services.registry_client import MAX_LARGE_RESPONSE_BYTES, _read_bounded_body

INDEX_HOST = "pypi.org"
FILES_HOST = "files.pythonhosted.org"
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
REQUEST_TIMEOUT_SECONDS = 8.0
MAX_INDEX_BYTES = MAX_LARGE_RESPONSE_BYTES  # 16 MB; numpy measured 2.8 MB on 2026-11-12
MAX_JSON_API_BYTES = MAX_LARGE_RESPONSE_BYTES
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_INDEX_FILES = 50_000
_PROJECT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MetadataFetchError(Exception):
    """The supervisor could not obtain trustworthy metadata. ``code`` is a short reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class DeadlineExceeded(Exception):
    """The run deadline passed. Not a classification: the job records TIMED_OUT (ADR-012)."""


@dataclass
class Deadline:
    at: Optional[float] = None  # time.monotonic() value; None means only the per-request cap applies
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def request_timeout(self, cap: float = REQUEST_TIMEOUT_SECONDS) -> float:
        if self.at is None:
            return cap
        remaining = self.at - self.clock()
        if remaining <= 0:
            raise DeadlineExceeded("the run deadline was reached")
        return min(cap, remaining)


@dataclass(frozen=True)
class IndexFile:
    filename: str
    url: str
    sha256: Optional[str]
    requires_python: Optional[str]
    yanked: bool
    yanked_reason: Optional[str]
    metadata_sha256: Optional[str]
    upload_time: Optional[str]  # raw PEP 700 value, normalized later


@dataclass(frozen=True)
class ProjectIndex:
    name: str
    files: Tuple[IndexFile, ...]
    endpoint: str
    retrieved_at: str

    def entries_named(self, filename: str) -> Tuple[IndexFile, ...]:
        return tuple(f for f in self.files if f.filename == filename)


@dataclass(frozen=True)
class VersionJson:
    data: dict
    endpoint: str
    retrieved_at: str


# (url, headers, timeout, max_bytes) -> (status_code, body)
HttpGet = Callable[[str, Dict[str, str], float, int], Tuple[int, bytes]]

_local = threading.local()


def requests_http_get(url: str, headers: Dict[str, str], timeout: float, max_bytes: int) -> Tuple[int, bytes]:
    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()
        session.headers["User-Agent"] = "dua-resolution-supervisor"
    try:
        resp = session.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=False)
    except requests.Timeout as err:
        raise MetadataFetchError("timeout", f"{url}: {err}") from err
    except requests.RequestException as err:
        raise MetadataFetchError("network", f"{url}: {err}") from err
    if resp.status_code != 200:
        resp.close()
        return resp.status_code, b""
    body, err = _read_bounded_body(resp, max_bytes)
    if body is None:
        code = "too_large" if err and "exceed" in err else "network"
        raise MetadataFetchError(code, f"{url}: {err}")
    return 200, body


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_url(url: str, host: str) -> str:
    """Return the URL without fragment if it is https://<host>/... with no credentials, port or query."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as err:
        raise MetadataFetchError("host_not_allowed", f"invalid URL: {url[:200]}") from err
    if (
        parts.scheme != "https"
        or parts.hostname != host
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.query
        or not parts.path.startswith("/")
    ):
        raise MetadataFetchError("host_not_allowed", f"URL not allowed for {host}: {url[:200]}")
    return f"https://{host}{parts.path}"


def project_slug(name: str) -> str:
    slug = canonicalize_name(name or "")
    if not _PROJECT.match(slug):
        raise MetadataFetchError("invalid_name", f"not a valid project name: {name[:100]!r}")
    return slug


def _json_object(body: bytes, what: str) -> dict:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise MetadataFetchError("malformed", f"{what}: invalid JSON") from err
    if not isinstance(data, dict):
        raise MetadataFetchError("malformed", f"{what}: not a JSON object")
    return data


class PyPIMetadataClient:
    def __init__(self, http_get: HttpGet = requests_http_get, now: Callable[[], str] = utc_now_iso) -> None:
        self._http_get = http_get
        self._now = now
        self._indexes: Dict[str, ProjectIndex] = {}
        self._versions: Dict[Tuple[str, str], VersionJson] = {}

    def _get(self, url: str, host: str, accept: Optional[str], max_bytes: int, deadline: Deadline) -> bytes:
        url = check_url(url, host)
        timeout = deadline.request_timeout()
        status, body = self._http_get(url, {"Accept": accept} if accept else {}, timeout, max_bytes)
        if status == 404:
            raise MetadataFetchError("not_found", url)
        if 300 <= status < 400:
            raise MetadataFetchError("redirect", f"{url} answered {status}; redirects are not followed")
        if status != 200:
            raise MetadataFetchError("http_status", f"{url} answered {status}")
        if len(body) > max_bytes:
            raise MetadataFetchError("too_large", f"{url} exceeded {max_bytes} bytes")
        return body

    def project_index(self, name: str, deadline: Deadline) -> ProjectIndex:
        slug = project_slug(name)
        if slug in self._indexes:
            return self._indexes[slug]
        endpoint = f"https://{INDEX_HOST}/simple/{slug}/"
        body = self._get(endpoint, INDEX_HOST, SIMPLE_JSON, MAX_INDEX_BYTES, deadline)
        index = _parse_index(_json_object(body, endpoint), slug, endpoint, self._now())
        self._indexes[slug] = index
        return index

    def metadata_file(self, file_url: str, expected_sha256: str, deadline: Deadline) -> bytes:
        url = check_url(file_url, FILES_HOST) + ".metadata"
        body = self._get(url, FILES_HOST, None, MAX_METADATA_BYTES, deadline)
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise MetadataFetchError("hash_mismatch", f"{url}: metadata does not match the index-advertised hash")
        return body

    def version_json(self, name: str, version: str, deadline: Deadline) -> VersionJson:
        slug = project_slug(name)
        try:
            ver = str(Version(version))
        except InvalidVersion as err:
            raise MetadataFetchError("invalid_version", f"{version[:50]!r}") from err
        key = (slug, ver)
        if key in self._versions:
            return self._versions[key]
        endpoint = f"https://{INDEX_HOST}/pypi/{slug}/{ver}/json"
        data = _json_object(self._get(endpoint, INDEX_HOST, "application/json", MAX_JSON_API_BYTES, deadline), endpoint)
        if not isinstance(data.get("info"), dict) or not isinstance(data.get("urls"), list):
            raise MetadataFetchError("malformed", f"{endpoint}: missing info or urls")
        result = VersionJson(data=data, endpoint=endpoint, retrieved_at=self._now())
        self._versions[key] = result
        return result


def _parse_index(data: dict, slug: str, endpoint: str, retrieved_at: str) -> ProjectIndex:
    meta = data.get("meta")
    if not isinstance(meta, dict) or not str(meta.get("api-version", "")).startswith("1."):
        raise MetadataFetchError("malformed", f"{endpoint}: unsupported or missing api-version")
    if not isinstance(data.get("name"), str) or canonicalize_name(data["name"]) != slug:
        raise MetadataFetchError("malformed", f"{endpoint}: project name does not match")
    raw_files = data.get("files")
    if not isinstance(raw_files, list) or len(raw_files) > MAX_INDEX_FILES:
        raise MetadataFetchError("malformed", f"{endpoint}: files missing or too many")
    files = []
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise MetadataFetchError("malformed", f"{endpoint}: file entry is not an object")
        filename, url, hashes = raw.get("filename"), raw.get("url"), raw.get("hashes")
        if not isinstance(filename, str) or not filename or "/" in filename or len(filename) > 255:
            raise MetadataFetchError("malformed", f"{endpoint}: invalid filename")
        if not isinstance(url, str) or not isinstance(hashes, dict):
            raise MetadataFetchError("malformed", f"{endpoint}: {filename}: missing url or hashes")
        absolute = check_url(urljoin(endpoint, url), FILES_HOST)
        sha = hashes.get("sha256")
        sha = sha.lower() if isinstance(sha, str) and _SHA256.match(sha.lower()) else None
        yanked = raw.get("yanked", False)
        if not isinstance(yanked, (bool, str)):
            raise MetadataFetchError("malformed", f"{endpoint}: {filename}: invalid yanked value")
        rp = raw.get("requires-python")
        meta_hash = raw.get("core-metadata", raw.get("data-dist-info-metadata"))
        meta_sha = None
        if isinstance(meta_hash, dict) and isinstance(meta_hash.get("sha256"), str):
            candidate = meta_hash["sha256"].lower()
            meta_sha = candidate if _SHA256.match(candidate) else None
        upload = raw.get("upload-time")
        files.append(
            IndexFile(
                filename=filename,
                url=absolute,
                sha256=sha,
                requires_python=rp if isinstance(rp, str) and rp.strip() else None,
                yanked=bool(yanked),
                yanked_reason=yanked if isinstance(yanked, str) and yanked else None,
                metadata_sha256=meta_sha,
                upload_time=upload if isinstance(upload, str) else None,
            )
        )
    return ProjectIndex(name=slug, files=tuple(files), endpoint=endpoint, retrieved_at=retrieved_at)
