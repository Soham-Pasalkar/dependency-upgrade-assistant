"""Unit tests for DockerSandboxRunner, bounded streaming capture, timeout enforcement, and cancellation."""
import io
import subprocess
import time
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
from src.sandbox.streaming_capture import BoundedStreamCapture, strip_ansi_sequences


class FakePopen:
    """Mock process for streaming Popen execution."""

    def __init__(
        self,
        stdout_text: str = "",
        stderr_text: str = "",
        returncode: int | None = 0,
        wait_delay: float = 0.0,
        raise_timeout: bool = False,
        is_active: bool = False,
    ) -> None:
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.returncode = None if is_active else returncode
        self.wait_delay = wait_delay
        self.raise_timeout = raise_timeout
        self.killed = False
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        if self.wait_delay > 0:
            time.sleep(self.wait_delay)
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(cmd=["docker", "run"], timeout=timeout or 5.0)
        return self.returncode if self.returncode is not None else 0

    def poll(self) -> int | None:
        if self.killed or self.terminated:
            return -9
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


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
@patch("src.sandbox.runner.subprocess.Popen")
def test_runner_captures_stdout_stderr_and_exit_code(
    mock_popen: MagicMock, mock_avail: MagicMock, tmp_path: Path
):
    """Verify clean exit code and bounded log capture on successful run."""
    mock_avail.return_value = DockerAvailability(is_available=True, version="26.0.0")
    fake_proc = FakePopen(
        stdout_text="=== 5 passed in 0.45s ===\n",
        stderr_text="DeprecationWarning: something\n",
        returncode=0,
    )
    mock_popen.return_value = fake_proc

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
@patch("src.sandbox.runner.subprocess.Popen")
def test_runner_captures_failure_exit_code(
    mock_popen: MagicMock, mock_avail: MagicMock, tmp_path: Path
):
    """Verify non-zero exit code on failed tests."""
    mock_avail.return_value = DockerAvailability(is_available=True, version="26.0.0")
    fake_proc = FakePopen(
        stdout_text="=== 1 failed, 2 passed ===\n",
        stderr_text="AssertionError: 1 != 2\n",
        returncode=1,
    )
    mock_popen.return_value = fake_proc

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    runner = DockerSandboxRunner()
    res = runner.run_command(command_args=["pytest"], working_dir=work_dir)

    assert res.is_success is False
    assert res.exit_code == 1
    assert "1 failed" in res.stdout
    assert "AssertionError" in res.stderr


@patch.object(DockerSandboxRunner, "check_availability")
@patch("src.sandbox.runner.subprocess.Popen")
def test_runner_handles_timeout_and_terminates_container(
    mock_popen: MagicMock, mock_avail: MagicMock, tmp_path: Path
):
    """CRITICAL SECURITY TEST: Ensure container timeout kills process and sets timed_out=True."""
    mock_avail.return_value = DockerAvailability(is_available=True, version="26.0.0")
    fake_proc = FakePopen(
        stdout_text="Running slow tests...",
        raise_timeout=True,
    )
    mock_popen.return_value = fake_proc

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
@patch("src.sandbox.runner.subprocess.Popen")
def test_runner_bounds_log_output_with_streaming_budget(
    mock_popen: MagicMock, mock_avail: MagicMock, tmp_path: Path
):
    """Verify log output exceeding MAX_OUTPUT_BYTES is safely capped without memory blowup."""
    mock_avail.return_value = DockerAvailability(is_available=True, version="26.0.0")
    huge_stdout = "A" * (MAX_OUTPUT_BYTES + 5000)
    fake_proc = FakePopen(stdout_text=huge_stdout, stderr_text="", returncode=0)
    mock_popen.return_value = fake_proc

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    runner = DockerSandboxRunner()
    res = runner.run_command(command_args=["python", "-c", "print('x')"], working_dir=work_dir)

    assert len(res.stdout) <= MAX_OUTPUT_BYTES + 300
    assert "Output truncated after reaching" in res.stdout
    assert res.is_truncated is True


def test_strip_ansi_sequences():
    """Verify terminal control sequences are stripped from logs."""
    raw = "\x1b[31mError:\x1b[0m \x1b[1mFailed\x1b[22m"
    clean = strip_ansi_sequences(raw)
    assert clean == "Error: Failed"


@patch.object(DockerSandboxRunner, "check_availability")
@patch("src.sandbox.runner.subprocess.Popen")
def test_runner_calls_on_process_started_callback(
    mock_popen: MagicMock, mock_avail: MagicMock, tmp_path: Path
):
    """Verify callback is executed immediately when process is spawned."""
    mock_avail.return_value = DockerAvailability(is_available=True, version="26.0.0")
    fake_proc = FakePopen(stdout_text="OK\n", returncode=0)
    mock_popen.return_value = fake_proc

    callback_data = {}

    def on_start(c_name: str, p_handle: subprocess.Popen) -> None:
        callback_data["name"] = c_name
        callback_data["proc"] = p_handle

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    runner = DockerSandboxRunner()
    res = runner.run_command(
        command_args=["pytest"],
        working_dir=work_dir,
        on_process_started=on_start,
    )
    assert res.is_success is True
    assert "name" in callback_data
    assert callback_data["name"].startswith("dua-sandbox-")
    assert callback_data["proc"] == fake_proc


