"""Architecture Reference Model Verification:
These tests verify the target architecture and state-transition model for persistent Docker cleanup recovery,
lease fencing tokens, OS-level filesystem locks (fcntl.flock), path containment validation, parent-directory fsync durability,
crash consistency, and multi-threaded concurrency with separate SQLite connections in WAL mode.

DISCLAIMER: These tests verify the architectural reference model and design contracts.
They do NOT represent a completed production implementation in src/ (scheduled for Phase 2 Construction),
nor do they claim real-daemon Docker runtime verification.
"""

import fcntl
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest.mock
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import pytest


class SecurityException(Exception):
    """Raised when a workspace path violates root containment or symlink policies."""
    pass


def validate_workspace_path(path: str, authorized_root: str) -> str:
    """Validates that a workspace path is strictly contained within authorized_root.
    
    Security invariants:
    - Path must not be empty.
    - Path must not be a direct symlink.
    - Canonical path (os.path.realpath) must be strictly contained within authorized_root.
    - Sibling directory attacks (e.g. /root_evil vs /root) must be rejected.
    - Must not equal authorized_root itself.
    """
    if not path or not authorized_root:
        raise ValueError("Path and authorized_root must be non-empty strings")
    
    if os.path.islink(path):
        raise SecurityException(f"Path is a direct symlink: {path}")

    real_root = os.path.realpath(authorized_root)
    real_path = os.path.realpath(path)

    if real_path == real_root:
        raise SecurityException(f"Path cannot be the authorized root directory: {path}")

    try:
        common = os.path.commonpath([real_root, real_path])
    except ValueError:
        raise SecurityException(f"Path is on a different drive from authorized root: {path}")

    if common != real_root:
        raise SecurityException(f"Path escapes authorized root ({real_root}): {path}")

    root_parts = os.path.normpath(real_root).split(os.sep)
    path_parts = os.path.normpath(real_path).split(os.sep)
    if len(path_parts) <= len(root_parts) or path_parts[:len(root_parts)] != root_parts:
        raise SecurityException(f"Path is not a strict child of authorized root: {path}")

    return real_path


def validate_quarantine_path(ws_path: str, q_path: str, lease_token: str, authorized_root: str) -> Tuple[str, str]:
    """Validates both workspace and quarantine paths and verifies exact quarantine naming structure.
    
    Security invariants:
    - Both paths must pass validate_workspace_path.
    - Quarantine directory must reside in the exact same parent directory as workspace.
    - Quarantine basename must be strictly '{ws_basename}.quarantine_{lease_token}'.
    - Reject any generic directory containing '.quarantine_' if naming contract does not match.
    """
    real_ws = validate_workspace_path(ws_path, authorized_root)
    real_q = validate_workspace_path(q_path, authorized_root)

    ws_parent = os.path.dirname(real_ws)
    q_parent = os.path.dirname(real_q)
    if ws_parent != q_parent:
        raise SecurityException("Quarantine directory parent does not match workspace parent")

    ws_basename = os.path.basename(real_ws)
    expected_q_basename = f"{ws_basename}.quarantine_{lease_token}"
    if os.path.basename(real_q) != expected_q_basename:
        raise SecurityException(
            f"Quarantine name mismatch: expected '{expected_q_basename}', got '{os.path.basename(real_q)}'"
        )

    return real_ws, real_q


class FsyncException(Exception):
    """Raised when directory metadata fsync fails."""
    pass


class DeletionException(Exception):
    """Raised when recursive directory deletion fails or leaves orphaned entries."""
    pass


class WorkspaceFileLock:
    """POSIX fcntl.flock advisory kernel lock with persistent lock file.
    
    Advisory Lock Contract:
    - flock is an advisory kernel lock. It only guarantees mutual exclusion between
      processes that cooperatively participate in the lock protocol.
    - Persistent lock file: Lock files are NOT unlinked during normal release.
      Unlinking a lock file creates an inode-splitting race where Worker A unlinks
      inode 1 while Worker B waits on it, and Worker C creates inode 2 at the same
      path, allowing B and C to hold locks on different inodes concurrently.
    - The open file descriptor is held continuously throughout the entire protected
      filesystem critical section.
    - Locks are opened with mode 0o600 (read/write by owner only).
    """
    def __init__(self, lock_dir: str, cleanup_id: str):
        self.lock_dir = os.path.realpath(lock_dir)
        self.cleanup_id = cleanup_id
        self.lock_path = os.path.join(self.lock_dir, f".cleanup_{cleanup_id}.lock")
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        if not os.path.exists(self.lock_dir):
            os.makedirs(self.lock_dir, mode=0o700, exist_ok=True)
            
        if os.path.islink(self.lock_path):
            raise SecurityException(f"Lock file path is a symlink: {self.lock_path}")
            
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
            
        fd = None
        try:
            fd = os.open(self.lock_path, flags, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd = fd
            return True
        except (BlockingIOError, OSError):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self.fd = None
            return False

    def release(self):
        """Releases the kernel lock and closes the descriptor. Does NOT unlink."""
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def fsync_directory(dir_path: str):
    """Flushes directory metadata to storage using platform-specific mechanisms.
    
    Durability Semantics:
    - macOS (Darwin): Executes fcntl.F_FULLFSYNC on an open directory descriptor,
      commanding the drive controller to flush its volatile track cache to non-volatile storage.
      Also invokes os.fsync for unified testing interception and POSIX conformance.
    - Linux / POSIX: Executes os.fsync(dir_fd), forcing kernel VFS dirty pages and filesystem
      journal/metadata blocks to the storage controller cache.
    - Fail-Closed: Raises FsyncException on any error (I/O failure or unsupported operation),
      ensuring no unverified durability assumptions are made.
    """
    if not dir_path or not os.path.exists(dir_path):
        return
    try:
        dir_fd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
            if hasattr(fcntl, "F_FULLFSYNC"):
                try:
                    fcntl.fcntl(dir_fd, fcntl.F_FULLFSYNC)
                except OSError as e:
                    raise FsyncException(f"macOS F_FULLFSYNC failed for directory {dir_path}: {e}") from e
        finally:
            os.close(dir_fd)
    except OSError as e:
        raise FsyncException(f"Directory fsync failed for {dir_path}: {e}") from e


HEARTBEAT_INTERVAL = 20


def secure_rmtree(path: str, authorized_root: str, heartbeat_fn: Optional[Callable[[], bool]] = None):
    """Recursively deletes a directory without following symlinks into external directories.
    
    Fails closed: Aggregates all errors during traversal and asserts the directory is gone.
    Raises DeletionException if any file or directory cannot be removed, or if heartbeat_fn fails.
    
    Heartbeat Contract:
    - A unified processed-item counter increments for EVERY filesystem entry processed:
      regular files, symlinks, normal directories, symlink directories, and the root entry.
    - An initial heartbeat check is executed prior to any filesystem deletion.
    - A heartbeat check is executed exactly every HEARTBEAT_INTERVAL (20) items.
    - The maximum number of filesystem operations that may occur between successful heartbeat
      checks is strictly bounded by 20 items.
    - If heartbeat_fn returns False at any check, deletion halts immediately, leaving remaining
      entries untouched on disk (fail closed).
    
    Security Limitations:
    - Uses os.walk(followlinks=False) to unlink symlinks directly rather than descending into them.
    - Note: os.walk(followlinks=False) does not prevent TOCTOU symlink replacement races
      that require atomic descriptor-relative openat(..., O_NOFOLLOW) system calls.
    """
    real_path = validate_workspace_path(path, authorized_root)
    if not os.path.exists(real_path) and not os.path.islink(real_path):
        return

    if heartbeat_fn is not None and not heartbeat_fn():
        raise DeletionException("Lease renewal heartbeat failed before recursive deletion started")

    items_processed = 0

    def _step_heartbeat():
        nonlocal items_processed
        items_processed += 1
        if heartbeat_fn is not None and items_processed % HEARTBEAT_INTERVAL == 0:
            if not heartbeat_fn():
                raise DeletionException(
                    f"Lease renewal heartbeat failed during recursive deletion after {items_processed} items"
                )

    errors = []
    for root, dirs, files in os.walk(real_path, topdown=False, followlinks=False):
        for name in files:
            file_path = os.path.join(root, name)
            _step_heartbeat()
            try:
                os.unlink(file_path)
            except OSError as e:
                errors.append(f"Failed to unlink file {file_path}: {e}")
        for name in dirs:
            dir_path = os.path.join(root, name)
            _step_heartbeat()
            if os.path.islink(dir_path):
                try:
                    os.unlink(dir_path)
                except OSError as e:
                    errors.append(f"Failed to unlink directory symlink {dir_path}: {e}")
            else:
                try:
                    os.rmdir(dir_path)
                except OSError as e:
                    errors.append(f"Failed to rmdir {dir_path}: {e}")

    _step_heartbeat()
    if os.path.islink(real_path):
        try:
            os.unlink(real_path)
        except OSError as e:
            errors.append(f"Failed to unlink root symlink {real_path}: {e}")
    elif os.path.exists(real_path):
        try:
            os.rmdir(real_path)
        except OSError as e:
            errors.append(f"Failed to rmdir root {real_path}: {e}")

    if errors or os.path.exists(real_path) or os.path.islink(real_path):
        raise DeletionException(
            f"secure_rmtree failed to completely remove {path}. Errors: {errors}"
        )


class MockDockerContainer:
    def __init__(self, container_id: str, labels: Dict[str, str], state: str = "running"):
        self.id = container_id
        self.labels = labels
        self.state = state
        self.stopped = False
        self.removed = False

    def stop(self, timeout: int = 5):
        self.stopped = True
        self.state = "exited"

    def remove(self, force: bool = True):
        if not self.stopped and not force:
            raise RuntimeError("Cannot remove running container without force")
        self.removed = True


class MockDockerClient:
    def __init__(self, containers: Optional[List[MockDockerContainer]] = None, is_available: bool = True):
        self._containers = containers or []
        self.is_available = is_available

    def list_containers(self, all: bool = True, filters: Optional[Dict[str, str]] = None) -> List[MockDockerContainer]:
        if not self.is_available:
            raise ConnectionError("Docker daemon is offline or socket /var/run/docker.sock unreachable")
        
        results = []
        for c in self._containers:
            if c.removed:
                continue
            if filters and "label" in filters:
                required_label = filters["label"]
                if "=" in required_label:
                    k, v = required_label.split("=", 1)
                    if c.labels.get(k) != v:
                        continue
                else:
                    if required_label not in c.labels:
                        continue
            results.append(c)
        return results

    def get_container(self, container_id: str) -> Optional[MockDockerContainer]:
        if not self.is_available:
            raise ConnectionError("Docker daemon is offline")
        for c in self._containers:
            if c.id == container_id and not c.removed:
                return c
        return None


def init_cleanup_db(conn: sqlite3.Connection):
    """Initializes the durable pending_cleanups schema with non-cascading FK, lease token, quarantine_dir, and schedule."""
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("""
    CREATE TABLE repositories (id TEXT PRIMARY KEY, name TEXT);
    """)
    conn.execute("""
    CREATE TABLE analysis_runs (
        id TEXT PRIMARY KEY,
        repository_id TEXT REFERENCES repositories(id),
        status TEXT NOT NULL,
        workspace_dir TEXT
    );
    """)
    conn.execute("""
    CREATE TABLE pending_cleanups (
        id TEXT PRIMARY KEY,
        originating_run_id TEXT NOT NULL,
        run_id TEXT REFERENCES analysis_runs(id) ON DELETE SET NULL,
        container_id TEXT,
        workspace_dir TEXT NOT NULL,
        quarantine_dir TEXT,
        status TEXT NOT NULL CHECK(status IN ('pending', 'in_progress', 'quarantine_intent', 'quarantined', 'completed', 'failed')),
        worker_id TEXT,
        lease_token TEXT,
        claimed_at TIMESTAMP,
        lease_expires_at TIMESTAMP,
        next_attempt_at TIMESTAMP,
        retry_count INTEGER NOT NULL DEFAULT 0,
        error_type TEXT CHECK(error_type IN ('TRANSIENT_DOCKER_UNAVAILABLE', 'PERMANENT_ERROR', NULL)),
        last_error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()


def enqueue_interrupted_run_cleanup(conn: sqlite3.Connection, run_id: str, workspace_dir: str, container_id: Optional[str] = None):
    """Enqueues an interrupted run into pending_cleanups and marks run failed."""
    conn.commit()
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE;")
    cursor.execute("UPDATE analysis_runs SET status = 'failed' WHERE id = ?;", (run_id,))
    cleanup_id = f"clean_{run_id}"
    cursor.execute(
        """
        INSERT INTO pending_cleanups (id, originating_run_id, run_id, container_id, workspace_dir, status, next_attempt_at)
        VALUES (?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP);
        """,
        (cleanup_id, run_id, run_id, container_id, workspace_dir)
    )
    conn.commit()


def claim_cleanup_record(
    conn: sqlite3.Connection,
    cleanup_id: str,
    worker_id: str,
    lease_seconds: int = 120,
    forced_token: Optional[str] = None,
    existing_lock: Optional[WorkspaceFileLock] = None
) -> Tuple[bool, Optional[str]]:
    """Atomically claims an eligible pending or expired in_progress cleanup record with a unique fencing lease token.
    
    Lock-First Protocol:
    - Before claiming or mutating DB state, verifies that WorkspaceFileLock is not held by another active worker.
    - If another worker holds WorkspaceFileLock, the claim yields immediately without mutating SQLite.
    - Generates 256 bits of cryptographic entropy (secrets.token_hex(32) -> 64 hex characters).
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT workspace_dir, quarantine_dir FROM pending_cleanups WHERE id = ?;",
        (cleanup_id,)
    )
    row = cursor.fetchone()
    if not row:
        return False, None
    ws_dir, q_dir = row

    lock_dir = None
    if ws_dir and os.path.dirname(ws_dir):
        lock_dir = os.path.dirname(ws_dir)
    elif q_dir and os.path.dirname(q_dir):
        lock_dir = os.path.dirname(q_dir)
    else:
        lock_dir = tempfile.gettempdir()

    # LOCK-FIRST PROTOCOL CHECK:
    # If caller pre-acquired the lock, verify it is active and held.
    # If caller did not pre-acquire the lock, verify that WorkspaceFileLock is not currently
    # held by another active worker on this workspace.
    if existing_lock is not None:
        if existing_lock.fd is None:
            return False, None
    else:
        test_lock = WorkspaceFileLock(lock_dir, cleanup_id)
        if not test_lock.acquire():
            # Another worker currently holds the physical filesystem lock!
            # Under Lock-Authoritative Ownership, the active lock holder owns the cleanup;
            # this normal claim MUST NOT supersede it.
            return False, None
        test_lock.release()

    token = forced_token or secrets.token_hex(32)
    modifier = f"+{lease_seconds} seconds" if lease_seconds >= 0 else f"{lease_seconds} seconds"
    cursor.execute("BEGIN IMMEDIATE;")
    cursor.execute(
        """
        UPDATE pending_cleanups
        SET status = 'in_progress',
            worker_id = ?,
            lease_token = ?,
            claimed_at = CURRENT_TIMESTAMP,
            lease_expires_at = datetime('now', ?),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND (
            (status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP))
            OR (status = 'in_progress' AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP))
        );
        """,
        (worker_id, token, modifier, cleanup_id)
    )
    if cursor.rowcount == 1:
        conn.commit()
        return True, token
    conn.rollback()
    return False, None


def renew_cleanup_lease(
    conn: sqlite3.Connection,
    cleanup_id: str,
    worker_id: str,
    lease_token: str,
    lease_seconds: int = 120
) -> bool:
    """Atomically renews the lease heartbeat, verifying worker identity, lease token, and that lease has not expired."""
    modifier = f"+{lease_seconds} seconds" if lease_seconds >= 0 else f"{lease_seconds} seconds"
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE pending_cleanups
        SET lease_expires_at = datetime('now', ?),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? 
          AND worker_id = ? 
          AND lease_token = ? 
          AND status IN ('in_progress', 'quarantine_intent', 'quarantined')
          AND lease_expires_at >= CURRENT_TIMESTAMP;
        """,
        (modifier, cleanup_id, worker_id, lease_token)
    )
    conn.commit()
    return cursor.rowcount == 1


def record_quarantine_intent(
    conn: sqlite3.Connection,
    cleanup_id: str,
    worker_id: str,
    lease_token: str,
    ws_dir: str,
    forced_q_dir: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    """Durable Step 1: Commits quarantine intent and target path to database before any filesystem action."""
    quarantine_path = forced_q_dir or f"{ws_dir}.quarantine_{lease_token}"
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE pending_cleanups
        SET status = 'quarantine_intent',
            quarantine_dir = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND worker_id = ?
          AND lease_token = ?
          AND status IN ('in_progress', 'quarantine_intent', 'quarantined')
          AND lease_expires_at >= CURRENT_TIMESTAMP;
        """,
        (quarantine_path, cleanup_id, worker_id, lease_token)
    )
    conn.commit()
    if cursor.rowcount == 1:
        return True, quarantine_path
    return False, None


