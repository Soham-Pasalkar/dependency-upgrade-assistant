"""Ephemeral workspace snapshot manager with strict secret scrubbing, symlink containment, and manifest tracking."""
from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set

from src.models.plan import UpgradeProposalResponse

logger = logging.getLogger(__name__)


class SecurityException(Exception):
    """Raised when a workspace violates containment or contains prohibited special files."""
    pass


class SnapshotCreationError(RuntimeError):
    """Raised when snapshot replication or manifest overlay fails."""
    pass


# Explicit directory and file names to exclude for security
SENSITIVE_DIR_NAMES: Set[str] = {
    ".git",
    ".github",
    ".gitlab",
    ".svn",
    ".hg",
    ".ssh",
    ".aws",
    ".azure",
    ".gcp",
    ".kube",
    ".docker",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".mypy_cache",
    ".idea",
    ".vscode",
}

SENSITIVE_FILE_NAMES: Set[str] = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
}

SENSITIVE_EXTENSIONS: Set[str] = {
    ".pem",
    ".key",
    ".cert",
    ".crt",
    ".pfx",
    ".p12",
    ".kdbx",
    ".ovpn",
}

# Permitted dotfiles essential for builds and tests
PERMITTED_DOTFILES: Set[str] = {
    ".ruff.toml",
    ".pytest.ini",
    ".eslintrc",
    ".eslintrc.json",
    ".eslintrc.js",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    ".prettierrc",
    ".prettierrc.json",
    ".babelrc",
    ".npmignore",
    ".python-version",
    ".editorconfig",
}


@dataclass
class SnapshotManifest:
    """Audit record of files included and excluded during snapshot creation (no secret contents)."""
    included_files_count: int = 0
    excluded_files_count: int = 0
    included_relative_paths: List[str] = field(default_factory=list)
    excluded_categories: List[str] = field(default_factory=list)
    has_proposal_overlay: bool = False
    overlay_target: Optional[str] = None


class IsolatedSnapshot:
    """Represents an isolated, ephemeral workspace snapshot directory."""

    def __init__(
        self,
        temp_dir: str,
        manifest: SnapshotManifest,
        manifest_path: Optional[Path] = None,
    ) -> None:
        self.path = Path(temp_dir).resolve()
        self.manifest = manifest
        self.manifest_path = manifest_path
        self._cleaned = False

    def cleanup(self) -> None:
        """Safely remove the ephemeral snapshot directory and all contents."""
        if not self._cleaned and self.path.exists():
            try:
                shutil.rmtree(self.path, ignore_errors=True)
            except Exception as exc:
                logger.warning(f"Error cleaning up snapshot directory {self.path}: {exc}")
            finally:
                self._cleaned = True

    def __enter__(self) -> IsolatedSnapshot:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()


def is_sensitive_entry(name: str, is_dir: bool = False) -> tuple[bool, str]:
    """Check if file or directory name should be excluded for security.
    
    Returns:
        (is_sensitive: bool, category_reason: str)
    """
    clean = name.strip().lower()

    # Direct match on directories
    if is_dir:
        if clean in SENSITIVE_DIR_NAMES:
            return True, f"sensitive_directory:{clean}"
        if clean.startswith(".env"):
            return True, "env_directory"
        return False, ""

    # Allow explicit safe dotfiles
    if clean in PERMITTED_DOTFILES:
        return False, ""

    # Sensitive filenames
    if clean in SENSITIVE_FILE_NAMES:
        return True, f"credential_file:{clean}"

    # .env variations
    if clean.startswith(".env.") or clean.endswith(".env"):
        return True, "environment_secrets_file"

    # Certificate & key extensions
    if any(clean.endswith(ext) for ext in SENSITIVE_EXTENSIONS):
        return True, "cryptographic_key_or_cert"

    # SSH key names
    if any(clean.startswith(prefix) for prefix in ["id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"]):
        return True, "ssh_private_key"

    # Token and secret naming heuristics
    if any(token_pattern in clean for token_pattern in ["token", "secret", "credential", "password", "passwd"]):
        # Disallow files named *secret*, *token*, etc.
        return True, "secret_or_token_named_file"

    return False, ""


