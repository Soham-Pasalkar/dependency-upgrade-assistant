"""M12 criterion 13: additive migration of an M11-era database; pre-M12 records stay intact and UNKNOWN."""
from __future__ import annotations

import sqlite3

from src.resolution.models import ResolutionRole
from src.services.run_store import RunStore

# Schema exactly as created by M11 (commit d8cb3b3), including its additive migrations.
M11_SCHEMA = """
CREATE TABLE dua_runs (id TEXT PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('github', 'manifest')),
    repository TEXT, workspace_dir TEXT, skipped TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL);
CREATE TABLE dua_run_manifests (run_id TEXT NOT NULL REFERENCES dua_runs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, path TEXT NOT NULL, manifest_type TEXT NOT NULL, kind TEXT NOT NULL,
    plannable INTEGER NOT NULL DEFAULT 1, note TEXT, error TEXT, content TEXT NOT NULL, analysis TEXT,
    recommendations TEXT, proposal_id TEXT, PRIMARY KEY (run_id, idx));
CREATE TABLE dua_proposals (proposal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES dua_runs(id) ON DELETE CASCADE, idx INTEGER NOT NULL,
    diff_sha256 TEXT NOT NULL, created_at REAL NOT NULL, proposal_json TEXT, consumed_at REAL);
CREATE TABLE dua_approvals (
    proposal_id TEXT PRIMARY KEY REFERENCES dua_proposals(proposal_id) ON DELETE CASCADE,
    diff_sha256 TEXT NOT NULL, note TEXT, approved_at REAL NOT NULL, validation_fingerprint TEXT);
"""



def _tables(db):
    with sqlite3.connect(db) as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}


def test_m11_database_migrates_additively(tmp_path):
    db = str(tmp_path / "m11.db")
    with sqlite3.connect(db) as c:
        c.executescript(M11_SCHEMA)
        c.execute("INSERT INTO dua_runs VALUES ('run_old','manifest',NULL,NULL,'[]',1.0)")
        c.execute("INSERT INTO dua_proposals VALUES ('prop_old','run_old',0,'d1',2.0,'{}',NULL)")
        c.execute("INSERT INTO dua_approvals VALUES ('prop_old','d1','n',3.0,'vfp')")
    store = RunStore(db_path=db)
    approval = store.get_approval("prop_old")
    # Pre-M12 rows are intact; they simply have no resolution fingerprint and no evidence (UNKNOWN).
    assert (approval.diff_sha256, approval.note, approval.approved_at, approval.validation_fingerprint) == (
        "d1", "n", 3.0, "vfp")
    assert approval.resolution_fingerprint is None
    assert store.latest_resolution("prop_old", ResolutionRole.PROPOSED) is None
    assert {"dua_jobs", "dua_resolutions", "dua_jobs_one_active"} <= _tables(db)
    # Re-opening is idempotent.
    RunStore(db_path=db).get_approval("prop_old")
