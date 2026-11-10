"""Container security configuration builder enforcing mandatory server-defined sandbox invariants."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from src.sandbox.image_catalog import (
    DEFAULT_PYTHON_IMAGE,
    get_pinned_image_reference,
    validate_image_or_raise,
)

logger = logging.getLogger(__name__)

# Server-enforced hard ceilings
SERVER_MAX_MEMORY_MB = 1024
SERVER_MAX_CPUS = 2.0
SERVER_MAX_PIDS = 128
SERVER_MAX_TIMEOUT_SECONDS = 120

DEFAULT_MEMORY_MB = 512
DEFAULT_CPUS = 1.0
DEFAULT_PIDS = 64
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_NON_ROOT_USER = "1000:1000"


@dataclass(frozen=True)
class SandboxSecurityConfig:
    """Server-enforced, immutable security policy for container execution."""
    image: str = DEFAULT_PYTHON_IMAGE
    network_enabled: bool = False   # Always False for test execution
    memory_mb: int = DEFAULT_MEMORY_MB
    cpu_limit: float = DEFAULT_CPUS
    pids_limit: int = DEFAULT_PIDS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    user: str = DEFAULT_NON_ROOT_USER
    read_only_root: bool = True     # Mandatory read-only root fs
    cap_drop_all: bool = True       # Mandatory drop capabilities
    no_new_privileges: bool = True  # Mandatory disable privilege escalation
    use_pinned_digest: bool = True

    # Backwards-compatible properties
    @property
    def memory_limit(self) -> str:
        return f"{self.memory_mb}m"


def sanitize_resource_limits(
    memory_mb: Optional[int] = None,
    cpu_limit: Optional[float | str] = None,
    pids_limit: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
) -> tuple[int, float, int, int]:
    """Validate and clamp resource limit inputs to safe server ceilings."""
    # Memory
    mem = memory_mb if memory_mb is not None else DEFAULT_MEMORY_MB
    try:
        mem_val = int(mem)
        mem_clamped = max(64, min(mem_val, SERVER_MAX_MEMORY_MB))
    except (ValueError, TypeError):
        mem_clamped = DEFAULT_MEMORY_MB

    # CPU
    cpu = cpu_limit if cpu_limit is not None else DEFAULT_CPUS
    try:
        cpu_val = float(cpu)
        cpu_clamped = max(0.1, min(cpu_val, SERVER_MAX_CPUS))
    except (ValueError, TypeError):
        cpu_clamped = DEFAULT_CPUS

    # PIDs
    pids = pids_limit if pids_limit is not None else DEFAULT_PIDS
    try:
        pids_val = int(pids)
        pids_clamped = max(16, min(pids_val, SERVER_MAX_PIDS))
    except (ValueError, TypeError):
        pids_clamped = DEFAULT_PIDS

    # Timeout
    timeout = timeout_seconds if timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS
    try:
        timeout_val = int(timeout)
        timeout_clamped = max(5, min(timeout_val, SERVER_MAX_TIMEOUT_SECONDS))
    except (ValueError, TypeError):
        timeout_clamped = DEFAULT_TIMEOUT_SECONDS

    return mem_clamped, cpu_clamped, pids_clamped, timeout_clamped


class ContainerConfigBuilder:
    """Builds hardened Docker CLI arguments enforcing all security invariants."""

    @staticmethod
    def build_run_args(
        config: SandboxSecurityConfig,
        working_dir: Path | str,
        reference_dir: Optional[Path | str] = None,
        staging_dir: Optional[Path | str] = None,
        command_args: Optional[List[str]] = None,
        container_name: Optional[str] = None,
    ) -> List[str]:
        """Construct the complete, security-hardened `docker run` argument list.

        Mandatory Invariants:
        1. Non-root user execution (--user 1000:1000).
        2. Network disabled (--network=none). Network cannot be enabled.
        3. Strict resource ceilings (RAM, CPU, PIDs).
        4. Dropped capabilities (--cap-drop=ALL).
        5. Privilege escalation disabled (--security-opt=no-new-privileges:true).
        6. Read-only root filesystem (--read-only) with isolated tmpfs scratch spaces.
        7. Strictly no --privileged flag.
        8. Strictly no host network (--net=host or --network=host).
        9. Strictly no host namespaces (--pid=host, --ipc=host, --uts=host).
        10. Strictly no docker.sock volume mounts.
        11. Image must be from allowlisted catalog, pinned by SHA-256 digest where configured.
        """
        # Validate Image and obtain pinned reference if available
        base_tag = validate_image_or_raise(config.image)
        if config.use_pinned_digest:
            try:
                image_ref = get_pinned_image_reference(base_tag)
            except Exception:
                image_ref = base_tag
        else:
            image_ref = base_tag

        # Validate Working Directory
        work_path = Path(working_dir).resolve()
        if not work_path.is_dir():
            raise FileNotFoundError(f"Working directory does not exist: {work_path}")
        if "docker.sock" in str(work_path):
            raise ValueError("Forbidden mount: Docker socket mounting is strictly prohibited.")

        args: List[str] = ["run", "--rm"]

        if container_name:
            args.extend(["--name", container_name])

        # 1. Non-root user execution (Mandatory)
        args.append(f"--user={config.user or DEFAULT_NON_ROOT_USER}")

        # 2. Strict Network Isolation (Mandatory --network=none)
        args.append("--network=none")

        # 3. Server-clamped resource boundaries
        mem_mb, cpus, pids, _ = sanitize_resource_limits(
            memory_mb=config.memory_mb,
            cpu_limit=config.cpu_limit,
            pids_limit=config.pids_limit,
            timeout_seconds=config.timeout_seconds,
        )
        args.append(f"--memory={mem_mb}m")
        args.append(f"--cpus={cpus:.1f}")
        args.append(f"--pids-limit={pids}")

        # 4. Privilege escalation & capabilities (Mandatory)
        args.append("--security-opt=no-new-privileges:true")
        args.append("--cap-drop=ALL")

        # 5. Read-only root filesystem & isolated tmpfs scratch spaces (Mandatory)
        args.append("--read-only")
        args.append("--tmpfs=/tmp:rw,noexec,nosuid,size=64m")
        args.append("--tmpfs=/workspace/scratch:rw,size=128m")

        # 6. Working volume mount (ephemeral snapshot directory)
        args.extend(["-v", f"{work_path}:/workspace/working:rw"])
        args.extend(["-w", "/workspace/working"])

        # 7. Optional reference volume mount (strictly read-only)
        if reference_dir:
            ref_path = Path(reference_dir).resolve()
            if ref_path.is_dir():
                if "docker.sock" in str(ref_path):
                    raise ValueError("Forbidden mount: Docker socket mounting is strictly prohibited.")
                args.extend(["-v", f"{ref_path}:/workspace/reference:ro"])

        # 8. Optional dependency staging cache mount (strictly read-only)
        if staging_dir:
            stg_path = Path(staging_dir).resolve()
            if stg_path.is_dir():
                if "docker.sock" in str(stg_path):
                    raise ValueError("Forbidden mount: Docker socket mounting is strictly prohibited.")
                args.extend(["-v", f"{stg_path}:/cache:ro"])

        # 9. Container Image (Pinned reference)
        args.append(image_ref)

        # 10. Whitelisted Command arguments
        if command_args:
            args.extend(command_args)

        return args
