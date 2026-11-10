"""Unit tests for DockerSandboxRunner, bounded logging, timeout enforcement, and fail-closed cleanup."""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src.sandbox.container_config import SandboxSecurityConfig
from src.sandbox.runner import (
    DockerAvailability,
    DockerSandboxRunner,
    MAX_OUTPUT_BYTES,
    SandboxExecutionResult,
)


def test_runner_fails_closed_when_docker_binary_missing(tmp_path: Path):
    """CRITICAL SECURITY TEST: When docker is missing from PATH, untrusted code is never run on host."""
    runner = DockerSandboxRunner(docker_binary="non_existent_docker_binary_xyz")
    availability = runner.check_availability()

    assert availability.is_available is False
    assert "Docker binary not found" in str(availability.error_message)

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    res = runner.run_command(command_args=["pytest"], working_dir=work_dir)
    assert res.is_runnable is False
    assert res.exit_code == -1
    assert "Sandbox unavailable" in str(res.error_message)
    assert "untrusted repository code will not be executed on the host" in str(res.error_message)


@patch("src.sandbox.runner.shutil.which", return_value="/usr/local/bin/docker")
@patch("src.sandbox.runner.subprocess.run")
def test_runner_fails_closed_when_docker_daemon_unreachable(
    mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path
):
    """CRITICAL SECURITY TEST: When docker daemon is down, return NOT_RUNNABLE."""
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    )
    runner = DockerSandboxRunner()
    availability = runner.check_availability()
    assert availability.is_available is False
    assert "daemon unreachable" in str(availability.error_message).lower()

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    res = runner.run_command(command_args=["pytest"], working_dir=work_dir)
    assert res.is_runnable is False
    assert res.exit_code == -1


@patch.object(DockerSandboxRunner, "check_availability")
@patch("src.sandbox.runner.subprocess.run")
def test_runner_captures_stdout_stderr_and_exit_code(
    mock_run: MagicMock, mock_avail: MagicMock, tmp_path: Path
):
    """Verify clean exit code and bounded log capture on successful run."""
    mock_avail.return_value = DockerAvailability(is_available=True, version="26.0.0")
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="=== 5 passed in 0.45s ===\n",
        stderr="DeprecationWarning: something\n",
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    runner = DockerSandboxRunner()
    res = runner.run_command(command_args=["pytest", "-v"], working_dir=work_dir)

    assert res.is_success is True
    assert res.exit_code == 0
    assert "5 passed in 0.45s" in res.stdout
    assert "DeprecationWarning" in res.stderr
    assert res.timed_out is False
    assert res.is_runnable is True


@patch.object(DockerSandboxRunner, "check_availability")
@patch("src.sandbox.runner.subprocess.run")
def test_runner_captures_failure_exit_code(
    mock_run: MagicMock, mock_avail: MagicMock, tmp_path: Path
):
    """Verify non-zero exit code on failed tests."""
    mock_avail.return_value = DockerAvailability(is_available=True, version="26.0.0")
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="=== 1 failed, 2 passed ===\n",
        stderr="AssertionError: 1 != 2\n",
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    runner = DockerSandboxRunner()
    res = runner.run_command(command_args=["pytest"], working_dir=work_dir)

    assert res.is_success is False
    assert res.exit_code == 1
    assert "1 failed" in res.stdout
    assert "AssertionError" in res.stderr


@patch.object(DockerSandboxRunner, "check_availability")
@patch("src.sandbox.runner.subprocess.run")
def test_runner_handles_timeout_and_terminates_container(
    mock_run: MagicMock, mock_avail: MagicMock, tmp_path: Path
):
    """CRITICAL SECURITY TEST: Ensure container timeout kills process and sets timed_out=True."""
    mock_avail.return_value = DockerAvailability(is_available=True, version="26.0.0")
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["docker", "run"], timeout=10)

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    runner = DockerSandboxRunner()
    with patch.object(runner, "terminate") as mock_term:
        res = runner.run_command(command_args=["pytest"], working_dir=work_dir, timeout=10)

        assert res.timed_out is True
        assert res.exit_code == 124
        assert "timed out after 10s" in str(res.error_message)
        # Verify container was killed
        mock_term.assert_called_once()


@patch.object(DockerSandboxRunner, "check_availability")
@patch("src.sandbox.runner.subprocess.run")
def test_runner_bounds_log_output(
    mock_run: MagicMock, mock_avail: MagicMock, tmp_path: Path
):
    """Verify log output exceeding MAX_OUTPUT_BYTES is safely capped without memory blowup."""
    mock_avail.return_value = DockerAvailability(is_available=True, version="26.0.0")
    huge_stdout = "A" * (MAX_OUTPUT_BYTES + 1000)
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=huge_stdout,
        stderr="",
    )

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    runner = DockerSandboxRunner()
    res = runner.run_command(command_args=["python", "-c", "print('x')"], working_dir=work_dir)

    assert len(res.stdout) <= MAX_OUTPUT_BYTES + 200
    assert "Output truncated after reaching" in res.stdout


@patch.object(DockerSandboxRunner, "check_availability")
@patch("src.sandbox.runner.subprocess.run")
def test_runner_fail_closed_cleanup_in_finally(
    mock_run: MagicMock, mock_avail: MagicMock, tmp_path: Path
):
    """Verify container removal is guaranteed in finally block."""
    mock_avail.return_value = DockerAvailability(is_available=True, version="26.0.0")
    mock_run.side_effect = RuntimeError("Kernel failure")

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    runner = DockerSandboxRunner()
    with patch.object(runner, "_ensure_container_removed") as mock_cleanup:
        res = runner.run_command(command_args=["pytest"], working_dir=work_dir)
        assert res.exit_code == -1
        assert "Container execution error" in str(res.error_message)
        # Verify cleanup ran in finally
        mock_cleanup.assert_called_once()
