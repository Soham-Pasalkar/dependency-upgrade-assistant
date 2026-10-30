"""Git acquisition service for cloning repositories into temporary workspaces.

Enforces strict security boundaries:
- Validates GitHub HTTPS URLs.
- Optimizes with shallow clones (--depth 1).
- Prioritizes public repositories.
- Protects private repository tokens (never in Git URLs, never in CLI arguments).
- Zero remote mutation guarantee (read-only operations only).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

# Strict regex matching valid GitHub HTTPS URLs
# Examples:
#   https://github.com/psf/requests
#   https://github.com/psf/requests.git
GITHUB_HTTPS_REGEX = re.compile(
    r"^https://github\.com/([a-zA-Z0-9_\-\.]+)/([a-zA-Z0-9_\-\.]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class GitCloneResult:
    """Outcome of a git clone operation."""
    success: bool
    repo_name: str
    target_dir: Path
    commit_sha: Optional[str] = None
    error_message: Optional[str] = None
    is_authenticated: bool = False


class GitError(Exception):
    """Exception raised when a git operation fails."""
    pass


class GitService:
    """Service class encapsulating git clone and repository operations."""

    def validate_github_url(self, url: str) -> bool:
        return validate_github_url(url)

    def extract_repo_components(self, url: str) -> Optional[Tuple[str, str]]:
        return extract_repo_components(url)

    def normalize_github_url(self, url: str) -> str:
        return normalize_github_url(url)

    def clone_repository(
        self,
        repo_url: str,
        target_dir: Path | str,
        pat: Optional[str] = None,
        depth: int = 1,
        timeout_seconds: int = 60,
    ) -> GitCloneResult:
        res = clone_repository(
            repo_url=repo_url,
            dest_dir=Path(target_dir),
            token=pat,
            depth=depth,
            timeout_seconds=timeout_seconds,
        )
        if not res.success:
            raise GitError(res.error_message or "Git clone failed.")
        return res

    def get_head_commit(self, repo_dir: Path | str) -> Optional[str]:
        p = Path(repo_dir)
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(p),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
            if res.returncode == 0:
                return res.stdout.strip()
        except Exception:
            pass
        return None


def validate_github_url(url: str) -> bool:
    """Validate whether a string is a well-formed GitHub HTTPS repository URL.
    
    Rejects insecure HTTP URLs, SSH endpoints (git@...), local file paths,
    and non-GitHub hostnames.
    """
    if not isinstance(url, str):
        return False
    stripped = url.strip()
    return bool(GITHUB_HTTPS_REGEX.match(stripped))


def extract_repo_components(url: str) -> Optional[Tuple[str, str]]:
    """Extract (owner, repo_name) from a valid GitHub HTTPS URL.
    
    Returns None if URL is invalid.
    """
    if not isinstance(url, str):
        return None
    match = GITHUB_HTTPS_REGEX.match(url.strip())
    if not match:
        return None
    owner, repo = match.groups()
    return owner, repo


def normalize_github_url(url: str) -> str:
    """Normalize a GitHub URL to a canonical https://github.com/owner/repo.git format."""
    components = extract_repo_components(url)
    if not components:
        raise ValueError(f"Invalid GitHub repository URL: {url}")
    owner, repo = components
    return f"https://github.com/{owner}/{repo}.git"


def _create_secure_askpass_script() -> Path:
    """Create a temporary executable script for GIT_ASKPASS.
    
    Streams the credentials from an ephemeral environment variable
    to avoid exposing tokens in CLI arguments or URLs.
    """
    fd, path_str = tempfile.mkstemp(prefix="dua_askpass_", suffix=".py")
    script_path = Path(path_str)
    
    script_content = (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ''\n"
        "if 'username' in prompt:\n"
        "    sys.stdout.write(os.environ.get('DUA_GIT_USERNAME', 'git') + '\\n')\n"
        "else:\n"
        "    sys.stdout.write(os.environ.get('DUA_GIT_TOKEN', '') + '\\n')\n"
    )
    
    with open(fd, "w", encoding="utf-8") as f:
        f.write(script_content)
        
    # Restrict permissions strictly to current user (rwx------ / 0o700)
    os.chmod(script_path, stat.S_IRWXU)
    return script_path


