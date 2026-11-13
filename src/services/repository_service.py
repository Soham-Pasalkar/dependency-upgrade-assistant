"""Hardened acquisition of public GitHub repositories and recursive manifest discovery.

Security properties (threat model T-15..T-18, ADR-006):
- Only ``https://github.com/<owner>/<repo>`` is accepted; the canonical URL is rebuilt server-side.
- ``github.com`` and ``api.github.com`` must resolve to public (global) addresses only.
- Repository metadata is checked through the GitHub REST API before cloning (exists, public, size).
  Redirects are only followed when they stay on ``api.github.com``.
- git runs with an empty environment, no system/global config, https-only transport, no hooks,
  no submodules, no LFS smudging, and ``core.symlinks=false``.
- Resource use is bounded *during* the clone: ``RLIMIT_FSIZE`` caps every file git writes, a
  watchdog kills the process group as soon as the workspace exceeds its byte or file quota, and a
  wall-clock timeout applies. Without a size-limited filesystem the byte quota can be overshot by at
  most one polling interval of transfer (never more than the per-file cap); this is reported honestly.
- Discovery never follows symlinks, prunes vendored/build directories and caps depth, count and size.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import resource
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

from src.analysis.manifest_scanner import is_requirements_filename

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits (NFR-9)
# ---------------------------------------------------------------------------
MAX_REPO_SIZE_KB = 200 * 1024            # GitHub-reported repository size
MAX_WORKSPACE_BYTES = 500 * 1024 * 1024  # on-disk quota while cloning
MAX_FILE_BYTES = 50 * 1024 * 1024        # RLIMIT_FSIZE for the git process
MAX_FILES = 20_000
CLONE_TIMEOUT_SECONDS = 90
WATCHDOG_INTERVAL_SECONDS = 0.25
MAX_CONCURRENT_CLONES = 2

MAX_DISCOVERY_DEPTH = 12
MAX_MANIFESTS = 100
MAX_MANIFEST_BYTES = 512 * 1024

GITHUB_HOST = "github.com"
GITHUB_API_HOST = "api.github.com"
GITHUB_API_TIMEOUT = 10

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

DISCOVERY_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".github", "node_modules", "bower_components", "vendor",
    ".venv", "venv", "env", ".env", "virtualenv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", "dist", "build", ".next", ".nuxt",
    "coverage", ".cache", "site-packages", ".idea", ".vscode",
}

_clone_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CLONES)


class RepositoryError(Exception):
    """A user-facing acquisition failure with a stable machine code."""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class RepositoryRef:
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def clone_url(self) -> str:
        return f"https://{GITHUB_HOST}/{self.owner}/{self.repo}.git"

    @property
    def html_url(self) -> str:
        return f"https://{GITHUB_HOST}/{self.owner}/{self.repo}"


@dataclass
class RepositoryMetadata:
    full_name: str
    default_branch: str
    size_kb: int
    description: Optional[str] = None


@dataclass
class DiscoveredFile:
    relative_path: str
    kind: str  # "requirements" | "pyproject" | "package_json"
    size_bytes: int


@dataclass
class DiscoveryResult:
    manifests: List[DiscoveredFile] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    files_scanned: int = 0
    truncated: bool = False


@dataclass
class AcquiredRepository:
    ref: RepositoryRef
    metadata: RepositoryMetadata
    workspace: Path
    commit_sha: Optional[str]
    bytes_on_disk: int
    file_count: int


# ---------------------------------------------------------------------------
# URL parsing (T-15)
# ---------------------------------------------------------------------------
def parse_repository_url(raw: str) -> RepositoryRef:
    """Parse and strictly validate a public GitHub repository URL."""
    if not isinstance(raw, str) or not raw.strip():
        raise RepositoryError("invalid_url", "Enter a GitHub repository URL.")
    value = raw.strip()
    if len(value) > 300 or any(c.isspace() or ord(c) < 32 for c in value):
        raise RepositoryError("invalid_url", "That does not look like a GitHub repository URL.")
    if "%" in value or "\\" in value:
        raise RepositoryError("invalid_url", "Encoded characters are not allowed in the repository URL.")

    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https":
        raise RepositoryError("invalid_url", "Use an https://github.com/ URL.")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise RepositoryError("invalid_url", "Credentials are not allowed in the repository URL.")
    if parsed.port is not None or parsed.netloc.lower() != GITHUB_HOST:
        raise RepositoryError("unsupported_host", "Only public repositories on github.com are supported.")
    if parsed.query or parsed.fragment:
        raise RepositoryError("invalid_url", "Remove the query string or fragment from the URL.")

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 2:
        raise RepositoryError("invalid_url", "Use the repository root URL, for example https://github.com/owner/name.")
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not OWNER_RE.match(owner) or not REPO_RE.match(repo) or repo in (".", ".."):
        raise RepositoryError("invalid_url", "The owner or repository name is not valid.")
    return RepositoryRef(owner=owner, repo=repo)


def _assert_public_resolution(host: str, resolver: Callable = socket.getaddrinfo) -> None:
    """Refuse to connect when a fixed host resolves to a non-public address (T-15)."""
    try:
        infos = resolver(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise RepositoryError("network", f"Could not resolve {host}. Check your network connection.", 502) from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise RepositoryError("network", f"Could not resolve {host}.", 502)
    for addr in addresses:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if not ip.is_global:
            raise RepositoryError(
                "unsafe_destination",
                f"{host} resolved to a non-public address; the request was blocked.",
                502,
            )


# ---------------------------------------------------------------------------
# GitHub metadata pre-check
# ---------------------------------------------------------------------------
def fetch_repository_metadata(
    ref: RepositoryRef,
    session: Optional[requests.Session] = None,
    resolver: Callable = socket.getaddrinfo,
) -> RepositoryMetadata:
    _assert_public_resolution(GITHUB_API_HOST, resolver)
    http = session or requests.Session()
    url = f"https://{GITHUB_API_HOST}/repos/{ref.owner}/{ref.repo}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "dependency-upgrade-assistant",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    for _hop in range(2):
        try:
            resp = http.get(url, headers=headers, timeout=GITHUB_API_TIMEOUT, allow_redirects=False)
        except requests.Timeout as exc:
            raise RepositoryError("timeout", "GitHub did not respond in time. Try again.", 504) from exc
        except requests.RequestException as exc:
            raise RepositoryError("network", "Could not reach GitHub. Check your network connection.", 502) from exc

        if resp.status_code in (301, 302, 307, 308):
            location = resp.headers.get("Location", "")
            target = urllib.parse.urlsplit(location)
            if target.scheme != "https" or target.netloc.lower() != GITHUB_API_HOST:
                raise RepositoryError("redirect_blocked", "GitHub redirected to an unexpected location.", 502)
            url = location
            continue
        break
    else:
        raise RepositoryError("redirect_blocked", "Too many redirects from GitHub.", 502)

    if resp.status_code == 404:
        raise RepositoryError(
            "not_found",
            f"{ref.full_name} was not found. Check the URL; private repositories are not supported.",
            404,
        )
    if resp.status_code in (403, 429):
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        if remaining == "0" or resp.status_code == 429:
            wait = ""
            if reset and reset.isdigit():
                minutes = max(1, int((int(reset) - time.time()) // 60) + 1)
                wait = f" Try again in about {minutes} minute{'s' if minutes != 1 else ''}."
            raise RepositoryError("rate_limited", f"GitHub's API rate limit was reached.{wait}", 429)
        raise RepositoryError("forbidden", "GitHub refused access to this repository.", 403)
    if resp.status_code != 200:
        raise RepositoryError("github_error", f"GitHub returned an unexpected response ({resp.status_code}).", 502)

    try:
        data = resp.json()
    except ValueError as exc:
        raise RepositoryError("github_error", "GitHub returned an unreadable response.", 502) from exc

    if data.get("private") or data.get("visibility") not in (None, "public"):
        raise RepositoryError("private", "Only public repositories are supported.", 403)
    size_kb = int(data.get("size") or 0)
    if size_kb > MAX_REPO_SIZE_KB:
        raise RepositoryError(
            "too_large",
            f"{ref.full_name} is about {size_kb // 1024} MB; the limit is {MAX_REPO_SIZE_KB // 1024} MB.",
            413,
        )
    return RepositoryMetadata(
        full_name=str(data.get("full_name") or ref.full_name),
        default_branch=str(data.get("default_branch") or "main"),
        size_kb=size_kb,
        description=data.get("description"),
    )


# ---------------------------------------------------------------------------
# Hardened clone (T-16, T-18)
# ---------------------------------------------------------------------------
GIT_HARDENING_FLAGS: Tuple[str, ...] = (
    "-c", "core.symlinks=false",
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "submodule.recurse=false",
    "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.process=",
    "-c", "filter.lfs.required=false",
    "-c", "http.followRedirects=false",
    "-c", "credential.helper=",
)


def build_clone_command(ref: RepositoryRef, dest: Path) -> List[str]:
    return [
        "git", *GIT_HARDENING_FLAGS,
        "clone", "--quiet", "--depth", "1", "--single-branch", "--no-tags", "--no-recurse-submodules",
        "--", ref.clone_url, str(dest),
    ]


def build_git_env(home: Path) -> Dict[str, str]:
    """A minimal environment: nothing from the server process leaks into git."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/echo",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_ALLOW_PROTOCOL": "https",
        "LC_ALL": "C",
    }


