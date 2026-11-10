"""Ephemeral workspace snapshot manager with secret scrubbing and symlink containment."""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Set

from src.models.plan import UpgradeProposalResponse

logger = logging.getLogger(__name__)

# Patterns and names strictly excluded from snapshots to prevent secret leakage
SENSITIVE_NAMES: Set[str] = {
    ".git",
    ".github",
    ".svn",
    ".hg",
    ".env",
    ".aws",
    ".gcp",
    ".azure",
    ".kube",
    ".npmrc",
    ".pypirc",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
}

SENSITIVE_EXTENSIONS: Set[str] = {
    ".pem",
    ".key",
    ".cert",
    ".crt",
    ".pfx",
    ".p12",
}


class SymlinkEscapeError(ValueError):
    """Raised when a workspace file is a symlink pointing outside the workspace root."""
    pass


class IsolatedSnapshot:
    """Represents an isolated, ephemeral workspace snapshot directory."""

    def __init__(self, temp_dir: str, manifest_path: Optional[Path] = None) -> None:
        self.path = Path(temp_dir).resolve()
        self.manifest_path = manifest_path
        self._cleaned = False

    def cleanup(self) -> None:
        """Remove the ephemeral snapshot directory and all contents."""
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


def is_sensitive_entry(name: str) -> bool:
    """Check if file or directory name should be excluded for security."""
    clean = name.strip().lower()
    if clean in SENSITIVE_NAMES:
        return True
    if clean.startswith(".env"):
        return True
    if any(clean.endswith(ext) for ext in SENSITIVE_EXTENSIONS):
        return True
    if any(prefix in clean for prefix in ["id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"]):
        return True
    return False


def copy_sanitized_workspace(source_root: Path, target_dir: Path) -> None:
    """Replicate workspace files into target_dir with strict security filtering.
    
    Invariants:
    1. Sensitive credentials, dotfiles, and virtual environments are excluded.
    2. Symlinks pointing outside source_root are rejected / omitted.
    3. Normal files and directories are copied safely.
    """
    real_source = source_root.resolve()

    for root, dirs, files in os.walk(real_source):
        rel_root = Path(root).relative_to(real_source)

        # Prune sensitive directories in-place so os.walk doesn't descend into them
        dirs[:] = [d for d in dirs if not is_sensitive_entry(d)]

        dest_dir = target_dir / rel_root
        dest_dir.mkdir(parents=True, exist_ok=True)

        for filename in files:
            if is_sensitive_entry(filename):
                continue

            src_file = Path(root) / filename
            dest_file = dest_dir / filename

            # Check for symlink containment
            if src_file.is_symlink():
                try:
                    resolved_target = src_file.resolve()
                    if not str(resolved_target).startswith(str(real_source)):
                        logger.warning(f"Skipping symlink escaping workspace root: {src_file} -> {resolved_target}")
                        continue
                except Exception as e:
                    logger.warning(f"Error resolving symlink {src_file}: {e}")
                    continue

            try:
                shutil.copy2(src_file, dest_file)
            except Exception as exc:
                logger.warning(f"Failed copying {src_file} to snapshot: {exc}")


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
            IsolatedSnapshot instance with ephemeral directory path.
        """
        temp_dir = tempfile.mkdtemp(prefix="dua_sandbox_")
        target_path = Path(temp_dir).resolve()
        manifest_file: Optional[Path] = None

        try:
            # 1. Copy source workspace if provided
            if source_workspace:
                src_path = Path(source_workspace).resolve()
                if src_path.is_dir():
                    copy_sanitized_workspace(src_path, target_path)

            # 2. Overlay proposal content if proposal is provided
            if proposal is not None:
                rel_manifest = proposal.file_path or (
                    "package.json" if proposal.manifest_type == "package.json" else "requirements.txt"
                )
                target_manifest = target_path / Path(rel_manifest).name
                target_manifest.write_text(proposal.proposed_content, encoding="utf-8")
                manifest_file = target_manifest
            elif original_content is not None:
                filename = "package.json" if manifest_type == "package.json" else "requirements.txt"
                target_manifest = target_path / filename
                target_manifest.write_text(original_content, encoding="utf-8")
                manifest_file = target_manifest

            return IsolatedSnapshot(temp_dir=temp_dir, manifest_path=manifest_file)
        except Exception:
            shutil.rmtree(target_path, ignore_errors=True)
            raise
