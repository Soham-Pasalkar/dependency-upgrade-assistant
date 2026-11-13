"""Verification tests for SQLite concurrency, partial unique index, atomic force_supersede,
and rigorous 6-point integrity verification for awaiting_approval runs.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from typing import Optional, Tuple
import pytest


def init_test_db(conn: sqlite3.Connection):
    """Initialize test schema matching architecture specification."""
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("""
    CREATE TABLE repositories (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        url TEXT NOT NULL
    );
    """)
    conn.execute("""
    CREATE TABLE analysis_runs (
        id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL REFERENCES repositories(id),
        status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'awaiting_approval', 'approved', 'rejected', 'completed', 'failed', 'cancelled')),
        base_commit_hash TEXT,
        workspace_dir TEXT,
        proposal_file TEXT,
        proposal_sha256 TEXT,
        patch_file TEXT,
        patch_sha256 TEXT,
        cancellation_reason TEXT,
        error_message TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP
    );
    """)
    # Crucial partial unique index specified in architecture
    conn.execute("""
    CREATE UNIQUE INDEX idx_one_active_run_per_repo ON analysis_runs(repository_id) 
    WHERE status IN ('queued', 'running', 'awaiting_approval');
    """)
    conn.commit()


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    init_test_db(conn)
    conn.execute("INSERT INTO repositories (id, name, url) VALUES ('repo_1', 'test-repo', 'https://github.com/test/repo');")
    conn.commit()
    yield conn
    conn.close()


def test_concurrent_run_creation_fails_with_conflict(db):
    """Verify that multiple active runs cannot exist concurrently for the same repository."""
    db.execute("INSERT INTO analysis_runs (id, repository_id, status) VALUES ('run_1', 'repo_1', 'running');")
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        db.execute("INSERT INTO analysis_runs (id, repository_id, status) VALUES ('run_2', 'repo_1', 'queued');")


def test_completed_runs_do_not_block_new_active_runs(db):
    """Verify that terminal runs (completed, failed, cancelled) do not trigger partial unique index."""
    db.execute("INSERT INTO analysis_runs (id, repository_id, status) VALUES ('run_1', 'repo_1', 'completed');")
    db.execute("INSERT INTO analysis_runs (id, repository_id, status) VALUES ('run_2', 'repo_1', 'failed');")
    db.execute("INSERT INTO analysis_runs (id, repository_id, status) VALUES ('run_3', 'repo_1', 'cancelled');")
    db.commit()

    db.execute("INSERT INTO analysis_runs (id, repository_id, status) VALUES ('run_4', 'repo_1', 'running');")
    db.commit()

    cursor = db.execute("SELECT count(*) FROM analysis_runs WHERE repository_id = 'repo_1';")
    assert cursor.fetchone()[0] == 4


def force_supersede_run(conn: sqlite3.Connection, repo_id: str, new_run_id: str, inject_failure: bool = False):
    """Simulates the atomic force_supersede transaction."""
    conn.commit()
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE;")
    try:
        cursor.execute(
            "SELECT id FROM analysis_runs WHERE repository_id = ? AND status = 'awaiting_approval';",
            (repo_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError("No awaiting_approval run to supersede")
        old_run_id = row[0]

        cursor.execute(
            "UPDATE analysis_runs SET status = 'cancelled', cancellation_reason = 'SUPERSEDED' WHERE id = ?;",
            (old_run_id,)
        )

        if inject_failure:
            raise RuntimeError("Simulated failure during insertion")

        cursor.execute(
            "INSERT INTO analysis_runs (id, repository_id, status) VALUES (?, ?, 'queued');",
            (new_run_id, repo_id)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def test_force_supersede_atomic_success(db):
    """Verify force_supersede atomically transitions awaiting_approval to cancelled and creates queued run."""
    db.execute("INSERT INTO analysis_runs (id, repository_id, status) VALUES ('run_old', 'repo_1', 'awaiting_approval');")
    db.commit()

    force_supersede_run(db, 'repo_1', 'run_new')

    cursor = db.execute("SELECT id, status, cancellation_reason FROM analysis_runs WHERE id = 'run_old';")
    old_run = cursor.fetchone()
    assert old_run == ('run_old', 'cancelled', 'SUPERSEDED')

    cursor = db.execute("SELECT id, status FROM analysis_runs WHERE id = 'run_new';")
    new_run = cursor.fetchone()
    assert new_run == ('run_new', 'queued')


def test_force_supersede_atomic_rollback_on_failure(db):
    """Verify that a failure during force_supersede preserves the prior run in awaiting_approval."""
    db.execute("INSERT INTO analysis_runs (id, repository_id, status) VALUES ('run_old', 'repo_1', 'awaiting_approval');")
    db.commit()

    with pytest.raises(RuntimeError, match="Simulated failure during insertion"):
        force_supersede_run(db, 'repo_1', 'run_new', inject_failure=True)

    cursor = db.execute("SELECT id, status, cancellation_reason FROM analysis_runs WHERE id = 'run_old';")
    old_run = cursor.fetchone()
    assert old_run == ('run_old', 'awaiting_approval', None)

    cursor = db.execute("SELECT id FROM analysis_runs WHERE id = 'run_new';")
    assert cursor.fetchone() is None


def verify_awaiting_approval_integrity(
    workspace_dir: Optional[str],
    proposal_file: Optional[str],
    proposal_sha256: Optional[str],
    patch_file: Optional[str],
    patch_sha256: Optional[str],
    expected_repo_id: str,
    expected_base_commit: str,
) -> Tuple[bool, str]:
    """Rigorous 6-point integrity verification for resuming an awaiting_approval run."""
    # 1. Structure Check
    if not workspace_dir or not os.path.isdir(workspace_dir):
        return False, "INTEGRITY_CHECK_FAILED: missing_workspace_dir"
    ref_dir = os.path.join(workspace_dir, "reference")
    work_dir = os.path.join(workspace_dir, "working")
    if not os.path.isdir(ref_dir) or not os.path.isdir(work_dir):
        return False, "INTEGRITY_CHECK_FAILED: invalid_workspace_structure"
    if not proposal_file or not os.path.isfile(proposal_file):
        return False, "INTEGRITY_CHECK_FAILED: missing_proposal"
    if not patch_file or not os.path.isfile(patch_file):
        return False, "INTEGRITY_CHECK_FAILED: missing_patch"

    # 2. SHA-256 Check
    with open(proposal_file, "rb") as f:
        actual_prop_sha = hashlib.sha256(f.read()).hexdigest()
    if actual_prop_sha != proposal_sha256:
        return False, "INTEGRITY_CHECK_FAILED: proposal_hash_mismatch"

    with open(patch_file, "rb") as f:
        actual_patch_sha = hashlib.sha256(f.read()).hexdigest()
    if actual_patch_sha != patch_sha256:
        return False, "INTEGRITY_CHECK_FAILED: patch_hash_mismatch"

    # 3. Repository Identity Check
    repo_meta_file = os.path.join(workspace_dir, ".dua_repo_id")
    if not os.path.exists(repo_meta_file):
        return False, "INTEGRITY_CHECK_FAILED: repository_identity_mismatch"
    with open(repo_meta_file, "r") as f:
        actual_repo_id = f.read().strip()
    if actual_repo_id != expected_repo_id:
        return False, "INTEGRITY_CHECK_FAILED: repository_identity_mismatch"

    # 4. Baseline Commit Check
    commit_meta_file = os.path.join(workspace_dir, ".dua_base_commit")
    if not os.path.exists(commit_meta_file):
        return False, "INTEGRITY_CHECK_FAILED: changed_baseline_commit"
    with open(commit_meta_file, "r") as f:
        actual_commit = f.read().strip()
    if actual_commit != expected_base_commit:
        return False, "INTEGRITY_CHECK_FAILED: changed_baseline_commit"

    # 5. Mutual Consistency between Proposal and Patch
    try:
        with open(proposal_file, "r") as f:
            proposal_data = json.load(f)
        modified_files_in_prop = set(proposal_data.get("modified_files", []))
    except Exception:
        return False, "INTEGRITY_CHECK_FAILED: malformed_proposal_json"

    with open(patch_file, "r") as f:
        patch_text = f.read()
    patch_target_files = set()
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            patch_target_files.add(line[6:].strip())

    if not patch_target_files.issubset(modified_files_in_prop):
        return False, "INTEGRITY_CHECK_FAILED: proposal_patch_inconsistent"

    # 6. Working Copy Validity
    valid_flag = os.path.join(work_dir, ".working_copy_valid")
    if not os.path.exists(valid_flag):
        return False, "INTEGRITY_CHECK_FAILED: invalid_working_copy"

    return True, "Resumed after restart"


def run_strong_startup_recovery(conn: sqlite3.Connection):
    """Executes startup recovery using strong integrity validation."""
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, repository_id, status, base_commit_hash, workspace_dir,
           proposal_file, proposal_sha256, patch_file, patch_sha256
    FROM analysis_runs
    WHERE status IN ('queued', 'running', 'awaiting_approval');
    """)
    active_runs = cursor.fetchall()

    for (run_id, repo_id, status, base_commit, ws_dir,
         prop_file, prop_sha, patch_file, patch_sha) in active_runs:
        if status == 'queued':
            cursor.execute(
                "UPDATE analysis_runs SET status = 'failed', error_message = 'Interrupted: server restarted before execution began' WHERE id = ?;",
                (run_id,)
            )
        elif status == 'running':
            cursor.execute(
                "UPDATE analysis_runs SET status = 'failed', error_message = 'Interrupted: server restarted during pipeline execution' WHERE id = ?;",
                (run_id,)
            )
        elif status == 'awaiting_approval':
            is_valid, msg = verify_awaiting_approval_integrity(
                workspace_dir=ws_dir,
                proposal_file=prop_file,
                proposal_sha256=prop_sha,
                patch_file=patch_file,
                patch_sha256=patch_sha,
                expected_repo_id=repo_id,
                expected_base_commit=base_commit,
            )
            if is_valid:
                cursor.execute(
                    "UPDATE analysis_runs SET notes = ? WHERE id = ?;",
                    (msg, run_id)
                )
            else:
                cursor.execute(
                    "UPDATE analysis_runs SET status = 'failed', error_message = ? WHERE id = ?;",
                    (msg, run_id)
                )
    conn.commit()