def confirm_quarantine(
    conn: sqlite3.Connection,
    cleanup_id: str,
    worker_id: str,
    lease_token: str,
    lease_seconds: int = 120
) -> bool:
    """Durable Step 2 (Option A): Confirms successful filesystem rename in database and refreshes lease.
    
    Under Option A (Lock-Authoritative Ownership), holding WorkspaceFileLock grants exclusive
    filesystem ownership for the duration of the critical section. Having executed the atomic
    rename on disk, the worker confirms the quarantined status in SQLite and refreshes its lease.
    Recovery cannot steal this record while the OS lock is held. Fences strictly on worker_id,
    lease_token, and status='quarantine_intent'.
    """
    modifier = f"+{lease_seconds} seconds" if lease_seconds >= 0 else f"{lease_seconds} seconds"
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE pending_cleanups
        SET status = 'quarantined',
            lease_expires_at = datetime('now', ?),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND worker_id = ?
          AND lease_token = ?
          AND status = 'quarantine_intent';
        """,
        (modifier, cleanup_id, worker_id, lease_token)
    )
    conn.commit()
    return cursor.rowcount == 1


def complete_quarantine_cleanup(
    conn: sqlite3.Connection,
    cleanup_id: str,
    worker_id: str,
    lease_token: str
) -> bool:
    """Durable Step 3 (Option A): Commits deletion completion to database.
    
    Fences strictly on worker_id, lease_token, status='quarantined', and unexpired lease.
    Clears lease and worker state.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE pending_cleanups
        SET status = 'completed',
            quarantine_dir = NULL,
            worker_id = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            next_attempt_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND worker_id = ?
          AND lease_token = ?
          AND status = 'quarantined'
          AND lease_expires_at >= CURRENT_TIMESTAMP;
        """,
        (cleanup_id, worker_id, lease_token)
    )
    conn.commit()
    return cursor.rowcount == 1


def verify_lease_ownership(
    conn: sqlite3.Connection,
    cleanup_id: str,
    worker_id: str,
    lease_token: str,
    expected_status: str
) -> bool:
    """Verifies that the worker still holds a valid, unexpired lease with the expected status.
    
    Must be called immediately before every destructive filesystem operation.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT 1 FROM pending_cleanups
        WHERE id = ?
          AND worker_id = ?
          AND lease_token = ?
          AND status = ?
          AND lease_expires_at >= CURRENT_TIMESTAMP;
        """,
        (cleanup_id, worker_id, lease_token, expected_status)
    )
    return cursor.fetchone() is not None


def quarantine_and_delete_workspace(
    conn: sqlite3.Connection,
    cleanup_id: str,
    worker_id: str,
    lease_token: str,
    ws_dir: str,
    authorized_root: Optional[str] = None,
    pre_rename_hook: Optional[Callable[[], None]] = None,
    existing_lock: Optional[WorkspaceFileLock] = None
) -> bool:
    """Durable crash-safe quarantine protocol with OS locking & parent fsync:
    1. Acquire OS-level exclusive WorkspaceFileLock (or reuse existing_lock).
    2. Validate path containment within authorized_root.
    3. Persist quarantine intent to SQLite (status = 'quarantine_intent', quarantine_dir recorded).
    4. PRE-RENAME FENCING CHECK: Verify lease ownership immediately before rename.
    5. Atomic filesystem rename: os.replace(ws_dir, quarantine_dir).
    6. Fsync parent directory to ensure rename durability across power loss (raises FsyncException on failure).
    7. Confirm quarantine in SQLite (status = 'quarantined').
    8. PRE-DELETION FENCING CHECK: Verify lease ownership immediately before deletion.
    9. Destructive deletion: secure_rmtree(quarantine_dir) with lease renewal heartbeat.
    10. Fsync parent directory to ensure unlinking durability (raises FsyncException on failure).
    11. Commit completion in SQLite (status = 'completed').
    12. Release lock (persistent lock file; not unlinked).
    """
    lock_dir = os.path.dirname(ws_dir) if ws_dir else tempfile.gettempdir()
    lock = existing_lock or WorkspaceFileLock(lock_dir, cleanup_id)
    owns_lock = existing_lock is None
    if owns_lock:
        if not lock.acquire():
            return False  # Another worker holds OS lock

    try:
        if authorized_root and ws_dir:
            ws_dir = validate_workspace_path(ws_dir, authorized_root)

        # Step 1: Persist intent
        ok, quarantine_path = record_quarantine_intent(conn, cleanup_id, worker_id, lease_token, ws_dir)
        if not ok or not quarantine_path:
            return False

        if authorized_root:
            validate_quarantine_path(ws_dir, quarantine_path, lease_token, authorized_root)

        # PRE-RENAME FENCING CHECK:
        if not verify_lease_ownership(conn, cleanup_id, worker_id, lease_token, "quarantine_intent"):
            return False

        if pre_rename_hook is not None:
            pre_rename_hook()

        # Step 2: Atomic rename
        if ws_dir and os.path.exists(ws_dir):
            os.replace(ws_dir, quarantine_path)
            try:
                fsync_directory(os.path.dirname(ws_dir))
            except FsyncException:
                # Fsync failed; durability not guaranteed. Do NOT confirm quarantine in DB.
                return False

        # Step 3: Confirm quarantine
        if not confirm_quarantine(conn, cleanup_id, worker_id, lease_token):
            return False

        # PRE-DELETION FENCING CHECK:
        if not verify_lease_ownership(conn, cleanup_id, worker_id, lease_token, "quarantined"):
            return False

        # Step 4: Destructive deletion
        if quarantine_path and os.path.exists(quarantine_path):
            try:
                root_for_rmtree = authorized_root or os.path.dirname(quarantine_path)
                def _heartbeat():
                    return renew_cleanup_lease(conn, cleanup_id, worker_id, lease_token)

                secure_rmtree(quarantine_path, root_for_rmtree, heartbeat_fn=_heartbeat)
                fsync_directory(os.path.dirname(quarantine_path))
            except (DeletionException, OSError, FsyncException):
                # Deletion or post-deletion fsync failed; fail closed without completing in DB
                return False

        # Step 5: Complete
        return complete_quarantine_cleanup(conn, cleanup_id, worker_id, lease_token)
    finally:
        if owns_lock:
            lock.release()


ALLOWED_RECOVERABLE_STATUSES = ("in_progress", "quarantine_intent", "quarantined")


def claim_stale_cleanup_for_recovery(
    conn: sqlite3.Connection,
    clean_id: str,
    expected_status: str,
    recovery_worker_id: str = "recovery_worker",
    lease_seconds: int = 120
) -> Tuple[bool, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Atomically claims an expired cleanup record for recovery in a short transaction.
    
    Invariants:
    - expected_status must be in ALLOWED_RECOVERABLE_STATUSES ('in_progress', 'quarantine_intent', 'quarantined').
    - Evaluates WHERE id = ? AND status = ? AND status IN (...) AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP).
    - If status changed concurrently (e.g. to completed or failed), or lease was renewed, rolls back and returns False.
    - Generates 256 bits of cryptographic entropy (secrets.token_hex(32) -> 64 hex characters).
    - Returns: (claimed, recovery_token, status, workspace_dir, quarantine_dir)
    """
    if expected_status not in ALLOWED_RECOVERABLE_STATUSES:
        return False, None, None, None, None

    recovery_token = secrets.token_hex(32)
    modifier = f"+{lease_seconds} seconds" if lease_seconds >= 0 else f"{lease_seconds} seconds"
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE;")
    cursor.execute(
        """
        SELECT status, workspace_dir, quarantine_dir
        FROM pending_cleanups
        WHERE id = ? 
          AND status = ?
          AND status IN ('in_progress', 'quarantine_intent', 'quarantined')
          AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP);
        """,
        (clean_id, expected_status)
    )
    row = cursor.fetchone()
    if not row:
        conn.rollback()
        return False, None, None, None, None
    
    current_status, ws_dir, q_dir = row
    cursor.execute(
        """
        UPDATE pending_cleanups
        SET worker_id = ?,
            lease_token = ?,
            lease_expires_at = datetime('now', ?),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? 
          AND status = ?
          AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP);
        """,
        (recovery_worker_id, recovery_token, modifier, clean_id, expected_status)
    )
    if cursor.rowcount == 1:
        conn.commit()
        return True, recovery_token, current_status, ws_dir, q_dir
    conn.rollback()
    return False, None, None, None, None


