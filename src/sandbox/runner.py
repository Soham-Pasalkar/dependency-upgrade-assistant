"""Provider-independent sandbox runner abstraction and Docker execution engine."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

from src.sandbox.container_config import (
    ContainerConfigBuilder,
    DEFAULT_NON_ROOT_USER,
    DEFAULT_TIMEOUT_SECONDS,
    SandboxSecurityConfig,
)
from src.sandbox.image_catalog import DEFAULT_PYTHON_IMAGE

logger = logging.getLogger(__name__)

# Maximum output size (512 KB) to prevent backend memory exhaustion
MAX_OUTPUT_BYTES = 512 * 1024


@dataclass(frozen=True)
class DockerAvailability:
    """Status indicating whether Docker runtime is available and reachable."""
    is_available: bool
    version: Optional[str] = None
    error_message: Optional[str] = None


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
    container_id: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.is_runnable and not self.timed_out and self.exit_code == 0


class SandboxRunnerInterface(ABC):
    """Abstract interface defining the contract for sandbox execution providers."""

    @abstractmethod
    def check_availability(self, timeout: float = 5.0) -> DockerAvailability:
        """Verify whether the sandbox runtime environment is available."""
        pass

    @abstractmethod
    def is_sandbox_available(self) -> bool:
        """Convenience method checking whether the sandbox is operational."""
        pass

    @abstractmethod
    def run_command(
        self,
        command_args: List[str],
        working_dir: Path | str,
        reference_dir: Optional[Path | str] = None,
        timeout: Optional[int] = None,
        config: Optional[SandboxSecurityConfig] = None,
    ) -> SandboxExecutionResult:
        """Execute a whitelisted command inside the isolated sandbox."""
        pass

    @abstractmethod
    def terminate(self, container_name_or_id: str) -> bool:
        """Forcefully terminate and remove an active container."""
        pass


class DockerSandboxRunner(SandboxRunnerInterface):
    """Production Docker sandbox runner enforcing strict security, resource limits, and cleanup."""

    def __init__(
        self,
        config: Optional[SandboxSecurityConfig] = None,
        docker_binary: Optional[str] = None,
    ) -> None:
        self.config = config or SandboxSecurityConfig()
        self.docker_bin = docker_binary or shutil.which("docker") or "docker"
        self._cached_availability: Optional[DockerAvailability] = None
        self._active_containers: Set[str] = set()
        self._lock = threading.Lock()

    def check_availability(self, timeout: float = 5.0) -> DockerAvailability:
        """Verify whether Docker CLI exists and daemon is reachable."""
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
        config: Optional[SandboxSecurityConfig] = None,
    ) -> SandboxExecutionResult:
        """Execute a whitelisted command inside the isolated Docker sandbox.
        
        Fail-Closed Guarantee:
        If Docker is unavailable, untrusted repository code is NEVER executed on the host.
        Instead, returns an explicit NOT_RUNNABLE execution result.
        """
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

        effective_config = config or self.config
        effective_timeout = timeout if timeout is not None else effective_config.timeout_seconds

        container_name = f"dua-sandbox-{uuid.uuid4().hex[:12]}"

        # Construct hardened docker run arguments
        run_args = ContainerConfigBuilder.build_run_args(
            config=effective_config,
            working_dir=working_dir,
            reference_dir=reference_dir,
            command_args=command_args,
            container_name=container_name,
        )

        full_cmd = [self.docker_bin] + run_args
        start_time = time.monotonic()

        with self._lock:
            self._active_containers.add(container_name)

        try:
            proc = subprocess.run(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=effective_timeout,
                check=False,
            )
            duration = round(time.monotonic() - start_time, 2)
            stdout_bounded = proc.stdout[:MAX_OUTPUT_BYTES]
            stderr_bounded = proc.stderr[:MAX_OUTPUT_BYTES]
            if len(proc.stdout) > MAX_OUTPUT_BYTES:
                stdout_bounded += f"\n... [Output truncated after reaching {MAX_OUTPUT_BYTES} bytes limit] ...\n"

            return SandboxExecutionResult(
                exit_code=proc.returncode,
                stdout=stdout_bounded,
                stderr=stderr_bounded,
                duration_seconds=duration,
                timed_out=False,
                is_runnable=True,
                container_id=container_name,
            )
        except subprocess.TimeoutExpired as exc:
            duration = round(time.monotonic() - start_time, 2)
            # Fail-closed cleanup: kill the running container immediately
            self.terminate(container_name)
            out = exc.stdout if isinstance(exc.stdout, str) else (
                exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
            )
            err = exc.stderr if isinstance(exc.stderr, str) else (
                exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            )
            return SandboxExecutionResult(
                exit_code=124,
                stdout=out[:MAX_OUTPUT_BYTES],
                stderr=err[:MAX_OUTPUT_BYTES],
                duration_seconds=duration,
                timed_out=True,
                is_runnable=True,
                container_id=container_name,
                error_message=f"Command execution timed out after {effective_timeout}s in sandbox.",
            )
        except Exception as exc:
            duration = round(time.monotonic() - start_time, 2)
            self.terminate(container_name)
            return SandboxExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                duration_seconds=duration,
                is_runnable=False,
                container_id=container_name,
                error_message=f"Container execution error: {exc}",
            )
        finally:
            with self._lock:
                self._active_containers.discard(container_name)
            # Defense-in-depth cleanup: ensure container is removed even if --rm failed
            self._ensure_container_removed(container_name)

    def terminate(self, container_name_or_id: str) -> bool:
        """Forcefully terminate and remove an active container."""
        if not container_name_or_id:
            return False
        try:
            subprocess.run(
                [self.docker_bin, "rm", "-f", container_name_or_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                check=False,
            )
            return True
        except Exception as exc:
            logger.warning(f"Error terminating container {container_name_or_id}: {exc}")
            return False

    def _ensure_container_removed(self, container_name: str) -> None:
        """Non-blocking attempt to remove container if still lingering."""
        try:
            subprocess.run(
                [self.docker_bin, "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                check=False,
            )
        except Exception:
            pass
