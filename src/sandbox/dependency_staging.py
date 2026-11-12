"""Controlled dependency staging manager enforcing safe acquisition and registry validation."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from src.models.manifest import Ecosystem

logger = logging.getLogger(__name__)

APPROVED_REGISTRY_HOSTS: Set[str] = {
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
}


@dataclass
class StagingResult:
    """Outcome of controlled dependency staging."""
    success: bool
    status: str  # "STAGED", "SKIPPED", "NOT_REQUIRED", "UNSUPPORTED", "UNSTAGEABLE", "REGISTRY_BLOCKED", "FAILED"
    cache_dir: Optional[Path] = None
    ecosystem: str = ""
    total_bytes: int = 0
    lockfile_path: Optional[Path] = None   # npm: lockfile generated for the proposed package.json
    npm_cache_dir: Optional[Path] = None   # npm: populated cache to copy into the snapshot
    artifact_count: int = 0
    staged_artifacts: List[str] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    message: str = ""

    def cleanup(self) -> None:
        """Clean up ephemeral staging cache directory."""
        if self.cache_dir and self.cache_dir.exists():
            try:
                import shutil
                shutil.rmtree(self.cache_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Error cleaning staging cache {self.cache_dir}: {e}")


def is_safe_registry_url(url: str) -> bool:
    """Verify that a package registry URL points strictly to approved public registries.
    
    Guarantees:
    - Rejects localhost, loopback, private IPs (10.x, 172.16.x, 192.168.x).
    - Rejects cloud metadata addresses (169.254.169.254).
    - Rejects non-HTTPS schemes.
    - Matches approved registry host allowlist.
    """
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme.lower() != "https":
            return False
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return False

        # Reject direct IP addresses and loopback/private destinations
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
                return False
        except ValueError:
            pass  # Not an IP string, check hostname against allowlist

        return hostname in APPROVED_REGISTRY_HOSTS
    except Exception:
        return False


class DependencyStagingManager:
    """Manages Stage A controlled dependency acquisition without executing untrusted code on host."""

    @staticmethod
    def prepare_staging_cache(
        ecosystem: Ecosystem | str,
        manifest_path: Optional[Path] = None,
        custom_registry: Optional[str] = None,
        manifest_content: Optional[str] = None,
        manifest_rel_path: Optional[str] = None,
        source_workspace: Optional[Path] = None,
        runner=None,
    ) -> StagingResult:
        """Prepare an isolated, offline dependency cache for container mounting.

        Safety Invariants:
        1. Host never executes untrusted setup.py, install scripts, or arbitrary packages.
           Python: pip download --only-binary=:all: (wheels only, no builds).
           npm: --ignore-scripts for both resolution and fetch; resolved URLs must be on the registry.
        2. Host tools run with an empty environment, isolated config and a pinned registry.
        3. Downloads are bounded in bytes and time while they run.
        4. When dependencies cannot be resolved securely, reports UNSTAGEABLE or FAILED; never fakes success.
        """
        if custom_registry and not is_safe_registry_url(custom_registry):
            return StagingResult(
                success=False,
                status="REGISTRY_BLOCKED",
                message=f"Access to registry '{custom_registry}' is blocked: destination is not in approved public registry allowlist.",
                diagnostics=["Blocked attempt to contact unapproved or non-public registry."],
            )

        temp_cache = tempfile.mkdtemp(prefix="dua_staging_cache_")
        cache_path = Path(temp_cache).resolve()
        eco_str = str(getattr(ecosystem, "value", ecosystem)).lower()

        if manifest_content is None:
            # No manifest supplied: nothing to stage (syntax-level validation only).
            return StagingResult(
                success=True,
                status="NOT_REQUIRED",
                cache_dir=cache_path,
                ecosystem=eco_str,
                message="No manifest supplied for staging. Container executes under strict --network none isolation.",
                diagnostics=["Offline execution policy active: container network disabled."],
            )

        run = runner or run_bounded
        try:
            if "npm" in eco_str or "node" in eco_str:
                return _stage_npm(cache_path, manifest_content, manifest_rel_path, source_workspace, run)
            return _stage_python(cache_path, manifest_content, run)
        except Exception as exc:  # fail closed
            logger.warning("Dependency staging failed: %s", exc)
            return StagingResult(
                success=False, status="FAILED", cache_dir=cache_path, ecosystem=eco_str,
                message="Dependencies could not be staged.", diagnostics=[str(exc)[:500]],
            )


# ---------------------------------------------------------------------------
# Limits and host-tool hardening
# ---------------------------------------------------------------------------
MAX_STAGING_BYTES = 300 * 1024 * 1024
STAGING_TIMEOUT_SECONDS = 180
CONTAINER_PYTHON_VERSION = "3.11"
NPM_REGISTRY = "https://registry.npmjs.org/"
PYPI_INDEX = "https://pypi.org/simple"


def container_arch() -> str:
    machine = platform.machine().lower()
    return "aarch64" if machine in ("arm64", "aarch64") else "x86_64"


def manylinux_platforms(arch: Optional[str] = None) -> List[str]:
    arch = arch or container_arch()
    tags = [f"manylinux_2_{minor}_{arch}" for minor in range(36, 16, -1)]
    tags += [f"manylinux2014_{arch}"]
    if arch == "x86_64":
        tags += ["manylinux2010_x86_64", "manylinux1_x86_64"]
    return tags


def _empty_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def host_tool_env(home: Path) -> Dict[str, str]:
    """Empty environment for pip/npm: no server secrets, no user config, pinned registries."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LC_ALL": "C",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "NPM_CONFIG_USERCONFIG": str(_empty_file(home / ".npmrc-user")),
        "NPM_CONFIG_GLOBALCONFIG": str(_empty_file(home / ".npmrc-global")),
        "NPM_CONFIG_REGISTRY": NPM_REGISTRY,
        "NPM_CONFIG_IGNORE_SCRIPTS": "true",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
    }