def clone_repository(
    repo_url: str,
    dest_dir: Path,
    token: Optional[str] = None,
    depth: int = 1,
    timeout_seconds: int = 60,
) -> GitCloneResult:
    """Clone a GitHub repository into an isolated local destination directory.
    
    Args:
        repo_url: Valid GitHub HTTPS repository URL.
        dest_dir: Target directory path where repository will be cloned.
        token: Optional GitHub Personal Access Token (for private repos).
               Held strictly in memory; never embedded in URLs or CLI args.
        depth: Shallow clone depth (default 1).
        timeout_seconds: Maximum execution time for git clone.
        
    Returns:
        GitCloneResult dataclass describing operation success, commit SHA, and diagnostics.
    """
    dest_path = Path(dest_dir).resolve()
    
    if not validate_github_url(repo_url):
        return GitCloneResult(
            success=False,
            repo_name="unknown",
            target_dir=dest_path,
            error_message=(
                f"Invalid GitHub repository URL: '{repo_url}'. "
                "Expected format: https://github.com/owner/repository"
            ),
        )

    owner, repo_name = extract_repo_components(repo_url) or ("unknown", "unknown")
    canonical_url = normalize_github_url(repo_url)

    # Ensure parent directory exists
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    
    # If destination exists and is not empty, clone would fail
    if dest_path.exists() and any(dest_path.iterdir()):
        return GitCloneResult(
            success=False,
            repo_name=repo_name,
            target_dir=dest_path,
            error_message=f"Destination directory already exists and is not empty: {dest_path}",
        )

    # Prepare command without embedding tokens in args or URL
    cmd = [
        "git",
        "clone",
        "--depth",
        str(max(1, depth)),
        "--single-branch",
        canonical_url,
        str(dest_path),
    ]

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    askpass_script: Optional[Path] = None
    is_authenticated = bool(token and token.strip())

    try:
        if is_authenticated and token is not None:
            askpass_script = _create_secure_askpass_script()
            env["GIT_ASKPASS"] = str(askpass_script)
            env["DUA_GIT_TOKEN"] = token.strip()
            env["DUA_GIT_USERNAME"] = "x-access-token"

        proc = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or "Unknown git clone error."
            # Sanitize stderr to ensure no sensitive path or token leaks
            if token and token in err:
                err = err.replace(token, "[REDACTED_TOKEN]")
            return GitCloneResult(
                success=False,
                repo_name=repo_name,
                target_dir=dest_path,
                error_message=f"Git clone failed (exit code {proc.returncode}): {err}",
                is_authenticated=is_authenticated,
            )

        # Retrieve commit SHA
        commit_sha: Optional[str] = None
        rev_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(dest_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if rev_proc.returncode == 0:
            commit_sha = rev_proc.stdout.strip()

        return GitCloneResult(
            success=True,
            repo_name=repo_name,
            target_dir=dest_path,
            commit_sha=commit_sha,
            is_authenticated=is_authenticated,
        )

    except subprocess.TimeoutExpired:
        return GitCloneResult(
            success=False,
            repo_name=repo_name,
            target_dir=dest_path,
            error_message=f"Git clone timed out after {timeout_seconds} seconds.",
            is_authenticated=is_authenticated,
        )
    except Exception as exc:
        return GitCloneResult(
            success=False,
            repo_name=repo_name,
            target_dir=dest_path,
            error_message=f"Unexpected error during git clone: {exc}",
            is_authenticated=is_authenticated,
        )
    finally:
        # Wipe askpass script and credentials from memory
        if askpass_script and askpass_script.exists():
            try:
                askpass_script.unlink()
            except OSError:
                pass
        env.pop("DUA_GIT_TOKEN", None)
        env.pop("DUA_GIT_USERNAME", None)

