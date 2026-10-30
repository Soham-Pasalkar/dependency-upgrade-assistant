"""Unit tests for Git acquisition service and secure credential handling."""
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.services.git_service import (
    GitCloneResult,
    clone_repository,
    extract_repo_components,
    normalize_github_url,
    validate_github_url,
)


def test_validate_github_url_valid():
    """Verify acceptance of valid GitHub HTTPS URLs."""
    valid_urls = [
        "https://github.com/psf/requests",
        "https://github.com/psf/requests.git",
        "https://github.com/psf/requests/",
        "https://github.com/owner-name/repo_name.git",
        "https://github.com/owner.123/repo-123.git",
    ]
    for url in valid_urls:
        assert validate_github_url(url) is True, f"Expected {url} to be valid"


def test_validate_github_url_invalid():
    """Verify rejection of invalid, insecure, or malicious URLs."""
    invalid_urls = [
        "http://github.com/psf/requests",               # Insecure HTTP
        "git@github.com:psf/requests.git",              # SSH format
        "https://gitlab.com/psf/requests.git",          # Non-GitHub domain
        "https://github.com/psf/requests/issues/1",     # Subpath
        "https://github.com/psf",                       # Missing repo
        "https://github.com/",                          # Missing owner and repo
        "https://malicious.com/fake/github.com/repo",   # Phishing domain
        "",                                             # Empty string
        "   ",                                          # Whitespace
        None,                                           # Non-string
        "https://github.com/owner/repo; rm -rf /",       # Injection attempt
    ]
    for url in invalid_urls:
        assert validate_github_url(url) is False, f"Expected {url} to be invalid"


def test_extract_repo_components():
    """Verify extraction of owner and repository name."""
    assert extract_repo_components("https://github.com/pallets/flask") == ("pallets", "flask")
    assert extract_repo_components("https://github.com/pallets/flask.git") == ("pallets", "flask")
    assert extract_repo_components("https://gitlab.com/pallets/flask") is None


def test_normalize_github_url():
    """Verify canonical URL formatting."""
    assert normalize_github_url("https://github.com/pallets/flask") == "https://github.com/pallets/flask.git"
    assert normalize_github_url("https://github.com/pallets/flask.git") == "https://github.com/pallets/flask.git"
    
    with pytest.raises(ValueError, match="Invalid GitHub repository URL"):
        normalize_github_url("https://invalid.com/not/github")


def test_clone_repository_invalid_url_fails_early(tmp_path: Path):
    """Verify early failure without invoking git when URL is malformed."""
    dest = tmp_path / "cloned_repo"
    result = clone_repository("invalid_url", dest)
    
    assert result.success is False
    assert "Invalid GitHub repository URL" in str(result.error_message)
    assert not dest.exists()


def test_clone_repository_destination_not_empty(tmp_path: Path):
    """Verify clone rejection if destination directory exists and contains files."""
    dest = tmp_path / "existing_repo"
    dest.mkdir(parents=True)
    (dest / "some_file.txt").write_text("pre-existing content", encoding="utf-8")

    result = clone_repository("https://github.com/psf/requests", dest)
    assert result.success is False
    assert "already exists and is not empty" in str(result.error_message)


@patch("src.services.git_service.subprocess.run")
def test_clone_repository_public_args(mock_run: MagicMock, tmp_path: Path):
    """Verify that public clones invoke shallow clone without credentials."""
    dest = tmp_path / "target_repo"

    # Mock subprocess.run for git clone and git rev-parse
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout="Cloning into target...", stderr=""),  # clone
        MagicMock(returncode=0, stdout="abc123commitsha\n", stderr=""),         # rev-parse
    ]

    result = clone_repository("https://github.com/psf/requests", dest, depth=1)

    assert result.success is True
    assert result.repo_name == "requests"
    assert result.commit_sha == "abc123commitsha"
    assert result.is_authenticated is False

    # Inspect git clone call arguments
    clone_call_args = mock_run.call_args_list[0]
    cmd = clone_call_args[0][0]
    env = clone_call_args[1]["env"]

    assert cmd == [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "https://github.com/psf/requests.git",
        str(dest.resolve()),
    ]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_ASKPASS" not in env
    assert "DUA_GIT_TOKEN" not in env


@patch("src.services.git_service.subprocess.run")
def test_clone_repository_secure_credential_handling(mock_run: MagicMock, tmp_path: Path):
    """CRITICAL SECURITY TEST: Verify PAT is NEVER in CLI args or URLs and askpass script is cleaned up."""
    dest = tmp_path / "private_repo"
    secret_pat = "ghp_SecretTokenXYZ1234567890"

    askpass_captured_path = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        env = kwargs.get("env", {})
        if "GIT_ASKPASS" in env:
            askpass_captured_path.append(Path(env["GIT_ASKPASS"]))
        # Assert token is NEVER in command line arguments
        cmd_str = " ".join(cmd)
        assert secret_pat not in cmd_str, f"Token leaked in CLI command: {cmd_str}"
        # Assert token is NEVER in the repository URL
        for arg in cmd:
            if "github.com" in arg:
                assert "@" not in arg, f"Credentials embedded in URL: {arg}"
        
        if cmd[1] == "clone":
            return MagicMock(returncode=0, stdout="Cloned successfully", stderr="")
        if cmd[1] == "rev-parse":
            return MagicMock(returncode=0, stdout="999888sha\n", stderr="")
        return MagicMock(returncode=0)

    mock_run.side_effect = fake_subprocess_run

    result = clone_repository(
        "https://github.com/my-org/private-repo",
        dest,
        token=secret_pat,
        depth=1,
    )

    assert result.success is True
    assert result.is_authenticated is True
    assert result.commit_sha == "999888sha"

    # Verify that the temporary askpass script was cleaned up immediately
    assert len(askpass_captured_path) == 1
    askpass_file = askpass_captured_path[0]
    assert not askpass_file.exists(), f"Askpass script was not unlinked: {askpass_file}"


@patch("src.services.git_service.subprocess.run")
def test_clone_repository_failure_sanitizes_error(mock_run: MagicMock, tmp_path: Path):
    """Verify that failure messages sanitize any accidental token appearances in stderr."""
    dest = tmp_path / "failed_repo"
    secret_pat = "ghp_VerySensitiveTokenABC"

    mock_run.return_value = MagicMock(
        returncode=128,
        stdout="",
        stderr=f"fatal: Authentication failed for token {secret_pat} on remote repository.",
    )

    result = clone_repository(
        "https://github.com/private-org/repo",
        dest,
        token=secret_pat,
    )

    assert result.success is False
    assert secret_pat not in str(result.error_message)
    assert "[REDACTED_TOKEN]" in str(result.error_message)


@patch("src.services.git_service.subprocess.run")
def test_clone_repository_timeout(mock_run: MagicMock, tmp_path: Path):
    """Verify graceful handling when git clone exceeds timeout."""
    dest = tmp_path / "slow_repo"
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="git clone", timeout=5)

    result = clone_repository("https://github.com/large/repo", dest, timeout_seconds=5)
    assert result.success is False
    assert "timed out after 5 seconds" in str(result.error_message)

