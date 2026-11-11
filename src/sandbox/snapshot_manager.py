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
from src.sandbox.container_config import (
    DEFAULT_NON_ROOT_USER,
    get_platform_execution_user,
    is_root_user,
)

logger = logging.getLogger(__name__)


class SecurityException(Exception):
    """Raised when a workspace violates containment or contains prohibited special files."""
    pass


class SnapshotCreationError(RuntimeError):
    """Raised when snapshot replication or manifest overlay fails."""
    pass


class SnapshotCleanupError(RuntimeError):
    """Raised when an ephemeral snapshot directory fails to be completely removed from disk."""
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


def parse_user_spec(user_spec: Optional[str] = None) -> tuple[int, int]:
    """Parse a user specification string into numerical (uid, gid).

    Security Invariants:
    1. If user_spec is None or empty/whitespace, resolves the platform's non-root
       execution identity (or DEFAULT_NON_ROOT_USER).
    2. Explicit specifications must be valid non-negative integer 'uid' or 'uid:gid'.
    3. Root container execution (UID 0 or user 'root') is strictly forbidden and raises ValueError.
    4. Non-numeric or malformed specifications fail closed with ValueError (never silent default fallback).
    """
    if user_spec is None or not str(user_spec).strip():
        resolved = get_platform_execution_user()
        parts = resolved.split(":")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else int(parts[0])

    spec_str = str(user_spec).strip()
    if is_root_user(spec_str):
        raise ValueError(
            f"Prohibited container user '{spec_str}': Root container execution (UID 0) is strictly forbidden."
        )

    parts = spec_str.split(":")
    if len(parts) > 2:
        raise ValueError(f"Malformed user specification '{spec_str}': expected 'uid' or 'uid:gid'.")

    try:
        uid = int(parts[0])
        gid = int(parts[1]) if len(parts) == 2 else uid
    except ValueError as exc:
        raise ValueError(
            f"Invalid non-numeric user specification '{spec_str}': UID and GID must be integers."
        ) from exc

    if uid < 0 or gid < 0:
        raise ValueError(f"Invalid negative user specification '{spec_str}': UID and GID cannot be negative.")

    if uid == 0 or gid == 0:
        raise ValueError(f"Prohibited container user '{spec_str}': UID 0 is strictly forbidden.")

    return uid, gid


def apply_secure_directory_permissions(
    dir_path: Path,
    container_user: Optional[str] = None,
) -> None:
    """Apply strict 0700 permissions and assign non-root container ownership if running as root.

    Raises:
        SnapshotCreationError: If permissions cannot be set or ownership cannot be assigned.
    """
    try:
        os.chmod(dir_path, stat.S_IRWXU)
    except OSError as exc:
        raise SnapshotCreationError(
            f"Failed applying secure directory permissions (0700) on '{dir_path}': {exc}"
        ) from exc

    if hasattr(os, "getuid") and os.getuid() == 0:
        uid, gid = parse_user_spec(container_user)
        try:
            os.chown(dir_path, uid, gid)
        except OSError as exc:
            raise SnapshotCreationError(
                f"Root host fallback failed assigning container ownership ({uid}:{gid}) to directory '{dir_path}': {exc}"
            ) from exc


def apply_secure_file_permissions(
    file_path: Path,
    container_user: Optional[str] = None,
) -> None:
    """Harden file permissions (strip group/world write) and assign non-root ownership if running as root.

    Raises:
        SnapshotCreationError: If permissions cannot be set or ownership cannot be assigned.
    """
    try:
        st = file_path.stat()
        secure_mode = (st.st_mode | stat.S_IRUSR | stat.S_IWUSR) & ~(stat.S_IWGRP | stat.S_IWOTH)
        os.chmod(file_path, secure_mode)
    except OSError as exc:
        raise SnapshotCreationError(
            f"Failed applying secure file permissions on '{file_path}': {exc}"
        ) from exc

    if hasattr(os, "getuid") and os.getuid() == 0:
        uid, gid = parse_user_spec(container_user)
        try:
            os.chown(file_path, uid, gid)
        except OSError as exc:
            raise SnapshotCreationError(
                f"Root host fallback failed assigning container ownership ({uid}:{gid}) to file '{file_path}': {exc}"
            ) from exc