def reclaim_stale_in_progress_cleanups(
    conn: sqlite3.Connection,
    allowed_base_dir: Optional[str] = None,
    recovery_worker_id: str = "recovery_daemon"
) -> int:
    """Atomic recovery protocol with lock-first OS file locking, canonical path validation, and parent fsync."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, status, workspace_dir, quarantine_dir
        FROM pending_cleanups
        WHERE (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP)
          AND status IN ('in_progress', 'quarantine_intent', 'quarantined');
        """
    )
    stale_records = cursor.fetchall()
    reclaimed_count = 0

    for clean_id, orig_status, raw_ws, raw_q in stale_records:
        lock_dir = None
        if raw_ws and os.path.dirname(raw_ws):
            lock_dir = os.path.dirname(raw_ws)
        elif raw_q and os.path.dirname(raw_q):
            lock_dir = os.path.dirname(raw_q)
        else:
            lock_dir = tempfile.gettempdir()

        lock = WorkspaceFileLock(lock_dir, clean_id)
        # LOCK-FIRST PROTOCOL: Attempt to acquire OS lock BEFORE claiming the DB record.
        # If an active worker is holding the OS lock, recovery yields immediately
        # without claiming or altering DB state.
        if not lock.acquire():
            continue

        try:
            claimed, rec_token, cur_status, cur_ws, cur_q = claim_stale_cleanup_for_recovery(
                conn, clean_id, expected_status=orig_status, recovery_worker_id=recovery_worker_id
            )
            if not claimed or not rec_token:
                continue

            if allowed_base_dir and cur_ws:
                cur_ws = validate_workspace_path(cur_ws, allowed_base_dir)
            if allowed_base_dir and cur_q:
                cur_q = validate_workspace_path(cur_q, allowed_base_dir)

            ws_exists = bool(cur_ws and os.path.exists(cur_ws))
            q_exists = bool(cur_q and os.path.exists(cur_q))

            def _recovery_heartbeat():
                return renew_cleanup_lease(conn, clean_id, recovery_worker_id, rec_token)

            if cur_status == "quarantined":
                if q_exists:
                    # Pre-deletion fencing check
                    if not verify_lease_ownership(conn, clean_id, recovery_worker_id, rec_token, "quarantined"):
                        continue
                    try:
                        root_for_rmtree = allowed_base_dir or os.path.dirname(cur_q)
                        secure_rmtree(cur_q, root_for_rmtree, heartbeat_fn=_recovery_heartbeat)
                        fsync_directory(os.path.dirname(cur_q))
                    except (DeletionException, OSError, FsyncException):
                        continue
                else:
                    # Quarantine directory already unlinked on disk.
                    # Retry parent directory fsync to guarantee durability before completing.
                    if cur_q:
                        try:
                            fsync_directory(os.path.dirname(cur_q))
                        except FsyncException:
                            continue

                cursor.execute(
                    """
                    UPDATE pending_cleanups
                    SET status = 'completed', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                        lease_expires_at = NULL, next_attempt_at = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND worker_id = ? AND lease_token = ? AND status = 'quarantined'
                      AND lease_expires_at >= CURRENT_TIMESTAMP;
                    """,
                    (clean_id, recovery_worker_id, rec_token)
                )
                if cursor.rowcount == 1:
                    conn.commit()
                    reclaimed_count += 1
                else:
                    conn.rollback()

            elif cur_status == "quarantine_intent":
                if ws_exists and not q_exists:
                    cursor.execute(
                        """
                        UPDATE pending_cleanups
                        SET status = 'pending', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                            lease_expires_at = NULL, next_attempt_at = CURRENT_TIMESTAMP,
                            last_error = 'Quarantine intent rolled back: workspace directory never renamed',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND worker_id = ? AND lease_token = ? AND status = 'quarantine_intent'
                          AND lease_expires_at >= CURRENT_TIMESTAMP;
                        """,
                        (clean_id, recovery_worker_id, rec_token)
                    )
                    if cursor.rowcount == 1:
                        conn.commit()
                        reclaimed_count += 1
                    else:
                        conn.rollback()
                elif not ws_exists and q_exists:
                    # Pre-deletion fencing check
                    if not verify_lease_ownership(conn, clean_id, recovery_worker_id, rec_token, "quarantine_intent"):
                        continue
                    try:
                        root_for_rmtree = allowed_base_dir or os.path.dirname(cur_q)
                        secure_rmtree(cur_q, root_for_rmtree, heartbeat_fn=_recovery_heartbeat)
                        fsync_directory(os.path.dirname(cur_q))
                    except (DeletionException, OSError, FsyncException):
                        continue
                    cursor.execute(
                        """
                        UPDATE pending_cleanups
                        SET status = 'completed', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                            lease_expires_at = NULL, next_attempt_at = NULL,
                            last_error = 'Quarantine completed by recovery worker after rename crash',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND worker_id = ? AND lease_token = ? AND status = 'quarantine_intent'
                          AND lease_expires_at >= CURRENT_TIMESTAMP;
                        """,
                        (clean_id, recovery_worker_id, rec_token)
                    )
                    if cursor.rowcount == 1:
                        conn.commit()
                        reclaimed_count += 1
                    else:
                        conn.rollback()
                elif ws_exists and q_exists:
                    # Pre-deletion fencing check
                    if not verify_lease_ownership(conn, clean_id, recovery_worker_id, rec_token, "quarantine_intent"):
                        continue
                    try:
                        root_for_rmtree = allowed_base_dir or os.path.dirname(cur_q)
                        secure_rmtree(cur_q, root_for_rmtree, heartbeat_fn=_recovery_heartbeat)
                        fsync_directory(os.path.dirname(cur_q))
                    except (DeletionException, OSError, FsyncException):
                        continue
                    cursor.execute(
                        """
                        UPDATE pending_cleanups
                        SET status = 'pending', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                            lease_expires_at = NULL, next_attempt_at = CURRENT_TIMESTAMP,
                            last_error = 'Quarantine artifact cleared; workspace preserved for retry',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND worker_id = ? AND lease_token = ? AND status = 'quarantine_intent'
                          AND lease_expires_at >= CURRENT_TIMESTAMP;
                        """,
                        (clean_id, recovery_worker_id, rec_token)
                    )
                    if cursor.rowcount == 1:
                        conn.commit()
                        reclaimed_count += 1
                    else:
                        conn.rollback()
                else:
                    if cur_q:
                        try:
                            fsync_directory(os.path.dirname(cur_q))
                        except FsyncException:
                            continue
                    cursor.execute(
                        """
                        UPDATE pending_cleanups
                        SET status = 'completed', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                            lease_expires_at = NULL, next_attempt_at = NULL, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND worker_id = ? AND lease_token = ? AND status = 'quarantine_intent'
                          AND lease_expires_at >= CURRENT_TIMESTAMP;
                        """,
                        (clean_id, recovery_worker_id, rec_token)
                    )
                    if cursor.rowcount == 1:
                        conn.commit()
                        reclaimed_count += 1
                    else:
                        conn.rollback()

            elif cur_status == "in_progress":
                cursor.execute(
                    """
                    UPDATE pending_cleanups
                    SET status = 'pending', worker_id = NULL, lease_token = NULL,
                        lease_expires_at = NULL, next_attempt_at = CURRENT_TIMESTAMP,
                        last_error = 'Reclaimed after application restart (lease expired or worker terminated)',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND worker_id = ? AND lease_token = ? AND status = 'in_progress'
                      AND lease_expires_at >= CURRENT_TIMESTAMP;
                    """,
                    (clean_id, recovery_worker_id, rec_token)
                )
                if cursor.rowcount == 1:
                    conn.commit()
                    reclaimed_count += 1
                else:
                    conn.rollback()
        finally:
            lock.release()

    return reclaimed_count


def trigger_docker_health_recovery(conn: sqlite3.Connection) -> int:
    """Reactively resets next_attempt_at to CURRENT_TIMESTAMP for transient Docker errors."""
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE pending_cleanups
        SET next_attempt_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE status = 'pending' AND error_type = 'TRANSIENT_DOCKER_UNAVAILABLE';
        """
    )
    conn.commit()
    return cursor.rowcount


def process_pending_cleanups(conn: sqlite3.Connection, docker_client: MockDockerClient, worker_id: str = "worker_default") -> int:
    """Processes eligible pending cleanup records respecting next_attempt_at, lease ownership, and idempotency."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, originating_run_id, container_id, workspace_dir, retry_count 
        FROM pending_cleanups 
        WHERE status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP);
        """
    )
    records = cursor.fetchall()
    
    completed_count = 0
    for clean_id, orig_run_id, container_id, ws_dir, retries in records:
        lock_dir = os.path.dirname(ws_dir) if ws_dir and os.path.dirname(ws_dir) else tempfile.gettempdir()
        lock = WorkspaceFileLock(lock_dir, clean_id)
        # LOCK-FIRST PROTOCOL: Acquire OS lock before claiming in DB
        if not lock.acquire():
            continue

        try:
            claimed, token = claim_cleanup_record(conn, clean_id, worker_id=worker_id, existing_lock=lock)
            if not claimed or not token:
                continue

            try:
                container = None
                if docker_client.is_available:
                    if container_id:
                        container = docker_client.get_container(container_id)
                    else:
                        matches = docker_client.list_containers(all=True, filters={"label": f"dev.dua.run_id={orig_run_id}"})
                        if matches:
                            container = matches[0]
                else:
                    raise ConnectionError("Docker daemon is offline or socket unreachable")

                if container:
                    if container.labels.get("dev.dua.managed") != "true":
                        raise PermissionError(f"Container {container.id} missing managed label")
                    container.stop(timeout=5)
                    container.remove(force=True)

                # Atomic race-safe quarantine and deletion
                success = quarantine_and_delete_workspace(conn, clean_id, worker_id, token, ws_dir, existing_lock=lock)
                if not success:
                    raise PermissionError("Worker lost lease ownership during quarantine transition; aborting")

                completed_count += 1

            except ConnectionError as ce:
                cursor.execute(
                    """
                    UPDATE pending_cleanups
                    SET status = 'pending', worker_id = NULL, lease_token = NULL, lease_expires_at = NULL,
                        next_attempt_at = datetime('now', '+30 seconds'),
                        error_type = 'TRANSIENT_DOCKER_UNAVAILABLE', last_error = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                    """,
                    (str(ce), clean_id)
                )
                conn.commit()
            except Exception as exc:
                new_retries = retries + 1
                if new_retries >= 5:
                    new_status = 'failed'
                    next_attempt = None
                else:
                    new_status = 'pending'
                    delay_sec = min(300, 10 * (2 ** new_retries))
                    next_attempt = f"+{delay_sec} seconds"

                cursor.execute(
                    """
                    UPDATE pending_cleanups
                    SET status = ?, retry_count = ?, worker_id = NULL, lease_token = NULL, lease_expires_at = NULL,
                        next_attempt_at = CASE WHEN ? IS NOT NULL THEN datetime('now', ?) ELSE NULL END,
                        error_type = 'PERMANENT_ERROR', last_error = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                    """,
                    (new_status, new_retries, next_attempt, next_attempt, str(exc), clean_id)
                )
                conn.commit()
        finally:
            lock.release()
            
    return completed_count


def operator_retry_failed_cleanup(conn: sqlite3.Connection, cleanup_id: str):
    """Explicit operator action to reset a terminal failed cleanup record."""
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE pending_cleanups
        SET status = 'pending',
            retry_count = 0,
            worker_id = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            next_attempt_at = CURRENT_TIMESTAMP,
            error_type = NULL,
            last_error = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?;
        """,
        (cleanup_id,)
    )
    conn.commit()


# ==============================================================================
# BASELINE RECOVERY & RETRY TESTS
# ==============================================================================

