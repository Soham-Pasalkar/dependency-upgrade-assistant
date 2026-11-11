"""Server-enforced workspace boundary validation preventing arbitrary host filesystem access."""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import Optional

from src.api.config import settings

logger = logging.getLogger(__name__)


class InvalidWorkspaceError(ValueError):
    """Raised when a requested workspace path violates server boundary constraints."""
    pass


def is_safe_relative_path(path_str: str) -> bool:
    """Check that a path string is purely relative and free from traversal or drive patterns."""
    if not path_str or not isinstance(path_str, str):
        return False
    cleaned = path_str.strip()
    if not cleaned:
        return False

    # 1. Null byte check
    if "\0" in cleaned:
        return False

    # 2. Reject absolute paths (POSIX and Windows)
    if cleaned.startswith(("/", "\\")):
        return False
    if Path(cleaned).is_absolute():
        return False
    if re.match(r"^[a-zA-Z]:", cleaned):
        return False
    if cleaned.startswith("~"):
        return False

    # 3. URL encoding check
    unquoted = urllib.parse.unquote(cleaned)
    if "\0" in unquoted or unquoted.startswith(("/", "\\", "~")) or re.match(r"^[a-zA-Z]:", unquoted):
        return False

    # 4. Traversal segment check across both slash types
    normalized = unquoted.replace("\\", "/").split("/")
    if ".." in normalized or "." in normalized:
        return False

    return True


def validate_trusted_workspace_path(
    candidate_path: str | Path,
    workspace_root: Optional[Path] = None,
) -> Path:
    """Resolve and validate that candidate_path strictly resides within the server workspace root.

    Guarantees:
    1. Rejects null bytes, URL encodings, Windows drive letters, and traversal (..).
    2. Strictly resolves relative to configured workspace_root.
    3. Rejects sibling directory prefix confusion (/root_evil vs /root).
    4. Rejects symlink escapes pointing outside workspace_root.
    5. Confirms that the target exists and is an authorized directory.
    6. Returns safe error without exposing internal server paths.
    """
    if not candidate_path:
        raise InvalidWorkspaceError("Workspace path cannot be empty.")

    raw_str = str(candidate_path).strip()
    if not is_safe_relative_path(raw_str):
        raise InvalidWorkspaceError("Invalid workspace path: absolute paths, traversals, and drive letters are forbidden.")

    root = (workspace_root or settings.resolved_workspace_root).resolve()
    unquoted = urllib.parse.unquote(raw_str).replace("\\", "/")

    # Form target path
    candidate_target = root / unquoted
    try:
        resolved_target = candidate_target.resolve()
    except Exception as exc:
        raise InvalidWorkspaceError("Failed to resolve workspace path.") from exc

    # Boundary containment verification:
    # Must be a strict child within root (never root itself)
    if resolved_target == root:
        raise InvalidWorkspaceError("Access denied: workspace path must not equal authorized root itself.")
    try:
        if root not in resolved_target.parents:
            raise InvalidWorkspaceError("Access denied: path escapes authorized workspace root.")
    except Exception as exc:
        raise InvalidWorkspaceError("Access denied: path escapes authorized workspace root.") from exc

    # Sibling prefix check (string prefix protection)
    root_str = str(root)
    target_str = str(resolved_target)
    if not target_str.startswith(root_str + os.sep):
        raise InvalidWorkspaceError("Access denied: path is not within authorized workspace root.")

    # Symlink escape verification
    if candidate_target.is_symlink():
        try:
            real_link_target = candidate_target.resolve()
            if real_link_target == root or root not in real_link_target.parents:
                raise InvalidWorkspaceError("Access denied: symlink points outside authorized workspace root.")
        except Exception as exc:
            raise InvalidWorkspaceError("Failed to verify symlink target.") from exc

    # Target existence and directory check
    if not resolved_target.exists():
        raise InvalidWorkspaceError("Specified workspace directory does not exist.")
    if not resolved_target.is_dir():
        raise InvalidWorkspaceError("Specified workspace path is not a directory.")

    return resolved_target

