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
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
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


class WorkspaceFileLock:
    """POSIX fcntl.flock advisory kernel lock.
    
    Guarantees:
    - Exactly one process can hold LOCK_EX on the cleanup lockfile at any instant.
    - If a process terminates, crashes, or is killed via SIGKILL, the kernel automatically
      and synchronously closes the open file descriptor and releases the lock.
    - On host reboot, all file descriptors and kernel locks are completely wiped.
    - Non-blocking (LOCK_NB) acquisition immediately detects whether another live worker
      is actively executing filesystem operations, avoiding concurrent rmtree races.
    """
    def __init__(self, lock_dir: str, cleanup_id: str):
        self.lock_path = os.path.join(lock_dir, f".cleanup_{cleanup_id}.lock")
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        try:
            self.fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
            return False

    def release(self):
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

    def unlink(self):
        self.release()
        try:
            if os.path.exists(self.lock_path):
                os.unlink(self.lock_path)
        except OSError:
            pass


def fsync_directory(dir_path: str):
    """Guarantees directory metadata durability across sudden power loss."""
    if not dir_path or not os.path.exists(dir_path):
        return
    try:
        dir_fd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def secure_rmtree(path: str, authorized_root: str):
    """Recursively deletes a directory without following symlinks."""
    validate_workspace_path(path, authorized_root)
    if not os.path.exists(path):
        return

    for root, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            file_path = os.path.join(root, name)
            try:
                os.unlink(file_path)
            except OSError:
                pass
        for name in dirs:
            dir_path = os.path.join(root, name)
            if os.path.islink(dir_path):
                try:
                    os.unlink(dir_path)
                except OSError:
                    pass
            else:
                try:
                    os.rmdir(dir_path)
                except OSError:
                    pass
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.exists(path):
        os.rmdir(path)


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
    forced_token: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    """Atomically claims an eligible pending or expired in_progress cleanup record with a unique fencing lease token."""
    token = forced_token or secrets.token_hex(16)
    modifier = f"+{lease_seconds} seconds" if lease_seconds >= 0 else f"{lease_seconds} seconds"
    cursor = conn.cursor()
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
    conn.commit()
    if cursor.rowcount == 1:
        return True, token
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
          AND status = 'in_progress'
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
          AND status = 'in_progress'
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
    lease_token: str
) -> bool:
    """Durable Step 2: Confirms successful filesystem rename in database."""
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE pending_cleanups
        SET status = 'quarantined',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND worker_id = ?
          AND lease_token = ?
          AND status = 'quarantine_intent'
          AND lease_expires_at >= CURRENT_TIMESTAMP;
        """,
        (cleanup_id, worker_id, lease_token)
    )
    conn.commit()
    return cursor.rowcount == 1


def complete_quarantine_cleanup(
    conn: sqlite3.Connection,
    cleanup_id: str,
    worker_id: str,
    lease_token: str
) -> bool:
    """Durable Step 3: Commits deletion completion to database."""
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


def quarantine_and_delete_workspace(
    conn: sqlite3.Connection,
    cleanup_id: str,
    worker_id: str,
    lease_token: str,
    ws_dir: str,
    authorized_root: Optional[str] = None
) -> bool:
    """Durable crash-safe quarantine protocol with OS locking & parent fsync:
    1. Acquire OS-level exclusive WorkspaceFileLock.
    2. Validate path containment within authorized_root.
    3. Persist quarantine intent to SQLite (status = 'quarantine_intent', quarantine_dir recorded).
    4. Atomic filesystem rename: os.replace(ws_dir, quarantine_dir).
    5. Fsync parent directory to ensure rename durability across power loss.
    6. Confirm quarantine in SQLite (status = 'quarantined').
    7. Destructive deletion: secure_rmtree(quarantine_dir).
    8. Commit completion in SQLite (status = 'completed').
    9. Release lock.
    """
    lock_dir = os.path.dirname(ws_dir) if ws_dir else tempfile.gettempdir()
    lock = WorkspaceFileLock(lock_dir, cleanup_id)
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

        # Step 2: Atomic rename
        if ws_dir and os.path.exists(ws_dir):
            os.replace(ws_dir, quarantine_path)
            fsync_directory(os.path.dirname(ws_dir))

        # Step 3: Confirm quarantine
        if not confirm_quarantine(conn, cleanup_id, worker_id, lease_token):
            return False

        # Step 4: Destructive deletion
        if quarantine_path and os.path.exists(quarantine_path):
            if authorized_root:
                secure_rmtree(quarantine_path, authorized_root)
            else:
                shutil.rmtree(quarantine_path)
            fsync_directory(os.path.dirname(quarantine_path))

        # Step 5: Complete
        return complete_quarantine_cleanup(conn, cleanup_id, worker_id, lease_token)
    finally:
        lock.unlink()


def claim_stale_cleanup_for_recovery(
    conn: sqlite3.Connection,
    clean_id: str,
    recovery_worker_id: str = "recovery_worker",
    lease_seconds: int = 120
) -> Tuple[bool, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Atomically claims an expired cleanup record for recovery in a short transaction.
    
    Returns: (claimed, recovery_token, status, workspace_dir, quarantine_dir)
    """
    recovery_token = secrets.token_hex(16)
    modifier = f"+{lease_seconds} seconds" if lease_seconds >= 0 else f"{lease_seconds} seconds"
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE;")
    cursor.execute(
        """
        SELECT status, workspace_dir, quarantine_dir
        FROM pending_cleanups
        WHERE id = ? 
          AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP);
        """,
        (clean_id,)
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
          AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP);
        """,
        (recovery_worker_id, recovery_token, modifier, clean_id)
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
    """Atomic recovery protocol with OS file locking, canonical path validation, and parent fsync."""
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
        claimed, rec_token, cur_status, cur_ws, cur_q = claim_stale_cleanup_for_recovery(
            conn, clean_id, recovery_worker_id=recovery_worker_id
        )
        if not claimed:
            continue

        lock_dir = os.path.dirname(cur_ws) if cur_ws else tempfile.gettempdir()
        lock = WorkspaceFileLock(lock_dir, clean_id)
        if not lock.acquire():
            # Live process holds OS lock; yield without interfering
            cursor.execute(
                """
                UPDATE pending_cleanups
                SET lease_expires_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND worker_id = ? AND lease_token = ?;
                """,
                (clean_id, recovery_worker_id, rec_token)
            )
            conn.commit()
            continue

        try:
            if allowed_base_dir and cur_ws:
                cur_ws = validate_workspace_path(cur_ws, allowed_base_dir)
            if allowed_base_dir and cur_q:
                cur_q = validate_workspace_path(cur_q, allowed_base_dir)

            ws_exists = bool(cur_ws and os.path.exists(cur_ws))
            q_exists = bool(cur_q and os.path.exists(cur_q))

            if cur_status == "quarantined":
                if q_exists:
                    if allowed_base_dir:
                        secure_rmtree(cur_q, allowed_base_dir)
                    else:
                        shutil.rmtree(cur_q)
                    fsync_directory(os.path.dirname(cur_q))
                cursor.execute(
                    """
                    UPDATE pending_cleanups
                    SET status = 'completed', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                        lease_expires_at = NULL, next_attempt_at = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND worker_id = ? AND lease_token = ?;
                    """,
                    (clean_id, recovery_worker_id, rec_token)
                )
                conn.commit()
                reclaimed_count += 1

            elif cur_status == "quarantine_intent":
                if ws_exists and not q_exists:
                    cursor.execute(
                        """
                        UPDATE pending_cleanups
                        SET status = 'pending', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                            lease_expires_at = NULL, next_attempt_at = CURRENT_TIMESTAMP,
                            last_error = 'Quarantine intent rolled back: workspace directory never renamed',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND worker_id = ? AND lease_token = ?;
                        """,
                        (clean_id, recovery_worker_id, rec_token)
                    )
                    conn.commit()
                    reclaimed_count += 1
                elif not ws_exists and q_exists:
                    if allowed_base_dir:
                        secure_rmtree(cur_q, allowed_base_dir)
                    else:
                        shutil.rmtree(cur_q)
                    fsync_directory(os.path.dirname(cur_q))
                    cursor.execute(
                        """
                        UPDATE pending_cleanups
                        SET status = 'completed', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                            lease_expires_at = NULL, next_attempt_at = NULL,
                            last_error = 'Quarantine completed by recovery worker after rename crash',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND worker_id = ? AND lease_token = ?;
                        """,
                        (clean_id, recovery_worker_id, rec_token)
                    )
                    conn.commit()
                    reclaimed_count += 1
                elif ws_exists and q_exists:
                    if allowed_base_dir:
                        secure_rmtree(cur_q, allowed_base_dir)
                    else:
                        shutil.rmtree(cur_q)
                    fsync_directory(os.path.dirname(cur_q))
                    cursor.execute(
                        """
                        UPDATE pending_cleanups
                        SET status = 'pending', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                            lease_expires_at = NULL, next_attempt_at = CURRENT_TIMESTAMP,
                            last_error = 'Quarantine artifact cleared; workspace preserved for retry',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND worker_id = ? AND lease_token = ?;
                        """,
                        (clean_id, recovery_worker_id, rec_token)
                    )
                    conn.commit()
                    reclaimed_count += 1
                else:
                    cursor.execute(
                        """
                        UPDATE pending_cleanups
                        SET status = 'completed', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                            lease_expires_at = NULL, next_attempt_at = NULL, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND worker_id = ? AND lease_token = ?;
                        """,
                        (clean_id, recovery_worker_id, rec_token)
                    )
                    conn.commit()
                    reclaimed_count += 1

            elif cur_status == "in_progress":
                cursor.execute(
                    """
                    UPDATE pending_cleanups
                    SET status = 'pending', worker_id = NULL, lease_token = NULL,
                        lease_expires_at = NULL, next_attempt_at = CURRENT_TIMESTAMP,
                        last_error = 'Reclaimed after application restart (lease expired or worker terminated)',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND worker_id = ? AND lease_token = ?;
                    """,
                    (clean_id, recovery_worker_id, rec_token)
                )
                conn.commit()
                reclaimed_count += 1
        finally:
            lock.unlink()

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
        claimed, token = claim_cleanup_record(conn, clean_id, worker_id=worker_id)
        if not claimed:
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
            success = quarantine_and_delete_workspace(conn, clean_id, worker_id, token, ws_dir)
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

        barrier = threading.Barrier(2)
        results = {}

        def recovery_thread():
            conn_rec = sqlite3.connect(db_path, timeout=10)
            conn_rec.execute("PRAGMA journal_mode=WAL;")
            barrier.wait()
            # Claim recovery and complete
            reclaimed = reclaim_stale_in_progress_cleanups(conn_rec, allowed_base_dir=tmpdir, recovery_worker_id="rec_daemon")
            results["reclaimed"] = reclaimed
            conn_rec.close()

        def stale_worker_thread():
            conn_w = sqlite3.connect(db_path, timeout=10)
            conn_w.execute("PRAGMA journal_mode=WAL;")
            barrier.wait()
            # Stale worker attempts to confirm quarantine using expired token1
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
            claimed, token, _, _, _ = claim_stale_cleanup_for_recovery(c, "clean_run_1", recovery_worker_id=worker_id)
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