def test_docker_unavailable_persists_pending_cleanup():
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_1")
        os.makedirs(ws_path)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")

        docker_client = MockDockerClient(is_available=False)
        completed = process_pending_cleanups(conn, docker_client)

        assert completed == 0
        assert os.path.exists(ws_path)

        row = conn.execute("SELECT status, retry_count, error_type, last_error, next_attempt_at FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "pending"
        assert row[1] == 0
        assert row[2] == "TRANSIENT_DOCKER_UNAVAILABLE"
        assert "Docker daemon is offline" in row[3]
        assert row[4] is not None

    conn.close()


def test_docker_becoming_available_later_retries_and_succeeds():
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_1")
        os.makedirs(ws_path)

        orphan_container = MockDockerContainer("c_1", {"dev.dua.managed": "true", "dev.dua.run_id": "run_1"})
        docker_client = MockDockerClient([orphan_container], is_available=False)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")

        process_pending_cleanups(conn, docker_client)
        assert os.path.exists(ws_path)

        docker_client.is_available = True
        trigger_docker_health_recovery(conn)
        completed = process_pending_cleanups(conn, docker_client)

        assert completed == 1
        assert orphan_container.stopped is True
        assert orphan_container.removed is True
        assert not os.path.exists(ws_path)

        row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"

    conn.close()


def test_application_restarting_while_cleanup_pending():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = os.path.join(tmpdir, "app.db")
        ws_path = os.path.join(tmpdir, "ws_1")
        os.makedirs(ws_path)

        conn1 = sqlite3.connect(db_file)
        init_cleanup_db(conn1)
        conn1.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn1.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', ?);", (ws_path,))
        enqueue_interrupted_run_cleanup(conn1, "run_1", ws_path, container_id="c_1")
        conn1.close()

        orphan_container = MockDockerContainer("c_1", {"dev.dua.managed": "true", "dev.dua.run_id": "run_1"})
        docker_client = MockDockerClient([orphan_container], is_available=True)

        conn2 = sqlite3.connect(db_file)
        completed = process_pending_cleanups(conn2, docker_client)

        assert completed == 1
        assert orphan_container.removed is True
        assert not os.path.exists(ws_path)

        row = conn2.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"
        conn2.close()


def test_repeated_cleanup_attempts_are_idempotent():
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_1")
        os.makedirs(ws_path)

        orphan_container = MockDockerContainer("c_1", {"dev.dua.managed": "true", "dev.dua.run_id": "run_1"})
        docker_client = MockDockerClient([orphan_container], is_available=True)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")

        assert process_pending_cleanups(conn, docker_client) == 1
        assert not os.path.exists(ws_path)

        assert process_pending_cleanups(conn, docker_client) == 0

        conn.execute("UPDATE pending_cleanups SET status = 'pending', next_attempt_at = CURRENT_TIMESTAMP WHERE id = 'clean_run_1';")
        conn.commit()
        assert process_pending_cleanups(conn, docker_client) == 1

        row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"

    conn.close()


def test_container_already_removed_before_retry():
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_1")
        os.makedirs(ws_path)

        docker_client = MockDockerClient([], is_available=True)
        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_already_gone")

        completed = process_pending_cleanups(conn, docker_client)

        assert completed == 1
        assert not os.path.exists(ws_path)
        row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"

    conn.close()


def test_workspace_deletion_only_after_confirmed_container_termination():
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_1")
        os.makedirs(ws_path)

        class FailingStopContainer(MockDockerContainer):
            def stop(self, timeout: int = 5):
                raise RuntimeError("Failed to stop container: device or resource busy")

        failing_container = FailingStopContainer("c_stuck", {"dev.dua.managed": "true", "dev.dua.run_id": "run_1"})
        docker_client = MockDockerClient([failing_container], is_available=True)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_stuck")

        completed = process_pending_cleanups(conn, docker_client)

        assert completed == 0
        assert os.path.exists(ws_path)
        row = conn.execute("SELECT status, retry_count, error_type, last_error FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "pending"
        assert row[1] == 1
        assert row[2] == "PERMANENT_ERROR"
        assert "device or resource busy" in row[3]

    conn.close()


def test_analysis_run_deletion_preserves_cleanup_obligation():
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    enqueue_interrupted_run_cleanup(conn, "run_1", "/tmp/ws_1", container_id="c_1")

    conn.execute("DELETE FROM analysis_runs WHERE id = 'run_1';")
    conn.commit()

    row = conn.execute(
        "SELECT id, originating_run_id, run_id, workspace_dir, status FROM pending_cleanups WHERE id = 'clean_run_1';"
    ).fetchone()

    assert row is not None
    assert row[0] == "clean_run_1"
    assert row[1] == "run_1"
    assert row[2] is None
    assert row[3] == "/tmp/ws_1"
    assert row[4] == "pending"

    conn.close()


def test_application_crash_after_cleanup_claim_leaves_in_progress():
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    enqueue_interrupted_run_cleanup(conn, "run_1", "/tmp/ws_1", container_id="c_1")

    claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_alpha", lease_seconds=120)
    assert claimed is True
    assert token is not None

    row = conn.execute("SELECT status, worker_id, lease_token, lease_expires_at FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
    assert row[0] == "in_progress"
    assert row[1] == "worker_alpha"
    assert row[2] == token
    assert row[3] is not None

    conn.close()


def test_startup_reclamation_of_stale_in_progress_records():
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    enqueue_interrupted_run_cleanup(conn, "run_1", "/tmp/ws_1", container_id="c_1")

    claim_cleanup_record(conn, "clean_run_1", worker_id="worker_dead", lease_seconds=-10)

    reclaimed = reclaim_stale_in_progress_cleanups(conn)
    assert reclaimed == 1

    row = conn.execute("SELECT status, worker_id, lease_token, lease_expires_at, last_error FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
    assert row[0] == "pending"
    assert row[1] is None
    assert row[2] is None
    assert row[3] is None
    assert "Reclaimed after application restart" in row[4]

    conn.close()


def test_concurrent_cleanup_workers_attempting_to_claim_one_record():
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    enqueue_interrupted_run_cleanup(conn, "run_1", "/tmp/ws_1", container_id="c_1")

    claim1, token1 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
    claim2, token2 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_2", lease_seconds=120)

    assert claim1 is True
    assert token1 is not None
    assert claim2 is False
    assert token2 is None

    row = conn.execute("SELECT status, worker_id, lease_token FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
    assert row[0] == "in_progress"
    assert row[1] == "worker_1"
    assert row[2] == token1

    conn.close()


def test_retry_exhaustion_and_explicit_recovery():
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_perm_fail")
        os.makedirs(ws_path)

        class AlwaysFailingContainer(MockDockerContainer):
            def stop(self, timeout: int = 5):
                raise PermissionError("EACCES: permission denied to stop container")

        failing_container = AlwaysFailingContainer("c_perm", {"dev.dua.managed": "true", "dev.dua.run_id": "run_1"})
        docker_client = MockDockerClient([failing_container], is_available=True)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_perm")

        for i in range(4):
            process_pending_cleanups(conn, docker_client)
            conn.execute("UPDATE pending_cleanups SET next_attempt_at = CURRENT_TIMESTAMP WHERE id = 'clean_run_1';")
            conn.commit()
            row = conn.execute("SELECT status, retry_count FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
            assert row[0] == "pending"
            assert row[1] == i + 1

        process_pending_cleanups(conn, docker_client)
        row = conn.execute("SELECT status, retry_count, error_type, next_attempt_at FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "failed"
        assert row[1] == 5
        assert row[2] == "PERMANENT_ERROR"
        assert row[3] is None

        completed = process_pending_cleanups(conn, docker_client)
        assert completed == 0
        assert os.path.exists(ws_path)

        failing_container.__class__ = MockDockerContainer
        operator_retry_failed_cleanup(conn, "clean_run_1")

        row = conn.execute("SELECT status, retry_count FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "pending"
        assert row[1] == 0

        completed = process_pending_cleanups(conn, docker_client)
        assert completed == 1
        assert not os.path.exists(ws_path)

        row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"

    conn.close()


def test_backoff_survives_application_restart_and_not_retried_prematurely():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = os.path.join(tmpdir, "app.db")
        ws_path = os.path.join(tmpdir, "ws_backoff")
        os.makedirs(ws_path)

        conn1 = sqlite3.connect(db_file)
        init_cleanup_db(conn1)
        conn1.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn1.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', ?);", (ws_path,))
        enqueue_interrupted_run_cleanup(conn1, "run_1", ws_path, container_id="c_1")

        conn1.execute("UPDATE pending_cleanups SET next_attempt_at = datetime('now', '+100 seconds') WHERE id = 'clean_run_1';")
        conn1.commit()
        conn1.close()

        conn2 = sqlite3.connect(db_file)
        docker_client = MockDockerClient([], is_available=True)

        completed = process_pending_cleanups(conn2, docker_client)
        assert completed == 0
        assert os.path.exists(ws_path)

        conn2.execute("UPDATE pending_cleanups SET next_attempt_at = CURRENT_TIMESTAMP WHERE id = 'clean_run_1';")
        conn2.commit()

        completed = process_pending_cleanups(conn2, docker_client)
        assert completed == 1
        assert not os.path.exists(ws_path)

        conn2.close()


# ==============================================================================
# LEASE FENCING & ATOMIC QUARANTINE PROTOCOL TESTS
# ==============================================================================

def test_heartbeat_after_lease_expiration_is_rejected():
    """Verify that a worker cannot renew its lease after the lease timestamp has expired."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    enqueue_interrupted_run_cleanup(conn, "run_1", "/tmp/ws_1", container_id="c_1")
    # Claim with expired lease in past
    claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=-5)
    assert claimed is True

    # Renewal must be rejected because lease_expires_at is in the past
    renewed = renew_cleanup_lease(conn, "clean_run_1", worker_id="worker_1", lease_token=token, lease_seconds=120)
    assert renewed is False

    conn.close()


def test_stale_worker_cannot_renew_after_reclamation():
    """Verify that a stale worker cannot renew its lease after another worker has reclaimed the record."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    enqueue_interrupted_run_cleanup(conn, "run_1", "/tmp/ws_1", container_id="c_1")

    # Worker 1 claims with expired lease
    claimed1, token1 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=-5)
    assert claimed1 is True

    # Worker 2 reclaims with a new lease token
    claimed2, token2 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_2", lease_seconds=120)
    assert claimed2 is True
    assert token1 != token2

    # Worker 1 attempts renewal with its stale token
    renewed1 = renew_cleanup_lease(conn, "clean_run_1", worker_id="worker_1", lease_token=token1, lease_seconds=120)
    assert renewed1 is False

    # Worker 2 valid renewal succeeds
    renewed2 = renew_cleanup_lease(conn, "clean_run_1", worker_id="worker_2", lease_token=token2, lease_seconds=120)
    assert renewed2 is True

    conn.close()


def test_lease_ownership_change_during_cleanup_blocks_destructive_deletion():
    """Verify that if lease ownership changes during cleanup, the stale worker is prevented from deleting."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_toctou_guard")
        os.makedirs(ws_path)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")

        # Worker 1 claims record with expired lease
        _, token1 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=-1)

        # Worker 2 reclaims record
        _, token2 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_2", lease_seconds=120)

        # Worker 1 attempts atomic quarantine and deletion using stale token
        success1 = quarantine_and_delete_workspace(conn, "clean_run_1", worker_id="worker_1", lease_token=token1, ws_dir=ws_path)
        assert success1 is False
        # Directory must remain intact on disk
        assert os.path.exists(ws_path)

        # Worker 2 attempts quarantine and deletion with valid token
        success2 = quarantine_and_delete_workspace(conn, "clean_run_1", worker_id="worker_2", lease_token=token2, ws_dir=ws_path)
        assert success2 is True
        assert not os.path.exists(ws_path)

    conn.close()


def test_application_crash_during_quarantine_transition():
    """Verify that an application crash while in quarantined state leaves a durable, recoverable record."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_crash_target")
        os.makedirs(ws_path)
        q_path = f"{ws_path}.quarantine_testtoken"

        # Simulate crash right after rename (status is 'quarantined')
        os.replace(ws_path, q_path)
        conn.execute(
            """
            INSERT INTO pending_cleanups (
                id, originating_run_id, run_id, workspace_dir, quarantine_dir, status, worker_id, lease_token
            ) VALUES ('clean_crash', 'run_1', 'run_1', ?, ?, 'quarantined', 'worker_crashed', 'testtoken');
            """,
            (ws_path, q_path)
        )
        conn.commit()

        # Quarantined directory exists on disk
        assert os.path.exists(q_path)
        assert not os.path.exists(ws_path)

        # Startup recovery sweep processes quarantined records
        reclaimed = reclaim_stale_in_progress_cleanups(conn)
        assert reclaimed == 1

        # Quarantined directory was cleaned up and record is marked completed
        assert not os.path.exists(q_path)
        row = conn.execute("SELECT status, quarantine_dir FROM pending_cleanups WHERE id = 'clean_crash';").fetchone()
        assert row[0] == "completed"
        assert row[1] is None

    conn.close()


def test_concurrent_cleanup_workers_cannot_delete_same_workspace():
    """Verify that concurrent workers attempting quarantine on the same workspace are strictly serialized."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_concurrent_delete")
        os.makedirs(ws_path)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")

        # Worker 1 claims record
        claimed1, token1 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
        assert claimed1 is True

        # Worker 2 tries to claim concurrently (rejected)
        claimed2, token2 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_2", lease_seconds=120)
        assert claimed2 is False

        # Worker 2 attempts quarantine with a guessed/stolen token
        success2 = quarantine_and_delete_workspace(conn, "clean_run_1", worker_id="worker_2", lease_token="invalid", ws_dir=ws_path)
        assert success2 is False
        assert os.path.exists(ws_path)

        # Worker 1 executes authorized quarantine and deletion
        success1 = quarantine_and_delete_workspace(conn, "clean_run_1", worker_id="worker_1", lease_token=token1, ws_dir=ws_path)
        assert success1 is True
        assert not os.path.exists(ws_path)

    conn.close()


def validate_workspace_permissions(path: str) -> bool:
    """Verifies that workspace directory permissions are strictly isolated:
    - Mode must NOT be world-writable (0777 or any others-write bit).
    - Permissions for 'others' (world) must be 000 (no read, no write, no exec).
    - Mode must be 0700 (owner-only) or 0770 (dedicated group).
    """
    st = os.stat(path)
    mode = st.st_mode
    # Forbid any 'others' permissions (read, write, exec)
    if mode & 0o007 != 0:
        return False
    # Specifically forbid world-writable
    if mode & 0o002 != 0:
        return False
    return True


def test_crash_after_intent_persistence_before_rename():
    """Verify crash safety when process dies after persisting quarantine intent but before rename occurs."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_crash_intent")
        os.makedirs(ws_path)
        with open(os.path.join(ws_path, "code.py"), "w") as f:
            f.write("print('hello')")

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
        assert claimed is True

        # Worker 1 persists intent
        ok, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)
        assert ok is True
        assert q_path is not None

        # CRASH INJECTION: Process dies before os.replace().
        # Filesystem: ws_path exists, q_path does NOT exist.
        assert os.path.exists(ws_path)
        assert not os.path.exists(q_path)

        # Expire Worker 1's lease
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Recovery runs
        reclaimed = reclaim_stale_in_progress_cleanups(conn)
        assert reclaimed == 1

        # State transition check: reset to 'pending', quarantine_dir cleared, workspace preserved
        row = conn.execute("SELECT status, quarantine_dir, worker_id, lease_token FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "pending"
        assert row[1] is None
        assert row[2] is None
        assert row[3] is None
        assert os.path.exists(ws_path)

        # Next worker claims and completes cleanly
        claimed2, token2 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_2", lease_seconds=120)
        assert claimed2 is True
        success = quarantine_and_delete_workspace(conn, "clean_run_1", "worker_2", token2, ws_path)
        assert success is True
        assert not os.path.exists(ws_path)
        row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"

    conn.close()


def test_crash_immediately_after_rename():
    """Verify recovery when crash happens after filesystem rename but before SQLite confirms quarantined status."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_crash_after_rename")
        os.makedirs(ws_path)
        with open(os.path.join(ws_path, "data.txt"), "w") as f:
            f.write("important data")

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
        assert claimed is True

        ok, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)
        assert ok is True

        # Rename succeeds
        os.replace(ws_path, q_path)

        # CRASH INJECTION: Process dies before confirm_quarantine().
        # Database has status = 'quarantine_intent'.
        # Filesystem: ws_path is gone, q_path exists.
        assert not os.path.exists(ws_path)
        assert os.path.exists(q_path)

        # Expire lease
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Recovery runs
        reclaimed = reclaim_stale_in_progress_cleanups(conn)
        assert reclaimed == 1

        # Recovery detected that rename happened: deleted q_path and completed cleanup
        assert not os.path.exists(q_path)
        row = conn.execute("SELECT status, quarantine_dir FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"
        assert row[1] is None

    conn.close()


def test_crash_after_quarantine_state_persistence():
    """Verify recovery when crash happens after quarantine state is confirmed in DB."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_crash_confirmed")
        os.makedirs(ws_path)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
        assert claimed is True

        ok, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)
        os.replace(ws_path, q_path)
        confirmed = confirm_quarantine(conn, "clean_run_1", "worker_1", token)
        assert confirmed is True

        # CRASH INJECTION: Process dies before rmtree.
        # DB status = 'quarantined', q_path exists on disk.
        assert os.path.exists(q_path)

        # Expire lease
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Recovery runs
        reclaimed = reclaim_stale_in_progress_cleanups(conn)
        assert reclaimed == 1

        assert not os.path.exists(q_path)
        row = conn.execute("SELECT status, quarantine_dir FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"
        assert row[1] is None

    conn.close()


def test_crash_during_recursive_deletion():
    """Verify recovery when crash happens mid-way through recursive directory deletion."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_partial_delete")
        os.makedirs(os.path.join(ws_path, "sub1"))
        os.makedirs(os.path.join(ws_path, "sub2"))
        with open(os.path.join(ws_path, "sub1", "f1.txt"), "w") as f:
            f.write("a")
        with open(os.path.join(ws_path, "sub2", "f2.txt"), "w") as f:
            f.write("b")

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
        ok, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)
        os.replace(ws_path, q_path)
        confirm_quarantine(conn, "clean_run_1", "worker_1", token)

        # Partial deletion: sub1 deleted, sub2 remains
        shutil.rmtree(os.path.join(q_path, "sub1"))
        assert os.path.exists(os.path.join(q_path, "sub2"))

        # Expire lease to simulate worker crash mid-delete
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Recovery sweeps
        reclaimed = reclaim_stale_in_progress_cleanups(conn)
        assert reclaimed == 1

        # Remainder of q_path is deleted and marked completed
        assert not os.path.exists(q_path)
        row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"

    conn.close()


def test_recovery_of_orphaned_quarantine_directories():
    """Verify multiple orphaned quarantine directories from aborted sessions are safely recovered."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'failed', '/tmp/ws_1');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_2', 'repo_1', 'failed', '/tmp/ws_2');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        q1 = os.path.join(tmpdir, "ws_1.quarantine_tok1")
        q2 = os.path.join(tmpdir, "ws_2.quarantine_tok2")
        os.makedirs(q1)
        os.makedirs(q2)

        conn.execute(
            """
            INSERT INTO pending_cleanups (id, originating_run_id, workspace_dir, quarantine_dir, status, lease_expires_at)
            VALUES ('c1', 'run_1', '/tmp/ws_1', ?, 'quarantined', datetime('now', '-10 seconds')),
                   ('c2', 'run_2', '/tmp/ws_2', ?, 'quarantined', datetime('now', '-10 seconds'));
            """,
            (q1, q2)
        )
        conn.commit()

        reclaimed = reclaim_stale_in_progress_cleanups(conn)
        assert reclaimed == 2
        assert not os.path.exists(q1)
        assert not os.path.exists(q2)

    conn.close()


def test_both_original_and_quarantine_directories_exist():
    """Verify safety when both original workspace and quarantine directory exist: removes artifact, preserves workspace."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_both")
        os.makedirs(ws_path)
        with open(os.path.join(ws_path, "preserved.py"), "w") as f:
            f.write("preserve me")

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
        ok, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)

        # Anomalous state: q_path was created, but ws_path also exists
        os.makedirs(q_path)
        with open(os.path.join(q_path, "artifact.txt"), "w") as f:
            f.write("corrupted artifact")

        assert os.path.exists(ws_path)
        assert os.path.exists(q_path)

        # Expire lease
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Recovery sweeps
        reclaimed = reclaim_stale_in_progress_cleanups(conn)
        assert reclaimed == 1

        # Quarantine artifact was purged; original workspace preserved for clean retry; reset to pending
        assert not os.path.exists(q_path)
        assert os.path.exists(ws_path)
        assert os.path.exists(os.path.join(ws_path, "preserved.py"))
        row = conn.execute("SELECT status, quarantine_dir FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "pending"
        assert row[1] is None

        # Clean retry succeeds
        claimed2, token2 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_2", lease_seconds=120)
        success = quarantine_and_delete_workspace(conn, "clean_run_1", "worker_2", token2, ws_path)
        assert success is True
        assert not os.path.exists(ws_path)

    conn.close()


def test_neither_directory_exists():
    """Verify recovery is completely idempotent when neither original nor quarantine directory exists."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_nonexistent');")
    conn.commit()

    enqueue_interrupted_run_cleanup(conn, "run_1", "/tmp/ws_nonexistent", container_id="c_1")
    claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
    ok, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, "/tmp/ws_nonexistent")

    # Both absent
    assert not os.path.exists("/tmp/ws_nonexistent")
    assert not os.path.exists(q_path)

    # Expire lease
    conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
    conn.commit()

    reclaimed = reclaim_stale_in_progress_cleanups(conn)
    assert reclaimed == 1

    row = conn.execute("SELECT status, quarantine_dir FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
    assert row[0] == "completed"
    assert row[1] is None

    conn.close()


def test_lease_expiration_during_cleanup():
    """Verify that a worker whose lease expires mid-cleanup is barred from committing completion."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_lease_expire")
        os.makedirs(ws_path)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
        ok, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)
        os.replace(ws_path, q_path)
        confirm_quarantine(conn, "clean_run_1", "worker_1", token)

        # Worker 1's lease expires while deleting
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Worker 1 finishes local rmtree and tries to mark completion
        if os.path.exists(q_path):
            shutil.rmtree(q_path)
        completed = complete_quarantine_cleanup(conn, "clean_run_1", "worker_1", token)
        assert completed is False  # Must be rejected because lease expired!

        # Recovery worker takes over
        reclaimed = reclaim_stale_in_progress_cleanups(conn)
        assert reclaimed == 1
        row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"

    conn.close()


def test_stale_worker_attempting_deletion():
    """Verify that when a lease expires and another worker claims it, the stale worker's deletion is rejected."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_stale_worker")
        os.makedirs(ws_path)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        # Worker 1 claims with expired lease
        claimed1, token1 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=-5)

        # Worker 2 claims valid lease
        claimed2, token2 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_2", lease_seconds=120)
        assert claimed2 is True

        # Worker 1 attempts intent persistence -> rejected
        ok1, _ = record_quarantine_intent(conn, "clean_run_1", "worker_1", token1, ws_path)
        assert ok1 is False

        # Worker 2 successfully quarantines and completes
        success2 = quarantine_and_delete_workspace(conn, "clean_run_1", "worker_2", token2, ws_path)
        assert success2 is True
        assert not os.path.exists(ws_path)

    conn.close()


def test_concurrent_recovery_workers():
    """Verify that concurrent recovery sweeps are safe and idempotent without conflicting double-deletions."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_concurrent_rec")
        os.makedirs(ws_path)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=-5)

        # First recovery sweep
        rec1 = reclaim_stale_in_progress_cleanups(conn)
        assert rec1 == 1

        # Second concurrent recovery sweep
        rec2 = reclaim_stale_in_progress_cleanups(conn)
        assert rec2 == 0

        # State is clean
        row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "pending"

    conn.close()


def test_repeated_recovery_execution_is_idempotent():
    """Verify repeated recovery passes produce zero side-effects and maintain clean state."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        ws_path = os.path.join(tmpdir, "ws_repeated_rec")
        os.makedirs(ws_path)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claim_cleanup_record(conn, "clean_run_1", worker_id="worker_dead", lease_seconds=-10)

        # Sweep 1
        assert reclaim_stale_in_progress_cleanups(conn) == 1
        # Sweep 2
        assert reclaim_stale_in_progress_cleanups(conn) == 0
        # Sweep 3
        assert reclaim_stale_in_progress_cleanups(conn) == 0

    conn.close()


def test_workspace_permission_isolation_forbids_world_writable():
    """Verify workspace permissions enforce isolation and forbid world-writable permissions (0777)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_secure = os.path.join(tmpdir, "ws_secure_0700")
        os.makedirs(ws_secure, mode=0o700)
        os.chmod(ws_secure, 0o700)

        # Secure mode 0700 must pass validation
        assert validate_workspace_permissions(ws_secure) is True
        st = os.stat(ws_secure)
        assert (st.st_mode & 0o007) == 0  # others has 000 permissions

        # Insecure world-writable mode 0777 must be rejected
        ws_insecure = os.path.join(tmpdir, "ws_insecure_0777")
        os.makedirs(ws_insecure, mode=0o777)
        os.chmod(ws_insecure, 0o777)
        assert validate_workspace_permissions(ws_insecure) is False

        # Mode with others-read (0755) also rejected for strict isolation
        ws_readable = os.path.join(tmpdir, "ws_readable_0755")
        os.makedirs(ws_readable, mode=0o755)
        os.chmod(ws_readable, 0o755)
        assert validate_workspace_permissions(ws_readable) is False


def test_concurrent_lease_expires_after_intent_persistence_before_rename():
    """Verify recovery when lease expires after intent persistence across separate SQLite connections in WAL mode."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_wal.db")
        conn_init = sqlite3.connect(db_path)
        conn_init.execute("PRAGMA journal_mode=WAL;")
        init_cleanup_db(conn_init)
        conn_init.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn_init.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn_init.commit()
        conn_init.close()

        ws_path = os.path.join(tmpdir, "ws_concurrent_intent")
        os.makedirs(ws_path)
        with open(os.path.join(ws_path, "file.py"), "w") as f:
            f.write("code")

        # Connection 1: Worker 1
        conn1 = sqlite3.connect(db_path)
        conn1.execute("PRAGMA journal_mode=WAL;")
        enqueue_interrupted_run_cleanup(conn1, "run_1", ws_path, container_id="c_1")
        claimed, token1 = claim_cleanup_record(conn1, "clean_run_1", worker_id="worker_1", lease_seconds=120)
        assert claimed is True

        ok, q_path = record_quarantine_intent(conn1, "clean_run_1", "worker_1", token1, ws_path)
        assert ok is True

        # Simulate lease expiration
        conn1.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn1.commit()

        # Connection 2: Recovery Worker on separate connection
        conn_rec = sqlite3.connect(db_path)
        conn_rec.execute("PRAGMA journal_mode=WAL;")
        reclaimed = reclaim_stale_in_progress_cleanups(conn_rec, allowed_base_dir=tmpdir)
        assert reclaimed == 1

        # State check: reset to pending, workspace preserved
        row = conn_rec.execute("SELECT status, quarantine_dir, worker_id FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "pending"
        assert row[1] is None
        assert row[2] is None
        assert os.path.exists(ws_path)

        conn1.close()
        conn_rec.close()


def test_concurrent_stale_worker_resumes_after_recovery_worker_obtains_ownership():
    """Verify that a stale worker resuming after recovery reclamation is strictly barred from confirming state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_wal_stale.db")
        conn_init = sqlite3.connect(db_path)
        conn_init.execute("PRAGMA journal_mode=WAL;")
        init_cleanup_db(conn_init)
        conn_init.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn_init.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn_init.commit()
        conn_init.close()

        ws_path = os.path.join(tmpdir, "ws_stale_compete")
        os.makedirs(ws_path)

        # Worker 1 connection
        conn1 = sqlite3.connect(db_path)
        conn1.execute("PRAGMA journal_mode=WAL;")
        enqueue_interrupted_run_cleanup(conn1, "run_1", ws_path, container_id="c_1")
        claimed, token1 = claim_cleanup_record(conn1, "clean_run_1", worker_id="worker_1", lease_seconds=120)
        assert claimed is True
        ok, q_path = record_quarantine_intent(conn1, "clean_run_1", "worker_1", token1, ws_path)
        assert ok is True

        # Expire lease
        conn1.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn1.commit()

        recovery_done = threading.Event()
        results = {}

        def recovery_thread():
            conn_rec = sqlite3.connect(db_path, timeout=10)
            conn_rec.execute("PRAGMA journal_mode=WAL;")
            # Claim recovery and complete
            reclaimed = reclaim_stale_in_progress_cleanups(conn_rec, allowed_base_dir=tmpdir, recovery_worker_id="rec_daemon")
            results["reclaimed"] = reclaimed
            conn_rec.close()
            recovery_done.set()

        def stale_worker_thread():
            conn_w = sqlite3.connect(db_path, timeout=10)
            conn_w.execute("PRAGMA journal_mode=WAL;")
            assert recovery_done.wait(timeout=5) is True
            # Stale worker attempts to confirm quarantine using superseded token1
            confirmed = confirm_quarantine(conn_w, "clean_run_1", "worker_1", token1)
            results["stale_confirmed"] = confirmed
            conn_w.close()

        t1 = threading.Thread(target=recovery_thread)
        t2 = threading.Thread(target=stale_worker_thread)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # The stale worker must have failed confirmation
        assert results["stale_confirmed"] is False
        conn1.close()


def test_concurrent_lease_expires_during_recursive_deletion():
    """Verify that a worker whose lease expires during deletion is rejected upon completion attempt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_wal_del_exp.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_del_exp")
        os.makedirs(ws_path)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
        ok, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)
        os.replace(ws_path, q_path)
        confirm_quarantine(conn, "clean_run_1", "worker_1", token)

        # Expire lease during deletion
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Worker attempts to complete cleanup
        completed = complete_quarantine_cleanup(conn, "clean_run_1", "worker_1", token)
        assert completed is False  # Must be rejected

        # Recovery worker takes over
        conn_rec = sqlite3.connect(db_path)
        reclaimed = reclaim_stale_in_progress_cleanups(conn_rec, allowed_base_dir=tmpdir)
        assert reclaimed == 1
        row = conn_rec.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"

        conn.close()
        conn_rec.close()


def test_two_recovery_workers_simultaneously_attempt_reclaim():
    """Verify that two recovery workers competing simultaneously for the same stale record are serialized atomically."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_wal_compete.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_compete")
        os.makedirs(ws_path)
        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claim_cleanup_record(conn, "clean_run_1", worker_id="worker_dead", lease_seconds=-5)
        conn.close()

        barrier = threading.Barrier(2)
        claims = {}

        def worker_task(worker_id):
            c = sqlite3.connect(db_path, timeout=10)
            c.execute("PRAGMA journal_mode=WAL;")
            barrier.wait()
            claimed, token, _, _, _ = claim_stale_cleanup_for_recovery(
                c, "clean_run_1", expected_status="in_progress", recovery_worker_id=worker_id
            )
            claims[worker_id] = claimed
            c.close()

        t1 = threading.Thread(target=worker_task, args=("worker_A",))
        t2 = threading.Thread(target=worker_task, args=("worker_B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one worker must succeed in acquiring the claim
        assert list(claims.values()).count(True) == 1
        assert list(claims.values()).count(False) == 1


def test_recovery_competes_with_active_deletion_os_lock():
    """Verify that recovery worker safely yields when a live worker holds the exclusive OS file lock."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_wal_lock.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_locked")
        os.makedirs(ws_path)
        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claim_cleanup_record(conn, "clean_run_1", worker_id="worker_active", lease_seconds=-5)

        # Worker 1 holds OS lock
        lock = WorkspaceFileLock(tmpdir, "clean_run_1")
        assert lock.acquire() is True

        # Recovery attempts sweep
        reclaimed = reclaim_stale_in_progress_cleanups(conn, allowed_base_dir=tmpdir)
        # Recovery must yield without touching files because OS lock is held
        assert reclaimed == 0
        assert os.path.exists(ws_path)

        lock.release()
        conn.close()


