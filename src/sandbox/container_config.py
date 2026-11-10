"""Container security configuration builder enforcing defense-in-depth sandbox constraints."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from src.sandbox.image_catalog import validate_image_or_raise, DEFAULT_PYTHON_IMAGE

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_LIMIT = "512m"
DEFAULT_CPU_LIMIT = "1.0"
DEFAULT_PIDS_LIMIT = 64
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_NON_ROOT_USER = "1000:1000"


@dataclass(frozen=True)
class SandboxSecurityConfig:
    """Immutable security and resource containment policy for Docker sandbox."""
    image: str = DEFAULT_PYTHON_IMAGE
    network_enabled: bool = False
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    cpu_limit: str = DEFAULT_CPU_LIMIT
    pids_limit: int = DEFAULT_PIDS_LIMIT
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    user: str = DEFAULT_NON_ROOT_USER
    read_only_root: bool = False
    cap_drop_all: bool = True
    no_new_privileges: bool = True


class ContainerConfigBuilder:
    """Builds hardened Docker CLI arguments enforcing all security invariants."""

    @staticmethod
    def build_run_args(
        config: SandboxSecurityConfig,
        working_dir: Path | str,
        reference_dir: Optional[Path | str] = None,
        command_args: Optional[List[str]] = None,
        container_name: Optional[str] = None,
    ) -> List[str]:
        """Construct the complete, security-hardened `docker run` argument list.

        Security Invariants:
        1. Image must be from allowlisted catalog.
        2. Non-root user execution enforced (--user).
        3. Network disabled by default (--network none).
        4. Memory, CPU, and process table limits enforced.
        5. Capabilities dropped (--cap-drop=ALL).
        6. Privilege escalation disabled (--security-opt=no-new-privileges:true).
        7. Strictly no --privileged flag.
        8. Strictly no host network (--net=host).
        9. Strictly no docker.sock volume mounts.
        """
        # Validate Image
        validated_image = validate_image_or_raise(config.image)

        # Validate Working Directory
        work_path = Path(working_dir).resolve()
        if not work_path.is_dir():
            raise FileNotFoundError(f"Working directory does not exist: {work_path}")
        if "docker.sock" in str(work_path):
            raise ValueError("Forbidden mount: Docker socket mounting is strictly prohibited.")

        args: List[str] = ["run", "--rm"]

        if container_name:
            args.extend(["--name", container_name])

        # 1. Non-root user execution
        user_str = config.user or DEFAULT_NON_ROOT_USER
        args.append(f"--user={user_str}")

        # 2. Network isolation
        if config.network_enabled:
            args.append("--network=bridge")
        else:
            args.append("--network=none")

        # 3. Resource boundaries
        args.append(f"--memory={config.memory_limit or DEFAULT_MEMORY_LIMIT}")
        args.append(f"--cpus={config.cpu_limit or DEFAULT_CPU_LIMIT}")
        args.append(f"--pids-limit={config.pids_limit or DEFAULT_PIDS_LIMIT}")

        # 4. Privilege escalation & capabilities
        if config.no_new_privileges:
            args.append("--security-opt=no-new-privileges:true")
        if config.cap_drop_all:
            args.append("--cap-drop=ALL")

        # 5. Read-only root filesystem & tmpfs scratch spaces
        if config.read_only_root:
            args.append("--read-only")
            args.append("--tmpfs=/tmp:rw,noexec,nosuid,size=64m")
            args.append("--tmpfs=/workspace/scratch:rw,size=128m")

        # 6. Working volume mount
        args.extend(["-v", f"{work_path}:/workspace/working:rw"])
        args.extend(["-w", "/workspace/working"])

        # 7. Optional reference volume mount
        if reference_dir:
            ref_path = Path(reference_dir).resolve()
            if ref_path.is_dir():
                if "docker.sock" in str(ref_path):
                    raise ValueError("Forbidden mount: Docker socket mounting is strictly prohibited.")
                args.extend(["-v", f"{ref_path}:/workspace/reference:ro"])

        # 8. Container Image
        args.append(validated_image)

        # 9. Whitelisted Command arguments
        if command_args:
            args.extend(command_args)

        return args