def setup_intact_workspace(base_dir: str, repo_id: str = "repo_1", base_commit: str = "abc1234") -> dict:
    """Helper to scaffold a fully intact workspace passing all 6 integrity checks."""
    ws = os.path.join(base_dir, "ws_intact")
    os.makedirs(os.path.join(ws, "reference"), exist_ok=True)
    work = os.path.join(ws, "working")
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, ".working_copy_valid"), "w") as f: f.write("ok")

    with open(os.path.join(ws, ".dua_repo_id"), "w") as f: f.write(repo_id)
    with open(os.path.join(ws, ".dua_base_commit"), "w") as f: f.write(base_commit)

    prop_path = os.path.join(ws, "proposal.json")
    prop_content = json.dumps({"modified_files": ["requirements.txt"]})
    with open(prop_path, "w") as f: f.write(prop_content)
    prop_sha = hashlib.sha256(prop_content.encode("utf-8")).hexdigest()

    patch_path = os.path.join(ws, "patch.diff")
    patch_content = "--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1 @@\n-requests==2.28.0\n+requests==2.31.0\n"
    with open(patch_path, "w") as f: f.write(patch_content)
    patch_sha = hashlib.sha256(patch_content.encode("utf-8")).hexdigest()

    return {
        "workspace_dir": ws,
        "proposal_file": prop_path,
        "proposal_sha256": prop_sha,
        "patch_file": patch_path,
        "patch_sha256": patch_sha,
        "repo_id": repo_id,
        "base_commit": base_commit
    }