def _limit_child() -> None:  # pragma: no cover - runs in the child process
    os.setsid()
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))


def measure_tree(root: Path, stop_after_files: int = MAX_FILES + 1) -> Tuple[int, int]:
    """Return (bytes, files) for a directory tree without following symlinks."""
    total, files = 0, 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if files >= stop_after_files:
                        return total, files
        except OSError:
            continue
    return total, files


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def clone_with_limits(
    ref: RepositoryRef,
    dest: Path,
    timeout: float = CLONE_TIMEOUT_SECONDS,
    max_bytes: int = MAX_WORKSPACE_BYTES,
    max_files: int = MAX_FILES,
    command: Optional[List[str]] = None,
) -> Tuple[int, int]:
    """Clone into ``dest`` with the watchdog enforcing quotas while git is running."""
    home = Path(tempfile.mkdtemp(prefix="dua_git_home_"))
    cmd = command or build_clone_command(ref, dest)
    try:
        proc = subprocess.Popen(
            cmd,
            env=build_git_env(home),
            cwd=str(home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            preexec_fn=_limit_child,
        )
        started = time.monotonic()
        violation: Optional[RepositoryError] = None
        while proc.poll() is None:
            time.sleep(WATCHDOG_INTERVAL_SECONDS)
            if time.monotonic() - started > timeout:
                violation = RepositoryError("timeout", f"Cloning took longer than {int(timeout)} seconds.", 504)
            elif dest.exists():
                used, count = measure_tree(dest, stop_after_files=max_files + 1)
                if used > max_bytes:
                    violation = RepositoryError(
                        "too_large", f"The repository exceeded the {max_bytes // (1024 * 1024)} MB workspace limit.", 413
                    )
                elif count > max_files:
                    violation = RepositoryError(
                        "too_many_files", f"The repository has more than {max_files:,} files.", 413
                    )
            if violation:
                _kill_group(proc)
                raise violation

        stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            if proc.returncode in (-signal.SIGXFSZ, 128 + signal.SIGXFSZ) or "File size limit" in stderr:
                raise RepositoryError(
                    "too_large", f"A file in the repository exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit.", 413
                )
            logger.warning("git clone failed (%s): %s", proc.returncode, stderr[-500:])
            raise RepositoryError("clone_failed", "The repository could not be cloned.", 502)

        used, count = measure_tree(dest, stop_after_files=max_files + 1)
        if used > max_bytes:
            raise RepositoryError("too_large", f"The repository exceeded the {max_bytes // (1024 * 1024)} MB workspace limit.", 413)
        if count > max_files:
            raise RepositoryError("too_many_files", f"The repository has more than {max_files:,} files.", 413)
        return used, count
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _read_head(dest: Path) -> Optional[str]:
    head = dest / ".git" / "HEAD"
    try:
        content = head.read_text(encoding="utf-8").strip()
        if content.startswith("ref: "):
            ref_file = dest / ".git" / content[5:]
            if ref_file.is_file():
                return ref_file.read_text(encoding="utf-8").strip()[:40]
            packed = dest / ".git" / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(content[5:]):
                        return line.split(" ", 1)[0][:40]
            return None
        return content[:40]
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Discovery (FR-2R)
# ---------------------------------------------------------------------------
def classify_manifest(file_name: str) -> Optional[str]:
    lower = file_name.lower()
    if lower == "package.json":
        return "package_json"
    if lower == "pyproject.toml":
        return "pyproject"
    if is_requirements_filename(file_name):
        return "requirements"
    return None


def discover_manifests(root: Path) -> DiscoveryResult:
    root = root.resolve()
    result = DiscoveryResult()
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        dirs[:] = sorted(
            d for d in dirs
            if d not in DISCOVERY_EXCLUDED_DIRS
            and not d.endswith(".egg-info")
            and not os.path.islink(os.path.join(current, d))
            and depth < MAX_DISCOVERY_DEPTH
        )
        for name in sorted(files):
            result.files_scanned += 1
            if result.files_scanned > MAX_FILES:
                result.truncated = True
                return result
            kind = classify_manifest(name)
            if not kind:
                continue
            path = current_path / name
            try:
                st = os.lstat(path)
            except OSError:
                continue
            rel = str(path.relative_to(root))
            if not os.path.isfile(path) or os.path.islink(path):
                result.skipped.append(f"{rel}: not a regular file")
                continue
            if st.st_size > MAX_MANIFEST_BYTES:
                result.skipped.append(f"{rel}: larger than {MAX_MANIFEST_BYTES // 1024} KB")
                continue
            if len(result.manifests) >= MAX_MANIFESTS:
                result.truncated = True
                result.skipped.append(f"Stopped after {MAX_MANIFESTS} manifests")
                return result
            result.manifests.append(DiscoveredFile(relative_path=rel, kind=kind, size_bytes=st.st_size))
    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def repositories_root(workspace_root: Path) -> Path:
    path = (workspace_root / "repos").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def acquire_repository(
    raw_url: str,
    workspace_root: Path,
    session: Optional[requests.Session] = None,
    resolver: Callable = socket.getaddrinfo,
    run_id: Optional[str] = None,
) -> AcquiredRepository:
    ref = parse_repository_url(raw_url)
    metadata = fetch_repository_metadata(ref, session=session, resolver=resolver)
    _assert_public_resolution(GITHUB_HOST, resolver)

    if not _clone_slots.acquire(blocking=False):
        raise RepositoryError("busy", "Another repository is being cloned. Try again in a moment.", 429)
    dest = repositories_root(workspace_root) / (run_id or uuid.uuid4().hex)
    try:
        used, count = clone_with_limits(ref, dest)
        commit = _read_head(dest)
        shutil.rmtree(dest / ".git", ignore_errors=True)
        return AcquiredRepository(
            ref=ref, metadata=metadata, workspace=dest, commit_sha=commit, bytes_on_disk=used, file_count=count
        )
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        _clone_slots.release()


def cleanup_expired_workspaces(workspace_root: Path, ttl_seconds: float = 3600.0) -> int:
    """Remove cloned workspaces older than the TTL. Returns the number removed."""
    removed = 0
    root = repositories_root(workspace_root)
    now = time.time()
    for entry in root.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink() and now - entry.stat().st_mtime > ttl_seconds:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