def verify_snapshot_security_invariants(
    snapshot_dir: Path,
    container_user: Optional[str] = None,
) -> None:
    """Recursively verify that a newly created snapshot satisfies all security invariants:
    1. Root directory exists and is mode 0700 (owner-only permissions).
    2. All nested directories have mode 0700 (owner-only, zero group/other permissions).
    3. Zero world-writable or group-writable bits on any file (st_mode & 0o022 == 0).
    4. All files are readable by their owner.
    5. All files and directories have expected non-root container ownership (on POSIX).
    6. Root snapshot directory matches configured container execution identity.
    7. Symlinks do not escape the snapshot root.
    8. Prohibited special filesystem objects (FIFOs, sockets, device nodes) raise SnapshotCreationError.

    Raises:
        SnapshotCreationError: If any directory, file, or descendant violates security invariants.
    """
    target_root = Path(snapshot_dir).resolve()
    if not target_root.exists() or not target_root.is_dir():
        raise SnapshotCreationError(f"Snapshot directory '{target_root}' does not exist or is not a directory.")

    if not validate_snapshot_permissions(target_root):
        raise SnapshotCreationError(
            f"Snapshot root directory '{target_root}' failed permission validation: directory must be mode 0700."
        )

    # Determine expected UID/GID
    expected_uid: Optional[int] = None
    if hasattr(os, "getuid"):
        if os.getuid() == 0:
            expected_uid, _ = parse_user_spec(container_user)
        else:
            expected_uid = os.getuid()

        try:
            st = target_root.stat()
            if st.st_uid != expected_uid:
                raise SnapshotCreationError(
                    f"Snapshot directory ownership mismatch on '{target_root}': "
                    f"owned by UID {st.st_uid}, expected non-root container identity UID {expected_uid}."
                )
        except OSError as exc:
            raise SnapshotCreationError(
                f"Failed verifying ownership on snapshot root '{target_root}': {exc}"
            ) from exc

    # Recursively verify all descendants
    for root, dirs, files in os.walk(target_root):
        for d in dirs:
            dir_path = Path(root) / d

            # Symlink check
            if dir_path.is_symlink():
                try:
                    resolved_target = dir_path.resolve()
                    if resolved_target != target_root and target_root not in resolved_target.parents:
                        raise SnapshotCreationError(
                            f"Snapshot directory symlink '{dir_path}' escapes snapshot root: points to '{resolved_target}'."
                        )
                except OSError as exc:
                    raise SnapshotCreationError(
                        f"Failed inspecting symlink '{dir_path}': {exc}"
                    ) from exc

            # Directory permissions check (must be mode 0700)
            if not validate_snapshot_permissions(dir_path):
                raise SnapshotCreationError(
                    f"Snapshot nested directory '{dir_path.name}' violates permissions: directory must be mode 0700."
                )

            # Directory ownership check
            if expected_uid is not None:
                try:
                    d_stat = dir_path.stat()
                    if d_stat.st_uid != expected_uid:
                        raise SnapshotCreationError(
                            f"Snapshot nested directory ownership mismatch on '{dir_path.name}': "
                            f"owned by UID {d_stat.st_uid}, expected UID {expected_uid}."
                        )
                except OSError as exc:
                    raise SnapshotCreationError(
                        f"Failed inspecting directory status on '{dir_path}': {exc}"
                    ) from exc

        for f in files:
            file_path = Path(root) / f

            # Reject special files (FIFOs, sockets, character/block devices)
            try:
                l_stat = os.lstat(file_path)
                mode = l_stat.st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                    raise SnapshotCreationError(
                        f"Prohibited special filesystem object detected in snapshot: '{file_path.name}' (mode {oct(mode)})."
                    )
            except OSError as exc:
                raise SnapshotCreationError(
                    f"Failed inspecting file status on '{file_path}': {exc}"
                ) from exc

            # Symlink containment check
            if file_path.is_symlink():
                try:
                    resolved_file = file_path.resolve()
                    if resolved_file != target_root and target_root not in resolved_file.parents:
                        raise SnapshotCreationError(
                            f"Snapshot file symlink '{file_path}' escapes snapshot root: points to '{resolved_file}'."
                        )
                except OSError as exc:
                    raise SnapshotCreationError(
                        f"Failed inspecting symlink target on '{file_path}': {exc}"
                    ) from exc

            # File permissions check
            try:
                f_stat = file_path.stat()
                # Prohibit group or world write bits
                if (f_stat.st_mode & 0o022) != 0:
                    raise SnapshotCreationError(
                        f"Snapshot file '{file_path.name}' has prohibited group or world write permissions (mode {oct(f_stat.st_mode)})."
                    )
                # File must be readable by owner
                if (f_stat.st_mode & stat.S_IRUSR) == 0:
                    raise SnapshotCreationError(
                        f"Snapshot file '{file_path.name}' is not readable by owner (mode {oct(f_stat.st_mode)})."
                    )

                # File ownership check
                if expected_uid is not None:
                    if f_stat.st_uid != expected_uid:
                        raise SnapshotCreationError(
                            f"Snapshot file ownership mismatch on '{file_path.name}': "
                            f"owned by UID {f_stat.st_uid}, expected UID {expected_uid}."
                        )
            except OSError as exc:
                raise SnapshotCreationError(
                    f"Failed inspecting file status on '{file_path}': {exc}"
                ) from exc