def test_recovery_fully_intact_approval_run(db):
    """Verify that a fully intact workspace passes all 6 integrity checks and remains awaiting_approval."""
    with tempfile.TemporaryDirectory() as tmpdir:
        meta = setup_intact_workspace(tmpdir)
        db.execute("""
        INSERT INTO analysis_runs (
            id, repository_id, status, base_commit_hash, workspace_dir,
            proposal_file, proposal_sha256, patch_file, patch_sha256
        ) VALUES ('run_intact', ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?);
        """, (
            meta["repo_id"], meta["base_commit"], meta["workspace_dir"],
            meta["proposal_file"], meta["proposal_sha256"],
            meta["patch_file"], meta["patch_sha256"]
        ))
        db.commit()

        run_strong_startup_recovery(db)

        row = db.execute("SELECT status, notes, error_message FROM analysis_runs WHERE id = 'run_intact';").fetchone()
        assert row[0] == "awaiting_approval"
        assert row[1] == "Resumed after restart"
        assert row[2] is None


def test_recovery_fails_on_missing_workspace(db):
    """Verify failure when workspace_dir does not exist on disk."""
    db.execute("""
    INSERT INTO analysis_runs (id, repository_id, status, base_commit_hash, workspace_dir, proposal_file, patch_file)
    VALUES ('run_no_ws', 'repo_1', 'awaiting_approval', 'abc1234', '/nonexistent/ws', '/nonexistent/prop', '/nonexistent/patch');
    """)
    db.commit()

    run_strong_startup_recovery(db)

    row = db.execute("SELECT status, error_message FROM analysis_runs WHERE id = 'run_no_ws';").fetchone()
    assert row[0] == "failed"
    assert "missing_workspace_dir" in row[1]