def copy_sanitized_workspace(
    source_root: Path,
    target_dir: Path,
    manifest: SnapshotManifest,
) -> None:
    """Replicate workspace files into target_dir with strict security filtering.
    
    Invariants:
    1. Sensitive credentials, VCS metadata, and private keys are strictly excluded.
    2. Symlinks pointing outside source_root are rejected.
    3. Special files (FIFOs, sockets, device nodes) raise SecurityException.
    4. Copy failures fail closed with SnapshotCreationError (never silently ignored).
    5. SnapshotManifest records counts and sanitized relative paths without leaking secret contents.
    """
    real_source = source_root.resolve()

    for root, dirs, files in os.walk(real_source):
        rel_root = Path(root).relative_to(real_source)

        # Check and filter directories in-place
        retained_dirs = []
        for d in dirs:
            dir_path = Path(root) / d
            # Reject symlinked directories escaping root
            if dir_path.is_symlink():
                try:
                    resolved_dir = dir_path.resolve()
                    if resolved_dir != real_source and real_source not in resolved_dir.parents:
                        manifest.excluded_files_count += 1
                        manifest.excluded_categories.append("escaping_symlink_directory")
                        continue
                except Exception:
                    manifest.excluded_files_count += 1
                    continue

            sensitive, reason = is_sensitive_entry(d, is_dir=True)
            if sensitive:
                manifest.excluded_files_count += 1
                if reason not in manifest.excluded_categories:
                    manifest.excluded_categories.append(reason)
            else:
                retained_dirs.append(d)

        dirs[:] = retained_dirs

        dest_dir = target_dir / rel_root
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(dest_dir, 0o777)
        except Exception:
            pass

        for filename in files:
            src_file = Path(root) / filename
            dest_file = dest_dir / filename

            # 1. Reject special files (sockets, FIFOs, devices)
            try:
                file_stat = os.lstat(src_file)
                mode = file_stat.st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode) or stat.S_ISDIR(mode)):
                    raise SecurityException(
                        f"Prohibited special filesystem object detected in workspace: {src_file.name} (mode {oct(mode)})"
                    )
            except OSError as e:
                raise SnapshotCreationError(f"Failed to inspect file status for {src_file.name}: {e}") from e

            # 2. Check sensitivity
            sensitive, reason = is_sensitive_entry(filename, is_dir=False)
            if sensitive:
                manifest.excluded_files_count += 1
                if reason not in manifest.excluded_categories:
                    manifest.excluded_categories.append(reason)
                continue

            # 3. Check for symlink containment
            if src_file.is_symlink():
                try:
                    resolved_target = src_file.resolve()
                    if resolved_target != real_source and real_source not in resolved_target.parents:
                        manifest.excluded_files_count += 1
                        manifest.excluded_categories.append("escaping_symlink_file")
                        continue
                except Exception:
                    manifest.excluded_files_count += 1
                    continue

            # 4. Copy regular file safely (Fail-Closed)
            try:
                shutil.copy2(src_file, dest_file)
                manifest.included_files_count += 1
                rel_path = str((rel_root / filename)).replace("\\", "/")
                manifest.included_relative_paths.append(rel_path)
            except Exception as exc:
                raise SnapshotCreationError(
                    f"Failed copying workspace file '{filename}' into snapshot: {exc}"
                ) from exc


class SnapshotManager:
    """Factory for creating ephemeral, sanitized workspace snapshots."""

    @staticmethod
    def create_snapshot(
        source_workspace: Optional[Path | str] = None,
        proposal: Optional[UpgradeProposalResponse] = None,
        manifest_type: Optional[str] = None,
        original_content: Optional[str] = None,
    ) -> IsolatedSnapshot:
        """Create an ephemeral isolated snapshot for sandboxed validation.

        Args:
            source_workspace: Optional existing workspace directory to copy and sanitize.
            proposal: Optional upgrade proposal to apply into the snapshot.
            manifest_type: Optional manifest type (e.g. 'requirements.txt' or 'package.json').
            original_content: Optional raw manifest content if no proposal exists.

        Returns:
            IsolatedSnapshot instance with ephemeral directory path and SnapshotManifest.
        """
        temp_dir = tempfile.mkdtemp(prefix="dua_sandbox_")
        target_path = Path(temp_dir).resolve()
        try:
            os.chmod(target_path, 0o777)
        except Exception:
            pass
        manifest = SnapshotManifest()
        manifest_file: Optional[Path] = None

        try:
            # 1. Copy source workspace if provided
            if source_workspace:
                src_path = Path(source_workspace).resolve()
                if not src_path.is_dir():
                    raise SnapshotCreationError(f"Source workspace does not exist or is not a directory: {src_path}")
                copy_sanitized_workspace(src_path, target_path, manifest)

            # 2. Overlay proposal content if proposal is provided
            if proposal is not None:
                rel_manifest = proposal.file_path or (
                    "package.json" if proposal.manifest_type == "package.json" else "requirements.txt"
                )
                target_manifest = target_path / Path(rel_manifest).name
                target_manifest.write_text(proposal.proposed_content, encoding="utf-8")
                manifest_file = target_manifest
                manifest.has_proposal_overlay = True
                manifest.overlay_target = target_manifest.name
            elif original_content is not None:
                filename = "package.json" if manifest_type == "package.json" else "requirements.txt"
                target_manifest = target_path / filename
                target_manifest.write_text(original_content, encoding="utf-8")
                manifest_file = target_manifest
                manifest.has_proposal_overlay = False
                manifest.overlay_target = filename

            return IsolatedSnapshot(
                temp_dir=temp_dir,
                manifest=manifest,
                manifest_path=manifest_file,
            )
        except Exception:
            shutil.rmtree(target_path, ignore_errors=True)
            raise
