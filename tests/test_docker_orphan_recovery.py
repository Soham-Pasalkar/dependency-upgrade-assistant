"""Verification tests for persistent Docker cleanup recovery, durable state transitions,
lease fencing tokens, atomic quarantine protocol (eliminating TOCTOU races), crash reclamation,
persistent retry scheduling (next_attempt_at), non-cascading FK preservation, and idempotency.
"""

import os
import secrets
import shutil
import sqlite3
import tempfile
from typing import Dict, List, Optional, Tuple
import pytest


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
    ws_dir: str
) -> bool:
    """Durable crash-safe quarantine protocol:
    1. Persist quarantine intent to SQLite (status = 'quarantine_intent', quarantine_dir recorded).
    2. Atomic filesystem rename: os.replace(ws_dir, quarantine_dir).
    3. Confirm quarantine in SQLite (status = 'quarantined').
    4. Destructive deletion: shutil.rmtree(quarantine_dir).
    5. Commit completion in SQLite (status = 'completed').
    """
    # Step 1: Persist intent
    ok, quarantine_path = record_quarantine_intent(conn, cleanup_id, worker_id, lease_token, ws_dir)
    if not ok or not quarantine_path:
        return False

    # Step 2: Atomic rename
    if ws_dir and os.path.exists(ws_dir):
        os.replace(ws_dir, quarantine_path)

    # Step 3: Confirm quarantine
    if not confirm_quarantine(conn, cleanup_id, worker_id, lease_token):
        return False

    # Step 4: Destructive deletion
    if quarantine_path and os.path.exists(quarantine_path):
        shutil.rmtree(quarantine_path)

    # Step 5: Complete
    return complete_quarantine_cleanup(conn, cleanup_id, worker_id, lease_token)


def reclaim_stale_in_progress_cleanups(conn: sqlite3.Connection, allowed_base_dir: Optional[str] = None) -> int:
    """Reclaims stale in_progress and quarantine_intent records, and recovers orphaned quarantined directories.
    
    Crash consistency matrix:
    - status = 'quarantined':
        - If q_dir exists: deletes q_dir, marks status = 'completed'.
        - If q_dir does not exist: marks status = 'completed'.
    - status = 'quarantine_intent':
        - If orig_dir exists and q_dir does not: rename never happened before crash.
          Resets status = 'pending', quarantine_dir = NULL so next worker can retry cleanly.
        - If orig_dir does not exist and q_dir exists: rename completed before crash.
          Deletes q_dir, marks status = 'completed'.
        - If both orig_dir and q_dir exist: q_dir is an incomplete quarantine artifact.
          Deletes q_dir, preserves orig_dir, resets status = 'pending', quarantine_dir = NULL.
        - If neither exists: directory already removed. Marks status = 'completed'.
    - status = 'in_progress':
        - Resets status = 'pending' if lease expired.
    """
    cursor = conn.cursor()
    reclaimed_count = 0

    # 1. Recover orphaned quarantined records
    cursor.execute(
        """
        SELECT id, workspace_dir, quarantine_dir 
        FROM pending_cleanups 
        WHERE status = 'quarantined'
          AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP);
        """
    )
    quarantined = cursor.fetchall()
    for clean_id, ws_dir, q_dir in quarantined:
        if q_dir:
            # Validate path safety
            if allowed_base_dir and not os.path.abspath(q_dir).startswith(os.path.abspath(allowed_base_dir)):
                continue
            if ".quarantine_" in q_dir and os.path.exists(q_dir):
                shutil.rmtree(q_dir)
        cursor.execute(
            """
            UPDATE pending_cleanups
            SET status = 'completed', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                lease_expires_at = NULL, next_attempt_at = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?;
            """,
            (clean_id,)
        )
        reclaimed_count += 1

    # 2. Recover orphaned quarantine_intent records
    cursor.execute(
        """
        SELECT id, workspace_dir, quarantine_dir 
        FROM pending_cleanups 
        WHERE status = 'quarantine_intent'
          AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP);
        """
    )
    intent_records = cursor.fetchall()
    for clean_id, ws_dir, q_dir in intent_records:
        ws_exists = bool(ws_dir and os.path.exists(ws_dir))
        q_exists = bool(q_dir and os.path.exists(q_dir))

        if q_dir and allowed_base_dir and not os.path.abspath(q_dir).startswith(os.path.abspath(allowed_base_dir)):
            continue

        if ws_exists and not q_exists:
            # Case 1: Rename never happened before crash. Reset to pending.
            cursor.execute(
                """
                UPDATE pending_cleanups
                SET status = 'pending', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                    lease_expires_at = NULL, next_attempt_at = CURRENT_TIMESTAMP,
                    last_error = 'Quarantine intent rolled back: workspace directory never renamed',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (clean_id,)
            )
        elif not ws_exists and q_exists:
            # Case 2: Rename succeeded before crash. Finish deletion of q_dir and mark completed.
            if ".quarantine_" in q_dir and os.path.exists(q_dir):
                shutil.rmtree(q_dir)
            cursor.execute(
                """
                UPDATE pending_cleanups
                SET status = 'completed', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                    lease_expires_at = NULL, next_attempt_at = NULL,
                    last_error = 'Quarantine completed by recovery worker after rename crash',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (clean_id,)
            )
        elif ws_exists and q_exists:
            # Case 3: Both exist. Safely remove orphaned quarantine artifact, keep orig_dir, reset to pending.
            if ".quarantine_" in q_dir and os.path.exists(q_dir):
                shutil.rmtree(q_dir)
            cursor.execute(
                """
                UPDATE pending_cleanups
                SET status = 'pending', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                    lease_expires_at = NULL, next_attempt_at = CURRENT_TIMESTAMP,
                    last_error = 'Quarantine artifact cleared; workspace preserved for retry',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (clean_id,)
            )
        else:
            # Case 4: Neither exists. Already removed.
            cursor.execute(
                """
                UPDATE pending_cleanups
                SET status = 'completed', quarantine_dir = NULL, worker_id = NULL, lease_token = NULL,
                    lease_expires_at = NULL, next_attempt_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (clean_id,)
            )
        reclaimed_count += 1

    # 3. Reclaim expired in_progress records back to pending
    cursor.execute(
        """
        UPDATE pending_cleanups
        SET status = 'pending',
            worker_id = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            next_attempt_at = CURRENT_TIMESTAMP,
            last_error = 'Reclaimed after application restart (lease expired or worker terminated)',
            updated_at = CURRENT_TIMESTAMP
        WHERE status = 'in_progress' AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP);
        """
    )
    reclaimed_count += cursor.rowcount
    conn.commit()
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
