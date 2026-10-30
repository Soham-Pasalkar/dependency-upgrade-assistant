"""Workspace Manager for temporary isolated dual-copy repository environments.

Maintains two distinct filesystem trees:
- reference/: Pristine, untouched repository clone (read-only ground truth).
- working/: Independent mutable copy where dependency bumps and AI improvements
            are applied and validated.

Guarantees full isolation and clean teardown via context manager or explicit cleanup.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional, Set

DEFAULT_EXCLUDED_DIRS: Set[str] = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".tox",
    "egg-info",
}


class WorkspaceSession:
    """Manages the lifecycle of a dual-copy temporary repository workspace."""

    def __init__(self, base_temp_dir: Optional[Path | str] = None) -> None:
        self.session_id = f"ws_{uuid.uuid4().hex[:12]}"
        base = Path(base_temp_dir) if base_temp_dir else None
        if base:
            base.mkdir(parents=True, exist_ok=True)
        
        self.root_dir = Path(
            tempfile.mkdtemp(prefix=f"dua_{self.session_id}_", dir=base)
        ).resolve()
        
        self.reference_dir = self.root_dir / "reference"
        self.working_dir = self.root_dir / "working"
        self._is_active = True

        # Initialize reference directory skeleton
        self.reference_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_active(self) -> bool:
        """Check whether the workspace is currently active."""
        return self._is_active and self.root_dir.exists()

    def prepare_working_copy(self) -> Path:
        """Create or refresh the mutable working/ directory from the pristine reference/ copy.
        
        Performs a full deep copy of reference/ to working/.
        Raises FileNotFoundError if reference/ does not contain files.
        """
        if not self.is_active:
            raise RuntimeError(f"Cannot prepare working copy: workspace {self.session_id} is inactive.")
            
        if not self.reference_dir.exists() or not any(self.reference_dir.iterdir()):
            raise FileNotFoundError(
                f"Reference directory is empty or missing: {self.reference_dir}. "
                "Clone repository into reference/ before preparing working copy."
            )

        # If working_dir already exists, remove it before refreshing
        if self.working_dir.exists():
            shutil.rmtree(self.working_dir, ignore_errors=True)

        # Deep copy reference/ to working/
        shutil.copytree(
            self.reference_dir,
            self.working_dir,
            symlinks=True,
            ignore_dangling_symlinks=True,
        )
        return self.working_dir

    def copy_reference_to_working(self) -> Path:
        """Alias for prepare_working_copy."""
        return self.prepare_working_copy()

    def list_working_files(
        self,
        extension: str = ".py",
        exclude_dirs: Optional[Set[str]] = None,
    ) -> List[Path]:
        """List all files in working/ matching the given extension, excluding ignored directories."""
        if not self.working_dir.exists():
            return []

        ignored = exclude_dirs or DEFAULT_EXCLUDED_DIRS
        matching: List[Path] = []

        for root, dirs, files in os.walk(self.working_dir):
            # Prune excluded directories in-place so os.walk does not descend into them
            dirs[:] = [d for d in dirs if d not in ignored and not d.endswith(".egg-info")]
            
            for file_name in files:
                if file_name.endswith(extension):
                    matching.append(Path(root) / file_name)

        return sorted(matching)

    def cleanup(self) -> None:
        """Safely delete the temporary workspace root directory and all contents."""
        if self.root_dir.exists():
            shutil.rmtree(self.root_dir, ignore_errors=True)
        self._is_active = False

    def __enter__(self) -> WorkspaceSession:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()

    def __repr__(self) -> str:
        status = "active" if self.is_active else "cleaned"
        return f"<WorkspaceSession id={self.session_id} status={status} path={self.root_dir}>"


class WorkspaceManager:
    """Factory and manager for WorkspaceSession instances."""

    def __init__(self, base_dir: Optional[Path | str] = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else None

    def create_workspace(self, base_dir: Optional[Path | str] = None) -> WorkspaceSession:
        return WorkspaceSession(base_temp_dir=base_dir or self.base_dir)