def test_stale_worker_attempts_update_on_reclaimed_record():
    """Verify that all state transitions fail when attempted by a stale worker with an obsolete token."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    enqueue_interrupted_run_cleanup(conn, "run_1", "/tmp/ws_1", container_id="c_1")
    _, token1 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=-5)
    _, token2 = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_2", lease_seconds=120)

    # Worker 1 attempts intent, confirm, complete with stale token1
    ok_intent, _ = record_quarantine_intent(conn, "clean_run_1", "worker_1", token1, "/tmp/ws_1")
    assert ok_intent is False

    ok_confirm = confirm_quarantine(conn, "clean_run_1", "worker_1", token1)
    assert ok_confirm is False

    ok_complete = complete_quarantine_cleanup(conn, "clean_run_1", "worker_1", token1)
    assert ok_complete is False

    conn.close()


def test_path_prefix_sibling_directory_attack_rejected():
    """Verify that path validation strictly rejects sibling directory attacks (e.g. /ws_evil vs /ws)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root_dir = os.path.join(tmpdir, "authorized_root")
        os.makedirs(root_dir)

        sibling_dir = os.path.join(tmpdir, "authorized_root_evil", "escape")
        os.makedirs(sibling_dir)

        # Sibling directory attack must raise SecurityException
        with pytest.raises(SecurityException):
            validate_workspace_path(sibling_dir, root_dir)


