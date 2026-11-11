"""Unit tests for DockerSandboxManager, security constraints, and fail-closed isolation."""
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.validation.sandbox_manager import (
    DockerAvailability,
    DockerSandboxManager,
    SandboxExecutionResult,
    SandboxSecurityConfig,
)


def test_docker_availability_missing_binary():
    """Verify clean unavailability detection when docker binary is not in PATH."""
    manager = DockerSandboxManager(docker_binary="non_existent_docker_bin_12345")
    status = manager.check_availability()

    assert status.is_available is False
    assert "Docker binary not found" in str(status.error_message)


@patch("src.validation.sandbox_manager.shutil.which", return_value="/usr/local/bin/docker")
@patch("src.validation.sandbox_manager.subprocess.run")
def test_docker_availability_daemon_running(mock_run: MagicMock, mock_which: MagicMock):
    """Verify successful detection when docker info responds with version."""
    mock_run.return_value = MagicMock(returncode=0, stdout="25.0.3\n", stderr="")
    manager = DockerSandboxManager()
    status = manager.check_availability()

    assert status.is_available is True
    assert status.version == "25.0.3"


@patch("src.validation.sandbox_manager.shutil.which", return_value="/usr/local/bin/docker")
@patch("src.validation.sandbox_manager.subprocess.run")
def test_docker_availability_daemon_unreachable(mock_run: MagicMock, mock_which: MagicMock):
    """Verify unavailability detection when docker daemon is stopped."""
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
    )
    manager = DockerSandboxManager()
    status = manager.check_availability()

    assert status.is_available is False
    assert "daemon unreachable" in str(status.error_message).lower()


@patch.object(DockerSandboxManager, "check_availability")
def test_run_command_fails_closed_when_docker_unavailable(mock_avail: MagicMock, tmp_path: Path):
    """CRITICAL SECURITY TEST: Ensure untrusted code is NEVER run on host when Docker is unavailable."""
    mock_avail.return_value = DockerAvailability(
        is_available=False,
        error_message="Docker daemon is not running.",
    )
    work_dir = tmp_path / "working"
    work_dir.mkdir()

    manager = DockerSandboxManager()
    res = manager.run_command(command_args=["pytest", "-v"], working_dir=work_dir)

    assert res.is_runnable is False
    assert res.exit_code == -1
    assert "Sandbox unavailable" in str(res.error_message)
    assert "untrusted repository code will not be executed on the host" in str(res.error_message)


@patch.object(DockerSandboxManager, "check_availability")
@patch("src.validation.sandbox_manager.subprocess.run")
def test_run_command_enforces_mandatory_security_flags(
    mock_run: MagicMock,
    mock_avail: MagicMock,
    tmp_path: Path,
):
    """CRITICAL SECURITY TEST: Verify container is spawned with mandatory isolation flags."""
    mock_avail.return_value = DockerAvailability(is_available=True, version="25.0.3")
    mock_run.return_value = MagicMock(returncode=0, stdout="Tests passed", stderr="")

    work_dir = tmp_path / "working"
    work_dir.mkdir()
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()

    config = SandboxSecurityConfig(
        image="python:3.11-slim",
        network_enabled=False,
        memory_limit="512m",
        cpu_limit="1.0",
        pids_limit=64,
        user="1000:1000",
    )
    manager = DockerSandboxManager(config=config)

    res = manager.run_command(
        command_args=["pytest", "-v"],
        working_dir=work_dir,
        reference_dir=ref_dir,
    )

    assert res.is_success is True

    # Inspect command array passed to subprocess.run for docker run
    called_cmd = mock_run.call_args_list[0][0][0]
    cmd_str = " ".join(called_cmd)

    # 1. Non-root user execution
    assert "--user=1000:1000" in called_cmd

    # 2. Network disabled
    assert "--network=none" in called_cmd

    # 3. Resource boundaries
    assert "--memory=512m" in called_cmd
    assert "--cpus=1.0" in called_cmd
    assert "--pids-limit=64" in called_cmd

    # 4. Strictly no privileged container
    assert "--privileged" not in called_cmd

    # 5. Strictly no docker.sock mount
    assert "docker.sock" not in cmd_str

    # 6. Reference copy mounted strictly read-only
    ref_mount = f"{ref_dir.resolve()}:/workspace/reference:ro"
    assert ref_mount in called_cmd

    # 7. Working copy mounted read-write
    work_mount = f"{work_dir.resolve()}:/workspace/working:rw"
    assert work_mount in called_cmd


@patch.object(DockerSandboxManager, "check_availability")
def test_run_command_rejects_docker_socket_mount(mock_avail: MagicMock, tmp_path: Path):
    """Verify attempt to mount docker socket throws ValueError."""
    mock_avail.return_value = DockerAvailability(is_available=True)
    fake_sock_dir = tmp_path / "docker.sock"
    fake_sock_dir.mkdir()

    manager = DockerSandboxManager()
    with pytest.raises(ValueError, match="Forbidden mount: Docker socket mounting is strictly prohibited"):
        manager.run_command(command_args=["pytest"], working_dir=fake_sock_dir)


@patch.object(DockerSandboxManager, "check_availability")
@patch("src.validation.sandbox_manager.subprocess.run")
def test_run_command_timeout(mock_run: MagicMock, mock_avail: MagicMock, tmp_path: Path):
    """Verify graceful handling when container execution exceeds timeout."""
    mock_avail.return_value = DockerAvailability(is_available=True)
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker run", timeout=10)

    work_dir = tmp_path / "working"
    work_dir.mkdir()

    manager = DockerSandboxManager()
    res = manager.run_command(command_args=["pytest"], working_dir=work_dir, timeout=10)

    assert res.timed_out is True
    assert res.exit_code == 124
    assert "timed out after 10s" in str(res.error_message)