def run_bounded(cmd: List[str], cwd: Path, watch: Path, env: Dict[str, str],
                max_bytes: int = MAX_STAGING_BYTES, timeout: float = STAGING_TIMEOUT_SECONDS) -> Tuple[int, str]:
    """Run a host tool; kill it as soon as `watch` exceeds max_bytes or the timeout passes."""
    from src.services.repository_service import WATCHDOG_INTERVAL_SECONDS, _kill_group, measure_tree

    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    started = time.monotonic()
    while proc.poll() is None:
        time.sleep(WATCHDOG_INTERVAL_SECONDS)
        if time.monotonic() - started > timeout:
            _kill_group(proc)
            raise RuntimeError(f"Staging took longer than {int(timeout)} seconds.")
        used, _ = measure_tree(watch, stop_after_files=10**9) if watch.exists() else (0, 0)
        if used > max_bytes:
            _kill_group(proc)
            raise RuntimeError(f"Staged dependencies exceeded the {max_bytes // (1024 * 1024)} MB limit.")
    output = (proc.stdout.read() if proc.stdout else b"").decode("utf-8", "replace")
    return proc.returncode, output


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Python: wheels only, resolved by pip for the container platform
# ---------------------------------------------------------------------------
_UNSTAGEABLE_PATTERNS = re.compile(r"^\s*(-e\b|--editable\b|git\+|hg\+|svn\+|bzr\+|https?://|file:|\.|/)", re.I)


def build_pip_download_command(requirements_file: Path, dest: Path, arch: Optional[str] = None) -> List[str]:
    cmd = [
        sys.executable, "-m", "pip", "download",
        "--isolated", "--no-cache-dir", "--disable-pip-version-check", "--no-input",
        "--only-binary=:all:",
        "--index-url", PYPI_INDEX,
        "--python-version", CONTAINER_PYTHON_VERSION,
        "--implementation", "cp",
        "--abi", "cp311", "--abi", "abi3", "--abi", "none",
    ]
    for plat in manylinux_platforms(arch) + ["any"]:
        cmd += ["--platform", plat]
    cmd += ["--dest", str(dest), "-r", str(requirements_file)]
    return cmd