def validate_snapshot_permissions(path: Path | str) -> bool:
    """Verify that a snapshot directory enforces strict isolation:
    - Mode is NOT world-writable (st_mode & 0o002 == 0).
    - Permissions for 'others' (world) are 0 (st_mode & 0o007 == 0).
    - Permissions for 'group' are 0 (st_mode & 0o070 == 0).
    - Mode is owner-accessible (0o700).
    """
    target = Path(path).resolve()
    if not target.exists() or not target.is_dir():
        return False
    try:
        st = target.stat()
        mode = st.st_mode
        if (mode & 0o002) != 0:
            return False
        if (mode & 0o007) != 0:
            return False
        if (mode & 0o070) != 0:
            return False
        return (mode & 0o700) == 0o700
    except OSError:
        return False


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
        """Safely remove the ephemeral snapshot directory and all contents.

        Security and Reliability Invariants:
        1. Restores owner write and execute permissions on all subdirectories and files
           before deletion to prevent read-only test artifacts from blocking removal.
        2. Handles ownership correctly for root-host and non-root execution.
        3. Never silently suppresses permission or deletion failures.
        4. Verifies the snapshot directory no longer exists on disk.
        5. Sets self._cleaned = True ONLY upon verified complete removal.
        6. On failure, raises SnapshotCleanupError and preserves state for retry.
        """
        if self._cleaned:
            return

        if not self.path.exists():
            self._cleaned = True
            return

        # 1. Restore permissions bottom-up
        try:
            for root, dirs, files in os.walk(self.path, topdown=False):
                for f in files:
                    file_path = os.path.join(root, f)
                    try:
                        os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR)
                    except OSError as exc:
                        raise SnapshotCleanupError(
                            f"Failed restoring file permissions for cleanup on '{file_path}': {exc}"
                        ) from exc

                for d in dirs:
                    dir_path = os.path.join(root, d)
                    try:
                        os.chmod(dir_path, stat.S_IRWXU)
                    except OSError as exc:
                        raise SnapshotCleanupError(
                            f"Failed restoring directory permissions for cleanup on '{dir_path}': {exc}"
                        ) from exc

            # Restore root directory permissions
            try:
                os.chmod(self.path, stat.S_IRWXU)
            except OSError as exc:
                raise SnapshotCleanupError(
                    f"Failed restoring snapshot root permissions for cleanup on '{self.path}': {exc}"
                ) from exc

        except SnapshotCleanupError:
            raise
        except Exception as exc:
            raise SnapshotCleanupError(
                f"Unexpected error while preparing snapshot directory '{self.path}' for cleanup: {exc}"
            ) from exc

        # 2. Perform removal (ignore_errors=False)
        try:
            shutil.rmtree(self.path, ignore_errors=False)
        except Exception as exc:
            raise SnapshotCleanupError(
                f"Failed deleting snapshot directory '{self.path}': {exc}"
            ) from exc

        # 3. Verify directory no longer exists
        if self.path.exists():
            raise SnapshotCleanupError(
                f"Snapshot directory '{self.path}' still exists after deletion attempt."
            )

        # 4. Only mark complete after verified removal
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
    container_user: Optional[str] = None,
) -> None:
    """Replicate workspace files into target_dir with strict security filtering.
    
    Invariants:
    1. Sensitive credentials, VCS metadata, and private keys are strictly excluded.
    2. Symlinks pointing outside source_root are rejected.
    3. Special files (FIFOs, sockets, device nodes) raise SecurityException.
    4. Copy failures fail closed with SnapshotCreationError (never silently ignored).
    5. SnapshotManifest records counts and sanitized relative paths without leaking secret contents.
    6. All copied directories and files have secure permissions and ownership assigned fail-closed.
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
        apply_secure_directory_permissions(dest_dir, container_user)

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
                apply_secure_file_permissions(dest_file, container_user)
                manifest.included_files_count += 1
                rel_path = str((rel_root / filename)).replace("\\", "/")
                manifest.included_relative_paths.append(rel_path)
            except SnapshotCreationError:
                raise
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
        container_user: Optional[str] = None,
    ) -> IsolatedSnapshot:
        """Create an ephemeral isolated snapshot for sandboxed validation.

        Args:
            source_workspace: Optional existing workspace directory to copy and sanitize.
            proposal: Optional upgrade proposal to apply into the snapshot.
            manifest_type: Optional manifest type (e.g. 'requirements.txt' or 'package.json').
            original_content: Optional raw manifest content if no proposal exists.
            container_user: Optional container execution user identity (e.g. "1000:1000").

        Returns:
            IsolatedSnapshot instance with ephemeral directory path and SnapshotManifest.
        """
        temp_dir = tempfile.mkdtemp(prefix="dua_sandbox_")
        target_path = Path(temp_dir).resolve()
        manifest = SnapshotManifest()
        manifest_file: Optional[Path] = None

        try:
            apply_secure_directory_permissions(target_path, container_user)

            # 1. Copy source workspace if provided
            if source_workspace:
                src_path = Path(source_workspace).resolve()
                if not src_path.is_dir():
                    raise SnapshotCreationError(f"Source workspace does not exist or is not a directory: {src_path}")
                copy_sanitized_workspace(src_path, target_path, manifest, container_user=container_user)

            # 2. Overlay proposal content if proposal is provided
            if proposal is not None:
                rel_manifest = proposal.file_path or (
                    "package.json" if proposal.manifest_type == "package.json" else "requirements.txt"
                )
                target_manifest = target_path / Path(rel_manifest).name
                target_manifest.write_text(proposal.proposed_content, encoding="utf-8")
                apply_secure_file_permissions(target_manifest, container_user)
                manifest_file = target_manifest
                manifest.has_proposal_overlay = True
                manifest.overlay_target = target_manifest.name
            elif original_content is not None:
                filename = "package.json" if manifest_type == "package.json" else "requirements.txt"
                target_manifest = target_path / filename
                target_manifest.write_text(original_content, encoding="utf-8")
                apply_secure_file_permissions(target_manifest, container_user)
                manifest_file = target_manifest
                manifest.has_proposal_overlay = False
                manifest.overlay_target = filename

            # 3. Post-creation verification: verify snapshot permissions and ownership
            verify_snapshot_security_invariants(target_path, container_user)

            return IsolatedSnapshot(
                temp_dir=temp_dir,
                manifest=manifest,
                manifest_path=manifest_file,
            )
        except Exception:
            try:
                if target_path.exists():
                    for root, dirs, files in os.walk(target_path, topdown=False):
                        for f in files:
                            try:
                                os.chmod(os.path.join(root, f), stat.S_IRUSR | stat.S_IWUSR)
                            except OSError:
                                pass
                        for d in dirs:
                            try:
                                os.chmod(os.path.join(root, d), stat.S_IRWXU)
                            except OSError:
                                pass
                    try:
                        os.chmod(target_path, stat.S_IRWXU)
                    except OSError:
                        pass
                    shutil.rmtree(target_path, ignore_errors=True)
            except Exception:
                pass
            raise
