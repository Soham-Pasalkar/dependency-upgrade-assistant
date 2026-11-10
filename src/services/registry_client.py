"""Package registry clients for PyPI and npm with caching and safety controls.

Enforces:
- Fixed official endpoints (no arbitrary user URLs; prevents SSRF).
- In-memory TTL caching to avoid duplicate requests.
- Bounded response parsing and strict timeouts.
- Categorized status reporting (SUCCESS, NOT_FOUND, RATE_LIMITED, NETWORK_ERROR).
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.data.catalog_loader import load_catalog, normalize_package_name
from src.models.manifest import RegistryLookupStatus

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_CACHE_TTL_SECONDS = 300.0  # 5 minutes
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB limit


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


@dataclass
class _CacheEntry:
    timestamp: float
    data: RegistryPackageMetadata


class PyPIRegistryClient:
    """Client for official PyPI JSON API with in-memory TTL caching."""

    OFFICIAL_BASE_URL = "https://pypi.org/pypi"

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._session = session or requests.Session()
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, _CacheEntry] = {}
        self._local_catalog = load_catalog()

    def get_package_metadata(self, package_name: str) -> RegistryPackageMetadata:
        """Fetch package metadata from PyPI, using cache when valid."""
        norm_name = normalize_package_name(package_name)

        # Check cache
        now = time.time()
        if norm_name in self._cache:
            entry = self._cache[norm_name]
            if now - entry.timestamp < self.cache_ttl:
                return entry.data

        # Construct official URL strictly using official base
        safe_name = urllib.parse.quote(norm_name)
        url = f"{self.OFFICIAL_BASE_URL}/{safe_name}/json"

        meta = self._fetch_from_pypi(norm_name, url)
        self._cache[norm_name] = _CacheEntry(timestamp=now, data=meta)
        return meta

    def _fetch_from_pypi(self, package_name: str, url: str) -> RegistryPackageMetadata:
        try:
            resp = self._session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                info = data.get("info", {})
                latest_ver = info.get("version")
                releases = data.get("releases", {})
                versions = list(releases.keys())

                # Check if latest release is yanked
                yanked = False
                yanked_reason = None
                latest_files = releases.get(latest_ver, [])
                for f in latest_files:
                    if f.get("yanked"):
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
            elif resp.status_code == 404:
                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=None,
                    available_versions=[],
                    status=RegistryLookupStatus.NOT_FOUND,
                    error_message=f"Package '{package_name}' not found on PyPI",
                )
            elif resp.status_code == 429:
                return self._fallback_local(
                    package_name,
                    RegistryLookupStatus.RATE_LIMITED,
                    "PyPI rate limit exceeded (HTTP 429)",
                )
            else:
                return self._fallback_local(
                    package_name,
                    RegistryLookupStatus.NETWORK_ERROR,
                    f"PyPI returned HTTP {resp.status_code}",
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
            return RegistryPackageMetadata(
                package_name=package_name,
                latest_version=entry.candidate_version,
                available_versions=entry.known_versions,
                status=RegistryLookupStatus.SUCCESS,
                deprecation_message=f"Serving from local catalog cache ({error_msg})",
            )
        return RegistryPackageMetadata(
            package_name=package_name,
            latest_version=None,
            available_versions=[],
            status=failure_status,
            error_message=error_msg,
        )


class NpmRegistryClient:
    """Client for official npm registry API with in-memory TTL caching."""

    OFFICIAL_BASE_URL = "https://registry.npmjs.org"

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._session = session or requests.Session()
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, _CacheEntry] = {}

    def get_package_metadata(self, package_name: str) -> RegistryPackageMetadata:
        """Fetch package metadata from npm registry, using cache when valid."""
        clean_name = package_name.strip().lower()

        now = time.time()
        if clean_name in self._cache:
            entry = self._cache[clean_name]
            if now - entry.timestamp < self.cache_ttl:
                return entry.data

        # Handle scoped packages (e.g. @types/node -> @types%2Fnode)
        if clean_name.startswith("@") and "/" in clean_name:
            scope, pkg = clean_name.split("/", 1)
            safe_name = f"@{urllib.parse.quote(scope.lstrip('@'))}%2F{urllib.parse.quote(pkg)}"
        else:
            safe_name = urllib.parse.quote(clean_name)

        url = f"{self.OFFICIAL_BASE_URL}/{safe_name}"

        meta = self._fetch_from_npm(clean_name, url)
        self._cache[clean_name] = _CacheEntry(timestamp=now, data=meta)
        return meta

    def _fetch_from_npm(self, package_name: str, url: str) -> RegistryPackageMetadata:
        try:
            resp = self._session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                dist_tags = data.get("dist-tags", {})
                latest_ver = dist_tags.get("latest")
                versions_dict = data.get("versions", {})
                versions = list(versions_dict.keys())

                # Check deprecation
                is_deprecated = False
                dep_msg = None
                if "deprecated" in data:
                    is_deprecated = True
                    dep_msg = str(data["deprecated"])
                elif latest_ver and latest_ver in versions_dict:
                    latest_info = versions_dict[latest_ver]
                    if "deprecated" in latest_info:
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
            elif resp.status_code == 404:
                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=None,
                    available_versions=[],
                    status=RegistryLookupStatus.NOT_FOUND,
                    error_message=f"Package '{package_name}' not found on npm registry",
                )
            elif resp.status_code == 429:
                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=None,
                    available_versions=[],
                    status=RegistryLookupStatus.RATE_LIMITED,
                    error_message="npm registry rate limit exceeded (HTTP 429)",
                )
            else:
                return RegistryPackageMetadata(
                    package_name=package_name,
                    latest_version=None,
                    available_versions=[],
                    status=RegistryLookupStatus.NETWORK_ERROR,
                    error_message=f"npm registry returned HTTP {resp.status_code}",
                )

        except (requests.Timeout, requests.ConnectionError, Exception) as err:
            logger.debug(f"Network error querying npm registry for {package_name}: {err}")
            return RegistryPackageMetadata(
                package_name=package_name,
                latest_version=None,
                available_versions=[],
                status=RegistryLookupStatus.NETWORK_ERROR,
                error_message=f"Network error querying npm registry: {err}",
            )