@patch.object(DockerSandboxManager, "check_availability")
@patch("src.validation.sandbox_manager.subprocess.run")
def test_legacy_sandbox_manager_unconditionally_enforces_network_none(
    mock_run: MagicMock,
    mock_avail: MagicMock,
    tmp_path: Path,
):
    """CRITICAL SECURITY REGRESSION: Legacy manager MUST NOT enable bridge networking even if network_enabled=True."""
    mock_avail.return_value = DockerAvailability(is_available=True)
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    # Pass network_enabled=True
    config = SandboxSecurityConfig(network_enabled=True)
    manager = DockerSandboxManager(config=config)

    manager.run_command(command_args=["python", "-V"], working_dir=work_dir)

    called_cmd = mock_run.call_args_list[0][0][0]
    assert "--network=none" in called_cmd
    assert "--network=bridge" not in called_cmd


@patch.object(DockerSandboxManager, "check_availability")
@patch("src.validation.sandbox_manager.subprocess.run")
def test_legacy_sandbox_manager_enforces_read_only_and_cap_drop(
    mock_run: MagicMock,
    mock_avail: MagicMock,
    tmp_path: Path,
):
    """CRITICAL SECURITY REGRESSION: Legacy manager must enforce read-only root and dropped capabilities."""
    mock_avail.return_value = DockerAvailability(is_available=True)
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    manager = DockerSandboxManager()
    manager.run_command(command_args=["python", "-V"], working_dir=work_dir)

    called_cmd = mock_run.call_args_list[0][0][0]
    assert "--read-only" in called_cmd
    assert "--cap-drop=ALL" in called_cmd
    assert "--security-opt=no-new-privileges:true" in called_cmd
    assert any("--tmpfs=/tmp" in a for a in called_cmd)


@patch.object(DockerSandboxManager, "check_availability")
def test_legacy_sandbox_manager_rejects_root_user(mock_avail: MagicMock, tmp_path: Path):
    """CRITICAL SECURITY REGRESSION: Legacy manager must reject root container execution fail-closed."""
    mock_avail.return_value = DockerAvailability(is_available=True)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    config = SandboxSecurityConfig(user="0")
    manager = DockerSandboxManager(config=config)

    with pytest.raises(ValueError, match="Root container execution .* is strictly forbidden"):
        manager.run_command(command_args=["python", "-V"], working_dir=work_dir)


@patch.object(DockerSandboxManager, "check_availability")
@patch("src.validation.sandbox_manager.subprocess.run")
def test_legacy_sandbox_manager_timeout_forces_container_termination(
    mock_run: MagicMock,
    mock_avail: MagicMock,
    tmp_path: Path,
):
    """Verify that timeout in legacy manager forcefully removes the specific named container."""
    mock_avail.return_value = DockerAvailability(is_available=True)

    # First call is docker run (which raises TimeoutExpired), second/third is docker rm -f
    mock_run.side_effect = [
        subprocess.TimeoutExpired(cmd="docker run", timeout=10),
        MagicMock(returncode=0),
        MagicMock(returncode=0),
    ]

    work_dir = tmp_path / "work_timeout"
    work_dir.mkdir()

    manager = DockerSandboxManager()
    res = manager.run_command(command_args=["pytest"], working_dir=work_dir, timeout=10)

    assert res.timed_out is True
    assert res.exit_code == 124

    # Verify docker rm -f was called with the specific dua-legacy container name
    rm_calls = [
        call for call in mock_run.call_args_list
        if len(call[0]) > 0 and len(call[0][0]) >= 3 and call[0][0][1] == "rm" and call[0][0][2] == "-f"
    ]
    assert len(rm_calls) >= 1
    c_name = rm_calls[0][0][0][3]
    assert c_name.startswith("dua-legacy-")


@patch.object(DockerSandboxManager, "check_availability")
@patch("src.validation.sandbox_manager.subprocess.run")
def test_legacy_sandbox_manager_interruption_cleans_up_in_finally(
    mock_run: MagicMock,
    mock_avail: MagicMock,
    tmp_path: Path,
):
    """Verify that abnormal exception/interruption guarantees container removal in finally."""
    mock_avail.return_value = DockerAvailability(is_available=True)

    mock_run.side_effect = [
        RuntimeError("Simulated system interruption"),
        MagicMock(returncode=0),
        MagicMock(returncode=0),
    ]

    work_dir = tmp_path / "work_interrupt"
    work_dir.mkdir()

    manager = DockerSandboxManager()
    res = manager.run_command(command_args=["pytest"], working_dir=work_dir)

    assert res.is_runnable is False
    assert "Simulated system interruption" in res.error_message

    rm_calls = [
        call for call in mock_run.call_args_list
        if len(call[0]) > 0 and len(call[0][0]) >= 3 and call[0][0][1] == "rm" and call[0][0][2] == "-f"
    ]
    assert len(rm_calls) >= 1
    assert rm_calls[0][0][0][3].startswith("dua-legacy-")


@patch("src.validation.sandbox_manager.subprocess.run")
def test_legacy_sandbox_manager_terminate_method(mock_run: MagicMock):
    """Verify manager.terminate forcefully terminates given container name."""
    mock_run.return_value = MagicMock(returncode=0)
    manager = DockerSandboxManager()
    assert manager.terminate("dua-legacy-target-123") is True
    called_cmd = mock_run.call_args_list[0][0][0]
    assert called_cmd == ["docker", "rm", "-f", "dua-legacy-target-123"]