def _stage_python(cache_path: Path, content: str, run) -> StagingResult:
    unsupported = [ln.strip() for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")
                   and _UNSTAGEABLE_PATTERNS.match(ln)]
    if unsupported:
        return StagingResult(
            success=False, status="UNSTAGEABLE", cache_dir=cache_path, ecosystem="python",
            message="Some requirements point at local paths, editable installs or URLs and cannot be staged offline.",
            diagnostics=unsupported[:10],
        )
    if any(ln.strip().startswith(("-r", "-c", "--requirement", "--constraint")) for ln in content.splitlines()):
        return StagingResult(
            success=False, status="UNSTAGEABLE", cache_dir=cache_path, ecosystem="python",
            message="Nested requirement files are not staged; only this file's own requirements are supported.",
        )
    work = Path(tempfile.mkdtemp(prefix="pip_work_", dir=cache_path))  # inside the watched cache
    try:
        req = work / "requirements.txt"
        req.write_text(content, encoding="utf-8")
        wheels = cache_path / "python"
        wheels.mkdir(parents=True, exist_ok=True)
        code, output = run(build_pip_download_command(req, wheels), work, cache_path, host_tool_env(work))
        if code != 0:
            tail = [ln for ln in output.splitlines() if ln.strip()][-6:]
            unstageable = "no matching distribution" in output.lower() or "could not find a version" in output.lower()
            return StagingResult(
                success=False, status="UNSTAGEABLE" if unstageable else "FAILED", cache_dir=cache_path, ecosystem="python",
                message=(
                    "Some packages have no prebuilt wheel for the sandbox platform; source builds are not run on this machine."
                    if unstageable else "pip could not resolve the requirements."
                ),
                diagnostics=tail,
            )
        files = sorted(p for p in wheels.iterdir() if p.is_file())
        bad = [p.name for p in files if not p.name.endswith(".whl")]
        if bad:
            raise RuntimeError(f"Unexpected non-wheel artifacts: {bad[:5]}")
        digests = [f"{p.name} sha256:{_sha256_file(p)}" for p in files]
        total = sum(p.stat().st_size for p in files)
        return StagingResult(
            success=True, status="STAGED", cache_dir=cache_path, ecosystem="python",
            artifact_count=len(files), staged_artifacts=digests, total_bytes=total,
            message=f"Staged {len(files)} wheels ({total // 1024} KB) from PyPI for offline install.",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# npm: lockfile resolution and integrity-checked cache, scripts never run
# ---------------------------------------------------------------------------
def _lockfile_hosts(lock: dict) -> Set[str]:
    hosts: Set[str] = set()
    for meta in (lock.get("packages") or {}).values():
        resolved = meta.get("resolved") if isinstance(meta, dict) else None
        if resolved:
            hosts.add((urllib.parse.urlparse(resolved).hostname or "").lower())
    return hosts


def _stage_npm(cache_path: Path, content: str, rel_path: Optional[str], workspace: Optional[Path], run) -> StagingResult:
    try:
        manifest = json.loads(content)
    except ValueError as exc:
        return StagingResult(success=False, status="FAILED", cache_dir=cache_path, ecosystem="npm",
                             message="package.json is not valid JSON.", diagnostics=[str(exc)])
    if not (manifest.get("dependencies") or manifest.get("devDependencies")):
        return StagingResult(success=True, status="NOT_REQUIRED", cache_dir=cache_path, ecosystem="npm",
                             message="package.json declares no dependencies.")
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (manifest.get(section) or {}).items():
            if isinstance(spec, str) and re.match(r"^(file:|link:|git|github:|https?:|workspace:)", spec.strip(), re.I):
                return StagingResult(success=False, status="UNSTAGEABLE", cache_dir=cache_path, ecosystem="npm",
                                     message=f"'{name}' is not installed from the npm registry and cannot be staged offline.")

    work = Path(tempfile.mkdtemp(prefix="npm_work_", dir=cache_path))  # inside the watched cache
    try:
        (work / "package.json").write_text(content, encoding="utf-8")
        lock_note = "resolved fresh"
        if workspace and rel_path:
            existing = (Path(workspace) / rel_path).parent / "package-lock.json"
            if existing.is_file() and not existing.is_symlink() and existing.stat().st_size < 20 * 1024 * 1024:
                shutil.copyfile(existing, work / "package-lock.json")
                lock_note = "updated from the repository's package-lock.json"
        npm_cache = cache_path / "npm-cache"
        env = host_tool_env(work)
        base = ["npm", "--cache", str(npm_cache), "--registry", NPM_REGISTRY, "--ignore-scripts",
                "--no-audit", "--no-fund", "--loglevel", "error"]
        code, output = run(base[:1] + ["install", "--package-lock-only"] + base[1:], work, cache_path, env)
        if code != 0:
            return StagingResult(success=False, status="FAILED", cache_dir=cache_path, ecosystem="npm",
                                 message="npm could not resolve the dependencies.",
                                 diagnostics=[ln for ln in output.splitlines() if ln.strip()][-6:])
        lock = json.loads((work / "package-lock.json").read_text(encoding="utf-8"))
        foreign = _lockfile_hosts(lock) - {"registry.npmjs.org"}
        if foreign:
            return StagingResult(success=False, status="REGISTRY_BLOCKED", cache_dir=cache_path, ecosystem="npm",
                                 message="The lockfile resolves packages from hosts other than registry.npmjs.org.",
                                 diagnostics=sorted(foreign)[:10])
        # Fetch every tarball into the cache; npm verifies each against the lockfile integrity hash.
        code, output = run(base[:1] + ["ci"] + base[1:], work, cache_path, env)
        shutil.rmtree(work / "node_modules", ignore_errors=True)
        if code != 0:
            return StagingResult(success=False, status="FAILED", cache_dir=cache_path, ecosystem="npm",
                                 message="npm could not fetch the packages.",
                                 diagnostics=[ln for ln in output.splitlines() if ln.strip()][-6:])
        lock_out = cache_path / "package-lock.json"
        shutil.copyfile(work / "package-lock.json", lock_out)
        packages = [k for k in (lock.get("packages") or {}) if k]
        from src.services.repository_service import measure_tree
        total, _ = measure_tree(npm_cache)
        return StagingResult(
            success=True, status="STAGED", cache_dir=cache_path, ecosystem="npm", artifact_count=len(packages),
            total_bytes=total, lockfile_path=lock_out, npm_cache_dir=npm_cache,
            message=f"Staged {len(packages)} npm packages ({total // 1024} KB); lockfile {lock_note}.",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