def test_runner_terminate_terminates_proc_and_container():
    """Verify terminate() kills the active process and runs docker rm -f."""
    runner = DockerSandboxRunner()
    fake_proc = FakePopen(is_active=True)
    c_name = "test-container-to-kill"
    runner._active_procs[c_name] = fake_proc

    with patch("src.sandbox.runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        killed = runner.terminate(c_name)
        assert killed is True
        assert fake_proc.killed is True
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "rm" in cmd
        assert "-f" in cmd
        assert c_name in cmd


# ----------------------------------------------------------------------
# Live Docker Integration Tests (executed when Docker daemon is available)
# ----------------------------------------------------------------------

def _get_live_docker_runner_or_skip() -> DockerSandboxRunner:
    runner = DockerSandboxRunner()
    avail = runner.check_availability()
    if not avail.is_available:
        pytest.skip(f"Live Docker daemon unavailable: {avail.error_message}")
    return runner


def test_live_docker_execution_success(tmp_path: Path):
    """Verify real container execution with pinned image on a live Docker daemon."""
    runner = _get_live_docker_runner_or_skip()
    work_dir = tmp_path / "live_work"
    work_dir.mkdir()
    try:
        os.chmod(work_dir, 0o777)
    except Exception:
        pass
    sample_file = work_dir / "sample.py"
    sample_file.write_text("x = 10 + 20\n")
    try:
        os.chmod(sample_file, 0o666)
    except Exception:
        pass

    res = runner.run_command(
        command_args=["python", "-m", "compileall", "-q", "."],
        working_dir=work_dir,
    )

    assert res.is_runnable is True
    assert res.is_success is True
    assert res.exit_code == 0
    assert res.timed_out is False
    assert res.container_id is not None
    assert res.image_digest is not None
    assert res.image_digest.startswith("sha256:")

    # Verify container was removed after execution
    inspect_proc = subprocess.run(
        ["docker", "inspect", res.container_id],
        capture_output=True,
    )
    assert inspect_proc.returncode != 0


def test_live_docker_execution_failure_exit_code(tmp_path: Path):
    """Verify non-zero exit code and stdout/stderr capture on a live Docker daemon."""
    runner = _get_live_docker_runner_or_skip()
    work_dir = tmp_path / "live_work_fail"
    work_dir.mkdir()

    res = runner.run_command(
        command_args=[
            "python",
            "-c",
            "import sys; print('live fail stdout'); sys.stderr.write('live fail stderr'); sys.exit(7)",
        ],
        working_dir=work_dir,
    )

    assert res.is_runnable is True
    assert res.is_success is False
    assert res.exit_code == 7
    assert "live fail stdout" in res.stdout
    assert "live fail stderr" in res.stderr


def test_live_docker_execution_timeout_and_cleanup(tmp_path: Path):
    """Verify real container timeout kills container and returns exit code 124."""
    runner = _get_live_docker_runner_or_skip()
    work_dir = tmp_path / "live_work_timeout"
    work_dir.mkdir()

    res = runner.run_command(
        command_args=["python", "-c", "import time; time.sleep(15)"],
        working_dir=work_dir,
        timeout=2,
    )

    assert res.timed_out is True
    assert res.exit_code == 124
    assert res.duration_seconds >= 1.5
    assert "timed out after 2s" in str(res.error_message)

    # Verify container was forcefully terminated
    inspect_proc = subprocess.run(
        ["docker", "inspect", res.container_id],
        capture_output=True,
    )
    assert inspect_proc.returncode != 0


def test_live_docker_security_invariants(tmp_path: Path):
    """Verify real kernel-level isolation: non-root user, network disabled, read-only root."""
    runner = _get_live_docker_runner_or_skip()
    work_dir = tmp_path / "live_work_sec"
    work_dir.mkdir()

    # 1. Non-root user (must be 1000)
    user_res = runner.run_command(command_args=["id", "-u"], working_dir=work_dir)
    assert user_res.exit_code == 0
    assert user_res.stdout.strip() == "1000"

    # 2. Network isolation (network=none should fail connecting)
    net_res = runner.run_command(
        command_args=[
            "python",
            "-c",
            "import urllib.request; urllib.request.urlopen('http://1.1.1.1', timeout=1)",
        ],
        working_dir=work_dir,
    )
    assert net_res.exit_code != 0
    assert (
        "Network is unreachable" in net_res.stderr
        or "URLError" in net_res.stderr
        or "timed out" in net_res.stderr
    )

    # 3. Read-only root filesystem (writing to root must fail)
    ro_res = runner.run_command(
        command_args=["touch", "/test_ro_file"],
        working_dir=work_dir,
    )
    assert ro_res.exit_code != 0
    assert "Read-only file system" in ro_res.stderr