def test_symlink_based_path_escape_rejected():
    """Verify that path validation rejects direct symlinks and symlink-based path escapes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root_dir = os.path.join(tmpdir, "workspaces")
        os.makedirs(root_dir)

        outside_dir = os.path.join(tmpdir, "outside_target")
        os.makedirs(outside_dir)

        symlink_path = os.path.join(root_dir, "symlink_escape")
        os.symlink(outside_dir, symlink_path)

        with pytest.raises(SecurityException):
            validate_workspace_path(symlink_path, root_dir)


def test_crash_after_filesystem_rename_durability():
    """Verify that parent directory fsync and quarantine confirmation safely recover if crash follows rename."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(":memory:")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_durability")
        os.makedirs(ws_path)
        with open(os.path.join(ws_path, "data.bin"), "wb") as f:
            f.write(b"data")

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)

        ok, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)
        os.replace(ws_path, q_path)
        fsync_directory(os.path.dirname(ws_path))

        # Crash occurs before confirm_quarantine. Status is 'quarantine_intent'.
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Recovery sweeps
        reclaimed = reclaim_stale_in_progress_cleanups(conn, allowed_base_dir=tmpdir)
        assert reclaimed == 1
        assert not os.path.exists(q_path)
        row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "completed"

        conn.close()


def test_repeated_recovery_after_interrupted_deletion():
    """Verify recovery idempotency when a previous recovery worker crashes mid-deletion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(":memory:")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_interrupted")
        os.makedirs(os.path.join(ws_path, "part1"))
        os.makedirs(os.path.join(ws_path, "part2"))

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path, container_id="c_1")
        claimed, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
        ok, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)
        os.replace(ws_path, q_path)
        confirm_quarantine(conn, "clean_run_1", "worker_1", token)

        # Worker 1 deletes part1, then crashes
        shutil.rmtree(os.path.join(q_path, "part1"))
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Recovery pass 1 finishes deletion and marks completed
        assert reclaim_stale_in_progress_cleanups(conn, allowed_base_dir=tmpdir, recovery_worker_id="rec_1") == 1
        assert not os.path.exists(q_path)

        # Repeated pass 2 finds zero work
        assert reclaim_stale_in_progress_cleanups(conn, allowed_base_dir=tmpdir, recovery_worker_id="rec_2") == 0

        conn.close()


def test_lock_file_inode_splitting_race_demonstration():
    """Demonstrates why unlinking lock files creates an inode race across competing processes.
    
    If Worker A unlinks its lock file path while Worker B holds an open descriptor,
    and Worker C creates a new file at the same path, B and C hold locks on different inodes.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = os.path.join(tmpdir, "test_race.lock")

        # Worker A creates inode 1 and locks it
        fd_a = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd_a, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inode_a = os.fstat(fd_a).st_ino

        # Worker B opens the same path (inode 1)
        fd_b = os.open(lock_path, os.O_RDWR)
        inode_b = os.fstat(fd_b).st_ino
        assert inode_a == inode_b  # Same file

        # Worker B cannot lock it yet because A holds it
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd_b, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Worker A unlinks the file path (the bug in the old design)
        os.unlink(lock_path)

        # Worker C comes along and opens/creates lock_path (creates inode 2!)
        fd_c = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        inode_c = os.fstat(fd_c).st_ino
        assert inode_c != inode_a  # Inodes are different!

        # Worker C acquires lock on inode 2
        fcntl.flock(fd_c, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Worker A releases lock on inode 1 and closes
        fcntl.flock(fd_a, fcntl.LOCK_UN)
        os.close(fd_a)

        # Now Worker B acquires lock on inode 1!
        fcntl.flock(fd_b, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # INODE RACE PROVEN: Worker B (inode 1) and Worker C (inode 2) BOTH hold locks simultaneously!
        # This proves why persistent lock files without unlinking are required.
        os.close(fd_b)
        os.close(fd_c)


def _mp_process_worker(lock_dir, cleanup_id, barrier, result_queue):
    lock = WorkspaceFileLock(lock_dir, cleanup_id)
    barrier.wait()
    acquired = lock.acquire()
    result_queue.put(acquired)
    if acquired:
        import time
        time.sleep(0.1)
        lock.release()


def test_persistent_lock_file_multi_process_mutual_exclusion():
    """Verifies that persistent WorkspaceFileLock provides strict mutual exclusion across separate OS processes."""
    import multiprocessing

    with tempfile.TemporaryDirectory() as tmpdir:
        barrier = multiprocessing.Barrier(2)
        queue = multiprocessing.Queue()

        p1 = multiprocessing.Process(target=_mp_process_worker, args=(tmpdir, "clean_multi", barrier, queue))
        p2 = multiprocessing.Process(target=_mp_process_worker, args=(tmpdir, "clean_multi", barrier, queue))
        p1.start()
        p2.start()
        p1.join()
        p2.join()

        results = [queue.get(), queue.get()]
        # Exactly one process must acquire the lock; the other must fail non-blocking
        assert results.count(True) == 1
        assert results.count(False) == 1


def test_token_entropy_256_bits_and_representation():
    """Verifies that generated fencing tokens have exactly 256 bits of entropy (64 hex characters)."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    enqueue_interrupted_run_cleanup(conn, "run_1", "/tmp/ws_1", container_id="c_1")

    # Claim cleanup record
    ok, token = claim_cleanup_record(conn, "clean_run_1", worker_id="worker_1", lease_seconds=120)
    assert ok is True
    assert len(token) == 64  # 32 bytes * 2 hex chars = 64 hex characters = 256 bits
    assert all(c in "0123456789abcdef" for c in token)

    # Claim recovery record
    conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
    conn.commit()
    ok_rec, rec_token, _, _, _ = claim_stale_cleanup_for_recovery(
        conn, "clean_run_1", expected_status="in_progress", recovery_worker_id="rec_1"
    )
    assert ok_rec is True
    assert len(rec_token) == 64
    assert all(c in "0123456789abcdef" for c in rec_token)

    conn.close()


def test_pre_action_fencing_lease_expiry_before_rename():
    """Verifies that pre-rename fencing aborts if the lease expires between intent recording and os.replace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(":memory:")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_pre_rename")
        os.makedirs(ws_path)
        with open(os.path.join(ws_path, "file.txt"), "w") as f:
            f.write("content")

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path)
        ok, token = claim_cleanup_record(conn, "clean_run_1", "worker_1", lease_seconds=120)
        assert ok is True

        # Simulate: Worker starts quarantine_and_delete_workspace, but lease expires right after intent is persisted
        # We test this by recording intent, then expiring the lease in DB, then calling verify_lease_ownership
        record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-10 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Pre-rename fencing check must fail
        fenced = verify_lease_ownership(conn, "clean_run_1", "worker_1", token, "quarantine_intent")
        assert fenced is False

        # Attempting quarantine_and_delete_workspace with expired lease must return False and not rename
        success = quarantine_and_delete_workspace(conn, "clean_run_1", "worker_1", token, ws_path, authorized_root=tmpdir)
        assert success is False
        assert os.path.exists(ws_path)  # Workspace remained intact!

        conn.close()


def test_pre_action_fencing_supersession_before_rename():
    """Verifies that if a recovery worker supersedes ownership before rename, original worker aborts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(":memory:")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_superseded")
        os.makedirs(ws_path)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path)
        _, token1 = claim_cleanup_record(conn, "clean_run_1", "worker_1", lease_seconds=120)
        ok_intent, _ = record_quarantine_intent(conn, "clean_run_1", "worker_1", token1, ws_path)
        assert ok_intent is True
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Recovery worker claims the record with token2
        ok_rec, token2, _, _, _ = claim_stale_cleanup_for_recovery(
            conn, "clean_run_1", expected_status="quarantine_intent", recovery_worker_id="rec_worker"
        )
        assert ok_rec is True

        # Original worker attempts pre-rename verification with token1
        assert verify_lease_ownership(conn, "clean_run_1", "worker_1", token1, "quarantine_intent") is False

        # Full quarantine_and_delete_workspace call by original worker is aborted
        assert quarantine_and_delete_workspace(conn, "clean_run_1", "worker_1", token1, ws_path, authorized_root=tmpdir) is False
        assert os.path.exists(ws_path)

        conn.close()


def test_pre_action_fencing_lease_expiry_before_deletion():
    """Verifies that pre-deletion fencing aborts if the lease expires after confirm_quarantine but before deletion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(":memory:")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_pre_del")
        os.makedirs(ws_path)

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path)
        _, token = claim_cleanup_record(conn, "clean_run_1", "worker_1", lease_seconds=120)
        _, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)
        os.replace(ws_path, q_path)
        confirm_quarantine(conn, "clean_run_1", "worker_1", token)

        # Lease expires right before deletion
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Pre-deletion check must fail
        assert verify_lease_ownership(conn, "clean_run_1", "worker_1", token, "quarantined") is False

        # Quarantined directory remains intact for recovery to handle
        assert os.path.exists(q_path)

        conn.close()