def test_recovery_fails_on_missing_proposal(db):
    """Verify failure when proposal.json is deleted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        meta = setup_intact_workspace(tmpdir)
        os.unlink(meta["proposal_file"]) # Delete proposal

        db.execute("""
        INSERT INTO analysis_runs (id, repository_id, status, base_commit_hash, workspace_dir, proposal_file, proposal_sha256, patch_file, patch_sha256)
        VALUES ('run_no_prop', ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?);
        """, (meta["repo_id"], meta["base_commit"], meta["workspace_dir"], meta["proposal_file"], meta["proposal_sha256"], meta["patch_file"], meta["patch_sha256"]))
        db.commit()

        run_strong_startup_recovery(db)

        row = db.execute("SELECT status, error_message FROM analysis_runs WHERE id = 'run_no_prop';").fetchone()
        assert row[0] == "failed"
        assert "missing_proposal" in row[1]


def test_recovery_fails_on_missing_patch(db):
    """Verify failure when patch.diff is deleted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        meta = setup_intact_workspace(tmpdir)
        os.unlink(meta["patch_file"]) # Delete patch

        db.execute("""
        INSERT INTO analysis_runs (id, repository_id, status, base_commit_hash, workspace_dir, proposal_file, proposal_sha256, patch_file, patch_sha256)
        VALUES ('run_no_patch', ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?);
        """, (meta["repo_id"], meta["base_commit"], meta["workspace_dir"], meta["proposal_file"], meta["proposal_sha256"], meta["patch_file"], meta["patch_sha256"]))
        db.commit()

        run_strong_startup_recovery(db)

        row = db.execute("SELECT status, error_message FROM analysis_runs WHERE id = 'run_no_patch';").fetchone()
        assert row[0] == "failed"
        assert "missing_patch" in row[1]


