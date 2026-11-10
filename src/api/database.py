"""Centralized SQLite Persistence Foundation for Dependency Upgrade Assistant 2.0."""

import contextlib
import os
import sqlite3
from pathlib import Path
from typing import Generator, Optional, Tuple
from src.api.config import settings


# Core DDL from approved architecture
SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Connected Repositories
CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('github', 'local')),
    url_or_path TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    cloud_llm_consent INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Analysis Runs
CREATE TABLE IF NOT EXISTS analysis_runs (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('created', 'queued', 'running', 'awaiting_approval', 'completed', 'failed', 'cancelled')),
    current_stage TEXT NOT NULL,
    base_commit_hash TEXT,
    head_commit_sha TEXT,
    workspace_dir TEXT,
    proposal_file TEXT,
    proposal_sha256 TEXT,
    patch_file TEXT,
    patch_sha256 TEXT,
    notes TEXT,
    idempotency_key TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error_message TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_runs_idempotency 
    ON analysis_runs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_run_per_repo 
    ON analysis_runs(repository_id) WHERE status IN ('queued', 'running', 'awaiting_approval');

-- Persistent Cleanup Queue (Durable Lock-First Recovery)
CREATE TABLE IF NOT EXISTS pending_cleanups (
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
"""


def get_db_path(override_path: Optional[str] = None) -> str:
    """Return the configured database path."""
    return override_path if override_path is not None else settings.DATABASE_PATH


def create_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Create and configure a new SQLite connection with WAL mode and foreign keys."""
    resolved_path = get_db_path(db_path)
    
    # Ensure parent directory exists if path contains directories
    parent_dir = os.path.dirname(resolved_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    conn = sqlite3.connect(resolved_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


@contextlib.contextmanager
def get_db_connection(db_path: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for SQLite connections ensuring proper commit and closing."""
    conn = create_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize SQLite database with schema tables and pragmas."""
    with get_db_connection(db_path) as conn:
        conn.executescript(SCHEMA_SQL)


def check_db_connectivity(db_path: Optional[str] = None) -> Tuple[bool, str]:
    """Verify live SQLite connectivity with a real probe query."""
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1;")
            result = cursor.fetchone()
            if result and result[0] == 1:
                return True, "connected"
            return False, "unexpected query result"
    except Exception as e:
        return False, f"database connection error: {str(e)}"
