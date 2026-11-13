"""Package registry clients for PyPI and npm with caching, bounded responses, and safety controls.

Enforces:
- Fixed official endpoints with strict redirect destination validation (anti-SSRF).
- Bounded streaming response limits (MAX_RESPONSE_BYTES enforced before JSON decoding).
- Thread-safe in-memory TTL caching.
- Thread-local requests.Session management for safe concurrent lookups.
- Categorized status reporting (SUCCESS, NOT_FOUND, RATE_LIMITED, NETWORK_ERROR).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

from src.data.catalog_loader import load_catalog, normalize_package_name
from src.models.manifest import RegistryLookupStatus

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_CACHE_TTL_SECONDS = 300.0  # 5 minutes
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB limit
# Bounded fallback for very large projects (numpy, boto3): PEP 691 Simple JSON / npm abbreviated metadata.
MAX_LARGE_RESPONSE_BYTES = 16 * 1024 * 1024
PEP691_ACCEPT = "application/vnd.pypi.simple.v1+json"
NPM_ABBREVIATED_ACCEPT = "application/vnd.npm.install-v1+json"
TRANSIENT_RETRIES = 2
RETRY_BACKOFF_SECONDS = 0.3
TRANSIENT_STATUS_CODES = {500, 502, 503, 504}


@dataclass
class RegistryPackageMetadata:
    """Standardized metadata returned from package registries."""
    package_name: str
    latest_version: Optional[str]
    available_versions: List[str]
    status: RegistryLookupStatus
    is_deprecated_or_yanked: bool = False
    deprecation_message: Optional[str] = None
    error_message: Optional[str] = None
    # LIVE_REGISTRY, CACHED_REGISTRY, OFFLINE_CATALOG or UNAVAILABLE (see RegistryDataSource)
    source: str = "LIVE_REGISTRY"


@dataclass
class _CacheEntry:
    timestamp: float
    data: RegistryPackageMetadata


class ThreadLocalSessionFactory:
    """Thread-local session manager that provisions an isolated requests.Session per thread.
    
    Preserves connection reuse within each worker thread while preventing unsafe concurrent
    mutations of shared session state across threads.
    """

    def __init__(
        self,
        session_factory: Optional[Callable[[], requests.Session]] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._local = threading.local()
        self._factory = session_factory or requests.Session
        self._injected_session = session

    def get_session(self) -> requests.Session:
        if self._injected_session is not None:
            return self._injected_session
        if not hasattr(self._local, "session"):
            self._local.session = self._factory()
        return self._local.session


def _read_bounded_body(resp: requests.Response, max_bytes: int) -> Tuple[Optional[bytes], Optional[str]]:
    """Stream and read response body strictly within max_bytes.
    
    Returns:
        (body_bytes, None) on success, or (None, error_message) on failure/exceeded size.
    Always closes the response on error or stream completion.
    Never falls back to unmetered .content or .json() access.
    """
    try:
        # Pre-check Content-Length header if provided
        cl_header = resp.headers.get("Content-Length") if hasattr(resp, "headers") and resp.headers else None
        if cl_header is not None:
            try:
                cl_val = int(str(cl_header).strip())
                if cl_val > max_bytes:
                    if hasattr(resp, "close"):
                        resp.close()
                    return None, f"Response Content-Length ({cl_val} bytes) exceeds maximum limit of {max_bytes} bytes"
            except (ValueError, TypeError):
                # Malformed or misleading Content-Length header is ignored; stream reading will enforce limit
                pass

        chunks: List[bytes] = []
        total = 0

        if not hasattr(resp, "iter_content"):
            if hasattr(resp, "close"):
                resp.close()
            return None, "Response object does not support streamed iter_content reading"

        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                if hasattr(resp, "close"):
                    resp.close()
                return None, f"Response payload exceeded maximum allowed size of {max_bytes} bytes"
            chunks.append(chunk)

        body = b"".join(chunks)
        return body, None

    except Exception as err:
        # Any stream reading failure (connection closed, mid-stream disconnect, iterator exception)
        # must abort and return an error; never treat partial chunks as a valid complete response
        if hasattr(resp, "close"):
            resp.close()
        return None, f"Streaming read failure: {err}"
    finally:
        if hasattr(resp, "close"):
            resp.close()


def _safe_fetch_json(
    session: requests.Session,
    url: str,
    timeout: float,
    max_bytes: int,
    allowed_hosts: Set[str],
    max_redirects: int = 3,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
    """Fetch JSON with bounded streaming, strict redirect validation, and size capping.
    
    Returns:
        (status_code, parsed_json_or_None, error_message_or_None)
    """
    current_url = url
    for _ in range(max_redirects + 1):
        parsed = urllib.parse.urlparse(current_url)
        if parsed.scheme != "https" or parsed.netloc.lower() not in allowed_hosts:
            return 400, None, f"Untrusted registry URL or redirect target: '{current_url}' (SSRF protection)"

        try:
            if headers:
                resp = session.get(current_url, timeout=timeout, stream=True, allow_redirects=False, headers=headers)
            else:
                resp = session.get(current_url, timeout=timeout, stream=True, allow_redirects=False)
        except requests.Timeout as err:
            return 504, None, f"Registry request timed out: {err}"
        except Exception as err:
            return 500, None, f"Network error connecting to registry: {err}"

        # Handle redirects
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location") if hasattr(resp, "headers") and resp.headers else None
            if hasattr(resp, "close"):
                resp.close()
            if not location:
                return resp.status_code, None, "Redirect response missing Location header"
            current_url = urllib.parse.urljoin(current_url, location)
            continue

        # If non-200 status code (404, 429, 500, etc.)
        if resp.status_code != 200:
            status_code = resp.status_code
            if hasattr(resp, "close"):
                resp.close()
            return status_code, None, None

        # Status 200: Stream bounded body strictly without fallback bypass
        body, err = _read_bounded_body(resp, max_bytes)
        if err is not None:
            if "exceed" in err.lower():
                return 413, None, err
            return 502, None, err

        if body is None or len(body) == 0:
            return 502, None, "Empty response body received from registry"

        try:
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict):
                return 502, None, "Registry response must be a JSON object"
            return 200, data, None
        except Exception as jerr:
            return 502, None, f"Invalid JSON received from registry: {jerr}"

    return 400, None, "Too many redirects"


def _from_cache(meta: RegistryPackageMetadata) -> RegistryPackageMetadata:
    """A cache hit keeps its origin explicit: live answers become CACHED_REGISTRY."""
    if meta.source == "LIVE_REGISTRY":
        return replace(meta, source="CACHED_REGISTRY")
    return meta


def _fetch_json_with_retry(
    session: requests.Session,
    url: str,
    timeout: float,
    max_bytes: int,
    allowed_hosts: Set[str],
    headers: Optional[Dict[str, str]] = None,
    retries: int = TRANSIENT_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
    """Bounded retry with exponential backoff for transient network and 5xx failures only."""
    attempt = 0
    while True:
        status_code, data, err = _safe_fetch_json(
            session=session, url=url, timeout=timeout, max_bytes=max_bytes,
            allowed_hosts=allowed_hosts, headers=headers,
        )
        transient = status_code in TRANSIENT_STATUS_CODES and data is None
        if not transient or attempt >= retries:
            return status_code, data, err
        sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt))
        attempt += 1


def _parse_pep691_project(data: Dict[str, Any]) -> Tuple[Optional[str], List[str], bool, Optional[str]]:
    """Derive (latest, versions, latest_yanked, yanked_reason) from a PEP 691 project response."""
    from packaging.version import InvalidVersion, Version

    versions: List[str] = [str(v) for v in (data.get("versions") or [])]
    files = data.get("files") or []
    files_by_version: Dict[str, List[Dict[str, Any]]] = {}
    for f in files:
        if not isinstance(f, dict):
            continue
        name = str(f.get("filename") or "")
        for v in versions:
            if f"-{v}-" in name or f"-{v}." in name or name.endswith(f"-{v}.tar.gz") or name.endswith(f"-{v}.zip"):
                files_by_version.setdefault(v, []).append(f)
                break

    def fully_yanked(v: str) -> Tuple[bool, Optional[str]]:
        vf = files_by_version.get(v) or []
        if not vf:
            return False, None
        if all(bool(x.get("yanked")) for x in vf):
            reason = next((x.get("yanked") for x in vf if isinstance(x.get("yanked"), str)), None)
            return True, reason or "Yanked on PyPI"
        return False, None

    parsed: List[Tuple[Version, str]] = []
    for v in versions:
        try:
            parsed.append((Version(v), v))
        except InvalidVersion:
            continue
    parsed.sort()
    latest: Optional[str] = None
    for ver, raw in reversed(parsed):
        if ver.is_prerelease or ver.is_devrelease:
            continue
        if fully_yanked(raw)[0]:
            continue
        latest = raw
        break
    if latest is None and parsed:
        latest = parsed[-1][1]
    yanked, reason = fully_yanked(latest) if latest else (False, None)
    return latest, versions, yanked, reason


class PyPIRegistryClient:
    """Client for official PyPI JSON API with bounded responses and thread-safe caching."""

    OFFICIAL_BASE_URL = "https://pypi.org/pypi"
    ALLOWED_HOSTS = {"pypi.org"}

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        session_factory: Optional[Callable[[], requests.Session]] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._session_manager = ThreadLocalSessionFactory(session_factory=session_factory, session=session)
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_response_bytes = max_response_bytes
        self._cache: Dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self._local_catalog = load_catalog()

    @property
    def _session(self) -> requests.Session:
        return self._session_manager.get_session()

    def get_package_metadata(self, package_name: str) -> RegistryPackageMetadata:
        """Fetch package metadata from PyPI, using cache when valid."""
        norm_name = normalize_package_name(package_name)

        now = time.time()
        with self._lock:
            if norm_name in self._cache:
                entry = self._cache[norm_name]
                if now - entry.timestamp < self.cache_ttl:
                    return _from_cache(entry.data)

        safe_name = urllib.parse.quote(norm_name)
        url = f"{self.OFFICIAL_BASE_URL}/{safe_name}/json"

        meta = self._fetch_from_pypi(norm_name, url)
        with self._lock:
            self._cache[norm_name] = _CacheEntry(timestamp=now, data=meta)
        return meta

    def _fetch_from_pypi(self, package_name: str, url: str) -> RegistryPackageMetadata:
        try:
            status_code, data, err_msg = _fetch_json_with_retry(
                session=self._session,
                url=url,
                timeout=self.timeout,
                max_bytes=self.max_response_bytes,
                allowed_hosts=self.ALLOWED_HOSTS,
            )

            if status_code == 413:
                # Very large projects exceed the JSON API cap: use the leaner PEP 691 Simple API.
                simple_url = f"https://pypi.org/simple/{urllib.parse.quote(package_name)}/"
                s_status, s_data, s_err = _fetch_json_with_retry(
                    session=self._session,
                    url=simple_url,
                    timeout=self.timeout,
                    max_bytes=max(self.max_response_bytes, MAX_LARGE_RESPONSE_BYTES),
                    allowed_hosts=self.ALLOWED_HOSTS,
                    headers={"Accept": PEP691_ACCEPT},
                )
                if s_status == 200 and s_data is not None and "versions" in s_data:
                    latest_ver, versions, yanked, reason = _parse_pep691_project(s_data)
                    return RegistryPackageMetadata(
                        package_name=package_name,
                        latest_version=latest_ver,
                        available_versions=versions,
                        status=RegistryLookupStatus.SUCCESS,
                        is_deprecated_or_yanked=yanked,
                        deprecation_message=reason,
                    )
                if s_status == 404:
                    status_code, err_msg = 404, None
                else:
                    return RegistryPackageMetadata(
                        package_name=package_name,
                        latest_version=None,
                        available_versions=[],
                        status=RegistryLookupStatus.TOO_LARGE,
                        source="UNAVAILABLE",
                        error_message=err_msg or s_err or "PyPI metadata is too large to process",
                    )

            if status_code == 200 and data is not None:
                info = data.get("info", {})
                latest_ver = info.get("version")
                releases = data.get("releases", {})
                versions = list(releases.keys())

                yanked = False
                yanked_reason = None
                latest_files = releases.get(latest_ver, [])
                for f in latest_files:
                    if isinstance(f, dict) and f.get("yanked"):
                        yanked = True
                        yanked_reason = f.get("yanked_reason") or "Yanked on PyPI"
                        break

                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=latest_ver,
                    available_versions=versions,
                    status=RegistryLookupStatus.SUCCESS,
                    is_deprecated_or_yanked=yanked,
                    deprecation_message=yanked_reason,
                )
            elif status_code == 404:
                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=None,
                    available_versions=[],
                    status=RegistryLookupStatus.NOT_FOUND,
                    error_message=f"Package '{package_name}' not found on PyPI",
                )
            elif status_code == 429:
                return self._fallback_local(
                    package_name,
                    RegistryLookupStatus.RATE_LIMITED,
                    "PyPI rate limit exceeded (HTTP 429)",
                )
            elif status_code == 504:
                return self._fallback_local(
                    package_name,
                    RegistryLookupStatus.TIMEOUT,
                    err_msg or "PyPI did not respond in time",
                )
            else:
                return self._fallback_local(
                    package_name,
                    RegistryLookupStatus.NETWORK_ERROR,
                    err_msg or f"PyPI returned HTTP {status_code}",
                )

        except (requests.Timeout, requests.ConnectionError, Exception) as err:
            logger.debug(f"Network error accessing PyPI for {package_name}: {err}")
            return self._fallback_local(
                package_name,
                RegistryLookupStatus.NETWORK_ERROR,
                f"Network error querying PyPI: {err}",
            )

    def _fallback_local(
        self,
        package_name: str,
        failure_status: RegistryLookupStatus,
        error_msg: str,
    ) -> RegistryPackageMetadata:
        """Fallback to local catalog if offline or PyPI is unreachable."""
        entry = self._local_catalog.get_package(package_name)
        if entry:
            # Explicitly offline: never reported as a live registry answer (F10).
            return RegistryPackageMetadata(
                package_name=package_name,
                latest_version=entry.candidate_version,
                available_versions=entry.known_versions,
                status=RegistryLookupStatus.OFFLINE_CATALOG,
                deprecation_message=None,
                error_message=f"PyPI unavailable ({failure_status.value}); using the bundled offline catalog: {error_msg}",
                source="OFFLINE_CATALOG",
            )
        return RegistryPackageMetadata(
            package_name=package_name,
            latest_version=None,
            available_versions=[],
            status=failure_status,
            error_message=error_msg,
            source="UNAVAILABLE",
        )


class NpmRegistryClient:
    """Client for official npm registry API with bounded responses and thread-safe caching."""

    OFFICIAL_BASE_URL = "https://registry.npmjs.org"
    ALLOWED_HOSTS = {"registry.npmjs.org"}

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        session_factory: Optional[Callable[[], requests.Session]] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._session_manager = ThreadLocalSessionFactory(session_factory=session_factory, session=session)
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_response_bytes = max_response_bytes
        self._cache: Dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    @property
    def _session(self) -> requests.Session:
        return self._session_manager.get_session()

    def get_package_metadata(self, package_name: str) -> RegistryPackageMetadata:
        """Fetch package metadata from npm registry, using cache when valid."""
        clean_name = package_name.strip().lower()

        now = time.time()
        with self._lock:
            if clean_name in self._cache:
                entry = self._cache[clean_name]
                if now - entry.timestamp < self.cache_ttl:
                    return _from_cache(entry.data)

        # Handle scoped packages (e.g. @types/node -> @types%2Fnode)
        if clean_name.startswith("@") and "/" in clean_name:
            scope, pkg = clean_name.split("/", 1)
            safe_name = f"@{urllib.parse.quote(scope.lstrip('@'))}%2F{urllib.parse.quote(pkg)}"
        else:
            safe_name = urllib.parse.quote(clean_name)

        url = f"{self.OFFICIAL_BASE_URL}/{safe_name}"

        meta = self._fetch_from_npm(clean_name, url)
        with self._lock:
            self._cache[clean_name] = _CacheEntry(timestamp=now, data=meta)
        return meta

    def _fetch_from_npm(self, package_name: str, url: str) -> RegistryPackageMetadata:
        try:
            status_code, data, err_msg = _fetch_json_with_retry(
                session=self._session,
                url=url,
                timeout=self.timeout,
                max_bytes=self.max_response_bytes,
                allowed_hosts=self.ALLOWED_HOSTS,
            )
            if status_code == 413:
                # Full packuments of large packages exceed the cap: request abbreviated install metadata.
                status_code, data, err_msg = _fetch_json_with_retry(
                    session=self._session,
                    url=url,
                    timeout=self.timeout,
                    max_bytes=max(self.max_response_bytes, MAX_LARGE_RESPONSE_BYTES),
                    allowed_hosts=self.ALLOWED_HOSTS,
                    headers={"Accept": NPM_ABBREVIATED_ACCEPT},
                )

            if status_code == 200 and data is not None:
                dist_tags = data.get("dist-tags", {})
                latest_ver = dist_tags.get("latest")
                versions_dict = data.get("versions", {})
                versions = list(versions_dict.keys())

                is_deprecated = False
                dep_msg = None
                if "deprecated" in data:
                    is_deprecated = True
                    dep_msg = str(data["deprecated"])
                elif latest_ver and latest_ver in versions_dict:
                    latest_info = versions_dict[latest_ver]
                    if isinstance(latest_info, dict) and "deprecated" in latest_info:
                        is_deprecated = True
                        dep_msg = str(latest_info["deprecated"])

                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=latest_ver,
                    available_versions=versions,
                    status=RegistryLookupStatus.SUCCESS,
                    is_deprecated_or_yanked=is_deprecated,
                    deprecation_message=dep_msg,
                )
            elif status_code == 404:
                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=None,
                    available_versions=[],
                    status=RegistryLookupStatus.NOT_FOUND,
                    error_message=f"Package '{package_name}' not found on npm registry",
                )
            elif status_code == 413:
                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=None,
                    available_versions=[],
                    status=RegistryLookupStatus.TOO_LARGE,
                    source="UNAVAILABLE",
                    error_message=err_msg or "npm metadata is too large to process",
                )
            elif status_code == 504:
                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=None,
                    available_versions=[],
                    status=RegistryLookupStatus.TIMEOUT,
                    source="UNAVAILABLE",
                    error_message=err_msg or "npm registry did not respond in time",
                )
            elif status_code == 429:
                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=None,
                    available_versions=[],
                    status=RegistryLookupStatus.RATE_LIMITED,
                    source="UNAVAILABLE",
                    error_message="npm registry rate limit exceeded (HTTP 429)",
                )
            else:
                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=None,
                    available_versions=[],
                    status=RegistryLookupStatus.NETWORK_ERROR,
                    source="UNAVAILABLE",
                    error_message=err_msg or f"npm registry returned HTTP {status_code}",
                )

        except (requests.Timeout, requests.ConnectionError, Exception) as err:
            logger.debug(f"Network error querying npm registry for {package_name}: {err}")
            return RegistryPackageMetadata(
                package_name=package_name,
                latest_version=None,
                available_versions=[],
                status=RegistryLookupStatus.NETWORK_ERROR,
                source="UNAVAILABLE",
                error_message=f"Network error querying npm registry: {err}",
            )