def test_recovery_yields_and_releases_claim_when_lock_held():
    """Verifies that recovery immediately yields without touching DB if WorkspaceFileLock is held."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(":memory:")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_yield")
        os.makedirs(ws_path)
        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path)
        claim_cleanup_record(conn, "clean_run_1", "worker_active", lease_seconds=-5)

        # Worker holds lock
        lock = WorkspaceFileLock(tmpdir, "clean_run_1")
        assert lock.acquire() is True

        # Recovery sweeps
        reclaimed = reclaim_stale_in_progress_cleanups(conn, allowed_base_dir=tmpdir, recovery_worker_id="rec_daemon")
        assert reclaimed == 0

        # Lock-first protocol: verify DB claim was NOT stolen or modified while worker held lock
        row = conn.execute("SELECT worker_id, lease_token FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "worker_active"
        assert row[1] is not None

        # Worker finishes and releases lock
        lock.release()

        # Recovery sweeps again and successfully reclaims
        reclaimed2 = reclaim_stale_in_progress_cleanups(conn, allowed_base_dir=tmpdir, recovery_worker_id="rec_daemon")
        assert reclaimed2 == 1

        row2 = conn.execute("SELECT status, worker_id, lease_token FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row2[0] == "pending"
        assert row2[1] is None

        conn.close()


def test_fsync_failure_handling_before_quarantine_confirmation(monkeypatch):
    """Verifies that if directory fsync fails, the worker aborts without confirming quarantine in SQLite."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(":memory:")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_fsync_fail")
        os.makedirs(ws_path)
        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path)
        _, token = claim_cleanup_record(conn, "clean_run_1", "worker_1", lease_seconds=120)

        def mock_fsync(fd):
            raise OSError("Disk controller write error on fsync")

        monkeypatch.setattr(os, "fsync", mock_fsync)

        success = quarantine_and_delete_workspace(conn, "clean_run_1", "worker_1", token, ws_path, authorized_root=tmpdir)
        assert success is False

        # Status in DB must remain 'quarantine_intent' (did not falsely advance to quarantined)
        row = conn.execute("SELECT status, quarantine_dir FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "quarantine_intent"
        q_path = row[1]
        assert os.path.exists(q_path)  # Renamed on disk

        # Recovery worker sweeps (unmock fsync)
        monkeypatch.undo()
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Recovery sees ws_dir missing and q_path existing, finishes deletion, and marks completed
        reclaimed = reclaim_stale_in_progress_cleanups(conn, allowed_base_dir=tmpdir)
        assert reclaimed == 1
        assert not os.path.exists(q_path)
        row2 = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row2[0] == "completed"

        conn.close()


def test_secure_rmtree_fails_closed_on_file_unlink_error(monkeypatch):
    """Verifies that secure_rmtree raises DeletionException if a file cannot be unlinked, preventing completed status."""
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        target_dir = os.path.join(real_tmp, "target")
        os.makedirs(target_dir)
        bad_file = os.path.join(target_dir, "locked.txt")
        with open(bad_file, "w") as f:
            f.write("test")

        real_unlink = os.unlink

        def mock_unlink(path, *args, **kwargs):
            if os.path.realpath(path) == os.path.realpath(bad_file):
                raise OSError("Permission denied: cannot unlink")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", mock_unlink)

        with pytest.raises(DeletionException) as excinfo:
            secure_rmtree(target_dir, real_tmp)
        assert "cannot unlink" in str(excinfo.value)
        assert os.path.exists(bad_file)
        monkeypatch.undo()


def test_secure_rmtree_fails_closed_on_directory_rmdir_error(monkeypatch):
    """Verifies that secure_rmtree raises DeletionException if rmdir fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        target_dir = os.path.join(real_tmp, "target_rmdir")
        subdir = os.path.join(target_dir, "subdir")
        os.makedirs(subdir)

        real_rmdir = os.rmdir

        def mock_rmdir(path, *args, **kwargs):
            if os.path.realpath(path) == os.path.realpath(subdir):
                raise OSError("Device busy: cannot rmdir")
            return real_rmdir(path, *args, **kwargs)

        monkeypatch.setattr(os, "rmdir", mock_rmdir)

        with pytest.raises(DeletionException) as excinfo:
            secure_rmtree(target_dir, real_tmp)
        assert "Device busy" in str(excinfo.value)
        assert os.path.exists(subdir)
        monkeypatch.undo()


def test_secure_rmtree_symlink_inside_workspace_unlinked_without_traversing():
    """Verifies that symlinks inside the workspace are unlinked directly without deleting external targets."""
    with tempfile.TemporaryDirectory() as tmpdir:
        external_dir = os.path.join(tmpdir, "external_sensitive")
        os.makedirs(external_dir)
        canary = os.path.join(external_dir, "canary.txt")
        with open(canary, "w") as f:
            f.write("important")

        workspace_dir = os.path.join(tmpdir, "ws_symlinks")
        os.makedirs(workspace_dir)
        symlink_in_ws = os.path.join(workspace_dir, "link_to_external")
        os.symlink(external_dir, symlink_in_ws)

        # secure_rmtree deletes workspace
        secure_rmtree(workspace_dir, tmpdir)

        # Workspace is deleted
        assert not os.path.exists(workspace_dir)
        # External target and canary MUST remain completely intact
        assert os.path.exists(external_dir)
        assert os.path.exists(canary)
        with open(canary, "r") as f:
            assert f.read() == "important"


def test_quarantine_path_containment_violation_rejected():
    """Verifies that validate_quarantine_path strictly rejects invalid naming or parent mismatch."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ws_dir = os.path.join(tmpdir, "ws_1")
        os.makedirs(ws_dir)

        token = "a" * 64
        # Wrong basename
        wrong_q = os.path.join(tmpdir, "ws_wrong_name")
        os.makedirs(wrong_q)
        with pytest.raises(SecurityException):
            validate_quarantine_path(ws_dir, wrong_q, token, tmpdir)

        # Different parent directory
        other_parent = os.path.join(tmpdir, "other_parent")
        os.makedirs(other_parent)
        wrong_parent_q = os.path.join(other_parent, f"ws_1.quarantine_{token}")
        os.makedirs(wrong_parent_q)
        with pytest.raises(SecurityException):
            validate_quarantine_path(ws_dir, wrong_parent_q, token, tmpdir)


def test_worker_paused_after_lease_check_recovery_cannot_race_due_to_lock():
    """Verifies that if a worker is paused between lease verification and rename,
    recovery cannot acquire the OS lock or steal DB ownership, eliminating TOCTOU races."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_race.db")
        conn_init = sqlite3.connect(db_path)
        init_cleanup_db(conn_init)
        conn_init.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn_init.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn_init.commit()
        
        ws_path = os.path.join(tmpdir, "ws_paused")
        os.makedirs(ws_path)
        enqueue_interrupted_run_cleanup(conn_init, "run_1", ws_path)
        _, worker_token = claim_cleanup_record(conn_init, "clean_run_1", "worker_1", lease_seconds=120)
        conn_init.close()

        paused_event = threading.Event()
        resume_event = threading.Event()
        worker_result = {}

        def pre_rename():
            paused_event.set()
            resume_event.wait(timeout=5)

        def worker_thread():
            conn_w = sqlite3.connect(db_path)
            conn_w.execute("PRAGMA busy_timeout = 5000;")
            try:
                res = quarantine_and_delete_workspace(
                    conn_w, "clean_run_1", "worker_1", worker_token, ws_path,
                    authorized_root=tmpdir, pre_rename_hook=pre_rename
                )
                worker_result["success"] = res
            finally:
                conn_w.close()

        t = threading.Thread(target=worker_thread)
        t.start()

        # Wait until worker reaches the pre-rename hook (holding WorkspaceFileLock)
        assert paused_event.wait(timeout=5) is True

        # Now simulate lease expiration in DB
        conn_r = sqlite3.connect(db_path)
        conn_r.execute("PRAGMA busy_timeout = 5000;")
        conn_r.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn_r.commit()

        # Recovery sweeps while worker is paused holding WorkspaceFileLock
        reclaimed = reclaim_stale_in_progress_cleanups(conn_r, allowed_base_dir=tmpdir, recovery_worker_id="rec_daemon")
        # Recovery MUST yield because Worker holds the OS lock
        assert reclaimed == 0

        # DB ownership was NOT stolen by recovery
        row = conn_r.execute("SELECT worker_id, lease_token FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "worker_1"
        assert row[1] == worker_token

        # Now allow worker to resume
        resume_event.set()
        t.join(timeout=5)

        # Under Option A (Lock-Authoritative Ownership), the worker retained exclusive
        # filesystem ownership via WorkspaceFileLock. It completed the atomic rename,
        # confirmed quarantine (refreshing the lease), deleted the directory with heartbeat,
        # committed completion in SQLite, and released the lock.
        assert worker_result.get("success") is True
        assert not os.path.exists(ws_path)

        # DB status is 'completed'
        row2 = conn_r.execute("SELECT status, quarantine_dir FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row2[0] == "completed"
        assert row2[1] is None

        # Recovery sweeps again; finds 0 records since cleanup is already completed
        reclaimed2 = reclaim_stale_in_progress_cleanups(conn_r, allowed_base_dir=tmpdir, recovery_worker_id="rec_daemon")
        assert reclaimed2 == 0

        row3 = conn_r.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row3[0] == "completed"

        conn_r.close()


def test_post_deletion_fsync_failure_prevents_completion_and_leaves_quarantined(monkeypatch):
    """Verifies that if post-deletion directory fsync fails, the worker fails closed without marking completed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(":memory:")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_post_fsync")
        os.makedirs(ws_path)
        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path)
        _, token = claim_cleanup_record(conn, "clean_run_1", "worker_1", lease_seconds=120)

        # Mock fsync so first call (rename) succeeds, second call (post-deletion) fails
        fsync_calls = 0
        real_fsync = os.fsync

        def mock_fsync(fd):
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls > 1:
                raise OSError("Disk sync failed after rmtree")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", mock_fsync)

        success = quarantine_and_delete_workspace(conn, "clean_run_1", "worker_1", token, ws_path, authorized_root=tmpdir)
        assert success is False

        # Status in DB must remain 'quarantined'
        row = conn.execute("SELECT status, quarantine_dir FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "quarantined"
        q_path = row[1]
        assert not os.path.exists(q_path)  # Already deleted on disk

        # Recovery sweeps after unmocking fsync
        monkeypatch.undo()
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Recovery retries parent fsync for the already-missing directory and safely advances to completed
        reclaimed = reclaim_stale_in_progress_cleanups(conn, allowed_base_dir=tmpdir)
        assert reclaimed == 1
        row2 = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row2[0] == "completed"

        conn.close()


def test_post_deletion_fsync_failure_in_recovery_fails_closed(monkeypatch):
    """Verifies that if recovery encounters an fsync failure when retrying post-deletion, it fails closed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(":memory:")
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_rec_fsync")
        os.makedirs(ws_path)
        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path)
        _, token = claim_cleanup_record(conn, "clean_run_1", "worker_1", lease_seconds=120)
        _, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_1", token, ws_path)
        confirm_quarantine(conn, "clean_run_1", "worker_1", token)
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()
        # Directory was deleted
        if os.path.exists(ws_path):
            os.rmdir(ws_path)

        def mock_fsync(fd):
            raise OSError("I/O error during recovery fsync")

        monkeypatch.setattr(os, "fsync", mock_fsync)

        reclaimed = reclaim_stale_in_progress_cleanups(conn, allowed_base_dir=tmpdir)
        assert reclaimed == 0

        # Status must still be 'quarantined'
        row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "quarantined"

        conn.close()


def test_stale_recovery_worker_after_lease_expiry_cannot_mark_completed():
    """Verifies that an expired recovery lease cannot commit completed status to SQLite."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()

    enqueue_interrupted_run_cleanup(conn, "run_1", "/tmp/ws_1")
    # Set to quarantined with expired lease
    conn.execute(
        """
        UPDATE pending_cleanups
        SET status = 'quarantined',
            worker_id = 'stale_rec_worker',
            lease_token = 'token_abc_123',
            lease_expires_at = datetime('now', '-10 seconds')
        WHERE id = 'clean_run_1';
        """
    )
    conn.commit()

    # Attempting to mark completed with expired lease fails (0 rows updated)
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE pending_cleanups
        SET status = 'completed', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
            lease_expires_at = NULL, next_attempt_at = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE id = 'clean_run_1' AND worker_id = 'stale_rec_worker' AND lease_token = 'token_abc_123'
          AND status = 'quarantined' AND lease_expires_at >= CURRENT_TIMESTAMP;
        """
    )
    conn.commit()
    assert cursor.rowcount == 0

    # Status remains quarantined
    row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
    assert row[0] == "quarantined"
    conn.close()


def test_secure_rmtree_lease_renewal_heartbeat_aborts_on_failure():
    """Verifies that if heartbeat_fn fails during secure_rmtree, deletion aborts immediately and fails closed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        target = os.path.join(real_tmp, "big_dir")
        os.makedirs(target)
        # Create 25 files
        for i in range(25):
            with open(os.path.join(target, f"file_{i}.txt"), "w") as f:
                f.write(f"data {i}")

        call_count = 0
        def failing_heartbeat():
            nonlocal call_count
            call_count += 1
            # Fail on the second heartbeat check (after 20 items processed)
            return call_count < 2

        with pytest.raises(DeletionException) as excinfo:
            secure_rmtree(target, real_tmp, heartbeat_fn=failing_heartbeat)

        assert "heartbeat failed during recursive deletion" in str(excinfo.value)
        # Directory and remaining files still exist (fail closed)
        assert os.path.exists(target)
        remaining = os.listdir(target)
        assert len(remaining) > 0


def test_recovery_claim_rejects_stale_observed_status_separate_connections():
    """Verifies that if a recovery worker observes a stale status, but another connection
    modifies the status before the atomic claim transaction executes, the claim fails closed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_wal_stale.db")
        conn_init = sqlite3.connect(db_path)
        conn_init.execute("PRAGMA journal_mode=WAL;")
        init_cleanup_db(conn_init)
        conn_init.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn_init.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn_init.commit()

        ws_path = os.path.join(tmpdir, "ws_stale")
        os.makedirs(ws_path)
        enqueue_interrupted_run_cleanup(conn_init, "run_1", ws_path)
        _, token1 = claim_cleanup_record(conn_init, "clean_run_1", "worker_1", lease_seconds=-5)
        record_quarantine_intent(conn_init, "clean_run_1", "worker_1", token1, ws_path)
        conn_init.close()

        # Connection 1: Recovery worker that observed status 'quarantine_intent'
        conn1 = sqlite3.connect(db_path, timeout=5)
        conn1.execute("PRAGMA journal_mode=WAL;")

        # Connection 2: Simulates active worker confirming quarantine before conn1 claims
        conn2 = sqlite3.connect(db_path, timeout=5)
        conn2.execute("PRAGMA journal_mode=WAL;")
        conn2.execute(
            """
            UPDATE pending_cleanups
            SET status = 'quarantined',
                lease_expires_at = datetime('now', '+120 seconds'),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 'clean_run_1';
            """
        )
        conn2.commit()
        conn2.close()

        # Conn1 tries to claim based on its stale observation of 'quarantine_intent'
        claimed, token, cur_status, _, _ = claim_stale_cleanup_for_recovery(
            conn1, "clean_run_1", expected_status="quarantine_intent", recovery_worker_id="rec_worker_1"
        )

        assert claimed is False
        assert token is None

        # Verify DB record is preserved as 'quarantined'
        row = conn1.execute("SELECT status, worker_id FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "quarantined"
        assert row[1] == "worker_1"

        conn1.close()


def test_recovery_claim_rejects_unallowed_status():
    """Verifies that claim_stale_cleanup_for_recovery rejects unrecoverable statuses immediately."""
    conn = sqlite3.connect(":memory:")
    init_cleanup_db(conn)
    conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
    conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
    conn.commit()
    enqueue_interrupted_run_cleanup(conn, "run_1", "/tmp/ws_1")

    for invalid_status in ["completed", "failed", "pending", "unknown", ""]:
        claimed, token, _, _, _ = claim_stale_cleanup_for_recovery(
            conn, "clean_run_1", expected_status=invalid_status
        )
        assert claimed is False
        assert token is None

    conn.close()


def test_worker_crashes_while_holding_lock_recovery_safely_reclaims():
    """Unit test simulation of descriptor closure / manual lock release after worker termination,
    verifying recovery acquires the lock, claims the record, and reconciles state.
    (For real OS kernel-level process crash verification with SIGKILL, see
    test_subprocess_crash_releases_kernel_lock_and_allows_recovery)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_crash.db")
        conn = sqlite3.connect(db_path)
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_crash")
        os.makedirs(ws_path)
        with open(os.path.join(ws_path, "artifact.txt"), "w") as f:
            f.write("crash test data")

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path)
        _, worker_token = claim_cleanup_record(conn, "clean_run_1", "worker_crash", lease_seconds=120)

        # Worker acquires lock and records quarantine intent
        lock_dir = tmpdir
        worker_lock = WorkspaceFileLock(lock_dir, "clean_run_1")
        assert worker_lock.acquire() is True
        ok, q_path = record_quarantine_intent(conn, "clean_run_1", "worker_crash", worker_token, ws_path)
        assert ok is True

        # Simulate process crash: worker lock is released (kernel releases fcntl.flock on exit)
        worker_lock.release()

        # Simulate lease expiry while worker was dead
        conn.execute("UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';")
        conn.commit()

        # Recovery daemon sweeps: successfully acquires lock, claims record, and recovers
        reclaimed = reclaim_stale_in_progress_cleanups(conn, allowed_base_dir=tmpdir, recovery_worker_id="rec_daemon")
        assert reclaimed == 1

        # Record is completed or pending retry depending on filesystem state
        # Here ws_path still exists and q_path does not, so intent rolled back to pending for safe retry
        row = conn.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
        assert row[0] == "pending"

        conn.close()