def test_recovery_fails_on_modified_proposal_hash_mismatch(db):
    """Verify failure when proposal.json was modified after recording its hash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        meta = setup_intact_workspace(tmpdir)
        # Tamper with proposal content
        with open(meta["proposal_file"], "w") as f:
            f.write(json.dumps({"modified_files": ["requirements.txt", "injected_backdoor.py"]}))

        db.execute("""
        INSERT INTO analysis_runs (id, repository_id, status, base_commit_hash, workspace_dir, proposal_file, proposal_sha256, patch_file, patch_sha256)
        VALUES ('run_tamper_prop', ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?);
        """, (meta["repo_id"], meta["base_commit"], meta["workspace_dir"], meta["proposal_file"], meta["proposal_sha256"], meta["patch_file"], meta["patch_sha256"]))
        db.commit()

        run_strong_startup_recovery(db)

        row = db.execute("SELECT status, error_message FROM analysis_runs WHERE id = 'run_tamper_prop';").fetchone()
        assert row[0] == "failed"
        assert "proposal_hash_mismatch" in row[1]


def test_recovery_fails_on_modified_patch_hash_mismatch(db):
    """Verify failure when patch.diff was modified after recording its hash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        meta = setup_intact_workspace(tmpdir)
        # Tamper with patch content
        with open(meta["patch_file"], "w") as f:
            f.write("--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1 @@\n-requests\n+malicious-pkg\n")

        db.execute("""
        INSERT INTO analysis_runs (id, repository_id, status, base_commit_hash, workspace_dir, proposal_file, proposal_sha256, patch_file, patch_sha256)
        VALUES ('run_tamper_patch', ?, 'awaiting_approval', ?, ?, ?, ?, ?, ?);
        """, (meta["repo_id"], meta["base_commit"], meta["workspace_dir"], meta["proposal_file"], meta["proposal_sha256"], meta["patch_file"], meta["patch_sha256"]))
        db.commit()

        run_strong_startup_recovery(db)

        row = db.execute("SELECT status, error_message FROM analysis_runs WHERE id = 'run_tamper_patch';").fetchone()
        assert row[0] == "failed"
        assert "patch_hash_mismatch" in row[1]


def test_recovery_fails_on_changed_baseline_commit(db):
    """Verify failure when workspace base commit hash does not match run record."""
    with tempfile.TemporaryDirectory() as tmpdir:
        meta = setup_intact_workspace(tmpdir, base_commit="original_sha123")
        # Tamper with workspace base commit metadata
        with open(os.path.join(meta["workspace_dir"], ".dua_base_commit"), "w") as f:
            f.write("different_sha456")

        db.execute("""
        INSERT INTO analysis_runs (id, repository_id, status, base_commit_hash, workspace_dir, proposal_file, proposal_sha256, patch_file, patch_sha256)
        VALUES ('run_changed_commit', ?, 'awaiting_approval', 'original_sha123', ?, ?, ?, ?, ?);
        """, (meta["repo_id"], meta["workspace_dir"], meta["proposal_file"], meta["proposal_sha256"], meta["patch_file"], meta["patch_sha256"]))
        db.commit()

        run_strong_startup_recovery(db)

        row = db.execute("SELECT status, error_message FROM analysis_runs WHERE id = 'run_changed_commit';").fetchone()
        assert row[0] == "failed"
        assert "changed_baseline_commit" in row[1]


def test_recovery_fails_on_repository_identity_mismatch(db):
    """Verify failure when workspace repo identity does not match run repository_id."""
    with tempfile.TemporaryDirectory() as tmpdir:
        meta = setup_intact_workspace(tmpdir, repo_id="foreign_repo")

        db.execute("""
        INSERT INTO analysis_runs (id, repository_id, status, base_commit_hash, workspace_dir, proposal_file, proposal_sha256, patch_file, patch_sha256)
        VALUES ('run_repo_mismatch', 'repo_1', 'awaiting_approval', ?, ?, ?, ?, ?, ?);
        """, (meta["base_commit"], meta["workspace_dir"], meta["proposal_file"], meta["proposal_sha256"], meta["patch_file"], meta["patch_sha256"]))
        db.commit()

        run_strong_startup_recovery(db)

        row = db.execute("SELECT status, error_message FROM analysis_runs WHERE id = 'run_repo_mismatch';").fetchone()
        assert row[0] == "failed"
        assert "repository_identity_mismatch" in row[1]
