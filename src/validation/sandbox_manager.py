"""Dedicated Docker sandbox manager for safe, isolated execution of untrusted repository code.

Enforces strict containment guarantees:
- Non-root user execution (--user 1000:1000).
- Network disabled by default (--network none).
- Resource constraints (--memory=512m, --cpus=1.0, --pids-limit=64).
- No privileged mode (--privileged strictly prohibited).
- Docker socket mounting strictly forbidden (never mount /var/run/docker.sock).
- Reference copy mounted strictly read-only; working copy mounted read-write.
- Fail-closed security policy: if Docker is unavailable, tests are classified NOT_RUNNABLE.
- Untrusted repository code is NEVER executed on the host system.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER_IMAGE = "python:3.11-slim"
DEFAULT_MEMORY_LIMIT = "512m"
DEFAULT_CPU_LIMIT = "1.0"
DEFAULT_PIDS_LIMIT = 64
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_NON_ROOT_USER = "1000:1000"


@dataclass(frozen=True)
class DockerAvailability:
    """Status indicating whether Docker runtime is available and reachable."""
    is_available: bool
    version: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class SandboxSecurityConfig:
    """Security and resource containment policy for Docker sandbox."""
    image: str = DEFAULT_CONTAINER_IMAGE
    network_enabled: bool = False
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    cpu_limit: str = DEFAULT_CPU_LIMIT
    pids_limit: int = DEFAULT_PIDS_LIMIT
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    user: str = DEFAULT_NON_ROOT_USER


@dataclass(frozen=True)
class SandboxExecutionResult:
    """Outcome of command execution inside the isolated sandbox."""
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    is_runnable: bool = True
    error_message: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.is_runnable and not self.timed_out and self.exit_code == 0


class DockerSandboxManager:
    """Manages containerized sandbox lifecycle, security checks, and execution."""

    def __init__(
        self,
        config: Optional[SandboxSecurityConfig] = None,
        docker_binary: Optional[str] = None,
    ) -> None:
        self.config = config or SandboxSecurityConfig()
        self.docker_bin = docker_binary or shutil.which("docker") or "docker"
        self._cached_availability: Optional[DockerAvailability] = None

    def check_availability(self, timeout: float = 5.0) -> DockerAvailability:
        """Verify whether Docker CLI exists and the daemon is reachable."""
        if not shutil.which(self.docker_bin):
            return DockerAvailability(
                is_available=False,
                error_message="Docker binary not found in system PATH. Docker is required for safe containerized validation.",
            )

        try:
            proc = subprocess.run(
                [self.docker_bin, "info", "--format", "{{.ServerVersion}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode == 0:
                version_str = proc.stdout.strip()
                return DockerAvailability(is_available=True, version=version_str)
            else:
                err = proc.stderr.strip() or proc.stdout.strip()
                return DockerAvailability(
                    is_available=False,
                    error_message=f"Docker daemon unreachable (exit code {proc.returncode}): {err}",
                )
        except subprocess.TimeoutExpired:
            return DockerAvailability(
                is_available=False,
                error_message=f"Docker daemon connection check timed out after {timeout}s.",
            )
        except Exception as exc:
            return DockerAvailability(
                is_available=False,
                error_message=f"Error checking Docker availability: {exc}",
            )

    def is_sandbox_available(self) -> bool:
        """Convenience method checking whether the sandbox is operational."""
        if self._cached_availability is None:
            self._cached_availability = self.check_availability()
        return self._cached_availability.is_available

    def run_command(
        self,
        command_args: List[str],
        working_dir: Path | str,
        reference_dir: Optional[Path | str] = None,
        timeout: Optional[int] = None,
    ) -> SandboxExecutionResult:
        """Execute a whitelisted command inside the isolated Docker sandbox.
        
        Args:
            command_args: Command list to execute (e.g. ["pytest", "-v"]).
            working_dir: Path to working repository copy on host (mounted read-write).
            reference_dir: Optional path to pristine reference copy (mounted read-only).
            timeout: Optional override for execution timeout.
            
        Returns:
            SandboxExecutionResult detailing exit code, stdout, stderr, and runnability.
        """
        # Fail-closed check: if Docker is unavailable, refuse execution
        availability = self.check_availability()
        if not availability.is_available:
            return SandboxExecutionResult(
                exit_code=-1,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                is_runnable=False,
                error_message=(
                    f"Sandbox unavailable: {availability.error_message}. "
                    "In accordance with security policy, untrusted repository code will not be executed on the host."
                ),
            )

        if not command_args:
            raise ValueError("Command arguments list cannot be empty.")

        work_path = Path(working_dir).resolve()
        if not work_path.is_dir():
            raise FileNotFoundError(f"Working directory does not exist: {work_path}")

        # Security Assertions on mounts
        if "docker.sock" in str(work_path):
            raise ValueError("Forbidden mount: Docker socket mounting is strictly prohibited.")

        effective_timeout = timeout if timeout is not None else self.config.timeout_seconds

        # Build secure container execution command
        container_cmd = [
            self.docker_bin,
            "run",
            "--rm",
            f"--user={self.config.user}",
            f"--network={'bridge' if self.config.network_enabled else 'none'}",
            f"--memory={self.config.memory_limit}",
            f"--cpus={self.config.cpu_limit}",
            f"--pids-limit={self.config.pids_limit}",
            "-v", f"{work_path}:/workspace/working:rw",
            "-w", "/workspace/working",
        ]

        if reference_dir:
            ref_path = Path(reference_dir).resolve()
            if ref_path.is_dir():
                if "docker.sock" in str(ref_path):
                    raise ValueError("Forbidden mount: Docker socket mounting is strictly prohibited.")
                container_cmd.extend(["-v", f"{ref_path}:/workspace/reference:ro"])

        container_cmd.append(self.config.image)
        container_cmd.extend(command_args)

        start_time = time.monotonic()
        try:
            proc = subprocess.run(
                container_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=effective_timeout,
                check=False,
            )
            duration = round(time.monotonic() - start_time, 2)
            return SandboxExecutionResult(
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_seconds=duration,
                timed_out=False,
                is_runnable=True,
            )
        except subprocess.TimeoutExpired as exc:
            duration = round(time.monotonic() - start_time, 2)
            out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout.decode("utf-8", errors="replace") if exc.stdout else "")
            err = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr.decode("utf-8", errors="replace") if exc.stderr else "")
            return SandboxExecutionResult(
                exit_code=124,
                stdout=out,
                stderr=err,
                duration_seconds=duration,
                timed_out=True,
                is_runnable=True,
                error_message=f"Command execution timed out after {effective_timeout}s in sandbox.",
            )
        except Exception as exc:
            duration = round(time.monotonic() - start_time, 2)
            return SandboxExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                duration_seconds=duration,
                is_runnable=False,
                error_message=f"Container execution error: {exc}",
            )