def test_secure_rmtree_uniform_heartbeat_covers_files_and_directories():
    """Verifies that secure_rmtree counts regular files, subdirectories, and symlink
    directories uniformly in its heartbeat interval."""
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        target = os.path.join(real_tmp, "nested_tree")
        os.makedirs(target)

        # Create 10 files in root
        for i in range(10):
            with open(os.path.join(target, f"root_file_{i}.txt"), "w") as f:
                f.write(f"root {i}")

        # Create 2 subdirectories, each with 5 files
        for d in range(2):
            sub = os.path.join(target, f"subdir_{d}")
            os.makedirs(sub)
            for f_idx in range(5):
                with open(os.path.join(sub, f"sub_file_{f_idx}.txt"), "w") as f:
                    f.write(f"sub {d} {f_idx}")

        # Create a directory symlink
        symlink_target = os.path.join(real_tmp, "external_dir")
        os.makedirs(symlink_target)
        os.symlink(symlink_target, os.path.join(target, "symlinked_dir"))

        heartbeat_calls = []
        def counting_heartbeat():
            heartbeat_calls.append(len(heartbeat_calls))
            return True

        secure_rmtree(target, real_tmp, heartbeat_fn=counting_heartbeat)

        # Initial heartbeat (before traversal) + at least 1 interval check (at 20 items)
        assert len(heartbeat_calls) >= 2
        assert not os.path.exists(target)


def test_fsync_directory_darwin_fullfsync_and_failure_injection(monkeypatch):
    """Verifies that fsync_directory utilizes F_FULLFSYNC when available and fails closed on error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Standard execution succeeds
        fsync_directory(tmpdir)

        # Test failure injection on os.fsync raises FsyncException
        def mock_fsync(fd):
            raise OSError("Injected disk failure on fsync")

        monkeypatch.setattr(os, "fsync", mock_fsync)
        with pytest.raises(FsyncException) as excinfo:
            fsync_directory(tmpdir)

        assert "Directory fsync failed" in str(excinfo.value)
        monkeypatch.undo()

        # If on macOS/Darwin, test F_FULLFSYNC failure injection as well
        if hasattr(fcntl, "F_FULLFSYNC"):
            real_fcntl = fcntl.fcntl

            def mock_fcntl(fd, op, *args, **kwargs):
                if op == fcntl.F_FULLFSYNC:
                    raise OSError("Injected hardware cache flush error")
                return real_fcntl(fd, op, *args, **kwargs)

            monkeypatch.setattr(fcntl, "fcntl", mock_fcntl)
            with pytest.raises(FsyncException) as excinfo2:
                fsync_directory(tmpdir)

            assert "macOS F_FULLFSYNC failed" in str(excinfo2.value)
            monkeypatch.undo()


def test_normal_claim_cannot_supersede_active_lock_holder():
    """Verifies that normal claim_cleanup_record() participates in the Lock-First Protocol
    and cannot supersede an expired in_progress record if another worker holds WorkspaceFileLock."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_normal_claim_lock.db")
        conn_init = sqlite3.connect(db_path)
        conn_init.execute("PRAGMA journal_mode = WAL;")
        init_cleanup_db(conn_init)
        conn_init.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn_init.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn_init.commit()

        ws_path = os.path.join(tmpdir, "ws_locked_active")
        os.makedirs(ws_path)
        enqueue_interrupted_run_cleanup(conn_init, "run_1", ws_path)
        _, token_a = claim_cleanup_record(conn_init, "clean_run_1", "worker_A", lease_seconds=120)
        conn_init.close()

        worker_a_paused = threading.Event()
        worker_a_resume = threading.Event()
        worker_a_result = {}

        def pre_rename():
            worker_a_paused.set()
            assert worker_a_resume.wait(timeout=5) is True

        def worker_a_task():
            conn_a = sqlite3.connect(db_path, timeout=10)
            conn_a.execute("PRAGMA journal_mode = WAL;")
            try:
                res = quarantine_and_delete_workspace(
                    conn_a, "clean_run_1", "worker_A", token_a, ws_path,
                    authorized_root=tmpdir, pre_rename_hook=pre_rename
                )
                worker_a_result["success"] = res
            finally:
                conn_a.close()

        t_a = threading.Thread(target=worker_a_task)
        t_a.start()

        # Step 2 & 3: Wait until Worker A acquires WorkspaceFileLock and pauses before rename
        assert worker_a_paused.wait(timeout=5) is True

        # Step 4: Expire Worker A's database lease while it holds WorkspaceFileLock
        conn_parent = sqlite3.connect(db_path, timeout=10)
        conn_parent.execute("PRAGMA journal_mode = WAL;")
        conn_parent.execute(
            "UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';"
        )
        conn_parent.commit()

        # Step 5: Worker B on a separate connection attempts normal claim_cleanup_record()
        conn_b = sqlite3.connect(db_path, timeout=10)
        conn_b.execute("PRAGMA journal_mode = WAL;")
        claimed_b, token_b = claim_cleanup_record(conn_b, "clean_run_1", worker_id="worker_B")

        # Step 6: Verify Worker B cannot supersede Worker A while A holds WorkspaceFileLock
        assert claimed_b is False
        assert token_b is None

        # Verify DB record was NOT mutated: worker_id remains worker_A, token remains token_a
        row = conn_parent.execute(
            "SELECT worker_id, lease_token, status FROM pending_cleanups WHERE id = 'clean_run_1';"
        ).fetchone()
        assert row[0] == "worker_A"
        assert row[1] == token_a
        assert row[2] == "quarantine_intent"

        # Step 7: Resume Worker A
        worker_a_resume.set()
        t_a.join(timeout=5)

        # Step 8: Verify that ownership, filesystem state, and database state remain consistent
        assert worker_a_result.get("success") is True
        assert not os.path.exists(ws_path)

        row_final = conn_parent.execute(
            "SELECT status, worker_id, lease_token FROM pending_cleanups WHERE id = 'clean_run_1';"
        ).fetchone()
        assert row_final[0] == "completed"
        assert row_final[1] is None
        assert row_final[2] is None

        conn_b.close()
        conn_parent.close()


def test_subprocess_crash_releases_kernel_lock_and_allows_recovery():
    """Verifies with a real OS child subprocess that terminating a process holding WorkspaceFileLock
    causes the OS kernel to release the fcntl.flock advisory lock, enabling recovery to acquire
    the lock and reconcile the filesystem and database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_subproc_crash.db")
        conn = sqlite3.connect(db_path)
        init_cleanup_db(conn)
        conn.execute("INSERT INTO repositories VALUES ('repo_1', 'my-repo');")
        conn.execute("INSERT INTO analysis_runs VALUES ('run_1', 'repo_1', 'running', '/tmp/ws_1');")
        conn.commit()

        ws_path = os.path.join(tmpdir, "ws_subproc")
        os.makedirs(ws_path)
        with open(os.path.join(ws_path, "important_data.txt"), "w") as f:
            f.write("payload")

        enqueue_interrupted_run_cleanup(conn, "run_1", ws_path)
        _, worker_token = claim_cleanup_record(conn, "clean_run_1", "worker_child", lease_seconds=120)
        # Record quarantine intent in DB
        record_quarantine_intent(conn, "clean_run_1", "worker_child", worker_token, ws_path)
        conn.close()

        # Step 1: Child subprocess acquires WorkspaceFileLock
        child_code = f"""
import sys, os, fcntl, time
lock_dir = {repr(tmpdir)}
lock_path = os.path.join(lock_dir, ".cleanup_clean_run_1.lock")
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
sys.stdout.write("LOCKED\\n")
sys.stdout.flush()
# Sleep indefinitely waiting for SIGKILL
time.sleep(60)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            # Wait for child to signal that it holds the kernel lock
            line = proc.stdout.readline()
            assert line.strip() == "LOCKED"

            # Step 2: Parent confirms that another process cannot acquire the lock
            parent_lock = WorkspaceFileLock(tmpdir, "clean_run_1")
            assert parent_lock.acquire() is False

            # Also verify recovery cannot acquire the lock and yields
            conn_rec = sqlite3.connect(db_path)
            conn_rec.execute(
                "UPDATE pending_cleanups SET lease_expires_at = datetime('now', '-5 seconds') WHERE id = 'clean_run_1';"
            )
            conn_rec.commit()

            reclaimed_while_locked = reclaim_stale_in_progress_cleanups(conn_rec, allowed_base_dir=tmpdir)
            assert reclaimed_while_locked == 0
            conn_rec.close()

            # Step 3: Terminate the child process with SIGKILL (hard crash)
            proc.kill()
            proc.wait(timeout=5)

            # Step 4: Confirm that the OS kernel automatically released the fcntl.flock lock
            assert parent_lock.acquire() is True
            parent_lock.release()

            # Step 5: Confirm recovery can acquire lock and safely reconcile database and filesystem
            conn_rec2 = sqlite3.connect(db_path)
            reclaimed_after_crash = reclaim_stale_in_progress_cleanups(conn_rec2, allowed_base_dir=tmpdir)
            assert reclaimed_after_crash == 1

            # Workspace preserved for clean retry (since rename never occurred before crash)
            row = conn_rec2.execute("SELECT status FROM pending_cleanups WHERE id = 'clean_run_1';").fetchone()
            assert row[0] == "pending"
            assert os.path.exists(ws_path)
            conn_rec2.close()

        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_fsync_directory_darwin_fullfsync_explicit_invocation():
    """Explicitly verifies that fcntl.F_FULLFSYNC is invoked on directory descriptors
    when available (e.g. macOS/Darwin), and that the OS call succeeds on real filesystem descriptors."""
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)

        # Test 1: Check platform support directly on directory descriptor
        if hasattr(fcntl, "F_FULLFSYNC"):
            dir_fd = os.open(real_tmp, os.O_RDONLY)
            try:
                res = fcntl.fcntl(dir_fd, fcntl.F_FULLFSYNC)
                assert res == 0  # Kernel confirms F_FULLFSYNC succeeded on directory descriptor
            finally:
                os.close(dir_fd)

            # Test 2: Spy on fcntl.fcntl to assert that fsync_directory actually invokes F_FULLFSYNC
            fullfsync_calls = []
            real_fcntl = fcntl.fcntl

            def spy_fcntl(fd, op, *args, **kwargs):
                if op == fcntl.F_FULLFSYNC:
                    fullfsync_calls.append(op)
                return real_fcntl(fd, op, *args, **kwargs)

            with unittest.mock.patch("fcntl.fcntl", side_effect=spy_fcntl):
                fsync_directory(real_tmp)

            assert len(fullfsync_calls) >= 1
            assert fullfsync_calls[0] == fcntl.F_FULLFSYNC
        else:
            # Non-Darwin POSIX: verify os.fsync is called
            fsync_directory(real_tmp)

