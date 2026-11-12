"""SQLite-backed store for server-authoritative runs, plans and approvals (ADR-005).

The client only ever holds opaque identifiers. Manifest content, analysis, recommendations,
proposal bindings and approvals live here, so client-supplied state cannot alter results.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, List, Optional

from src.api.config import settings
from src.models.manifest import ManifestAnalysisResponse
from src.models.plan import UpgradeProposalResponse
from src.models.recommendation import RecommendationBatchResponse
from src.models.run import RepositoryInfo, RunManifest, RunResponse
from src.resolution.models import (
    JobStatus,
    ResolutionReport,
    ResolutionRole,
    ResolutionStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS dua_runs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK(source IN ('github', 'manifest')),
    repository TEXT,
    workspace_dir TEXT,
    skipped TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dua_run_manifests (
    run_id TEXT NOT NULL REFERENCES dua_runs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    path TEXT NOT NULL,
    manifest_type TEXT NOT NULL,
    kind TEXT NOT NULL,
    plannable INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    error TEXT,
    content TEXT NOT NULL,
    analysis TEXT,
    recommendations TEXT,
    proposal_id TEXT,
    PRIMARY KEY (run_id, idx)
);
CREATE TABLE IF NOT EXISTS dua_proposals (
    proposal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES dua_runs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    diff_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dua_approvals (
    proposal_id TEXT PRIMARY KEY REFERENCES dua_proposals(proposal_id) ON DELETE CASCADE,
    diff_sha256 TEXT NOT NULL,
    note TEXT,
    approved_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dua_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('resolution')),
    subject_id TEXT NOT NULL REFERENCES dua_proposals(proposal_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN
        ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','INTERRUPTED')),
    current_role TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    deadline_at REAL,
    finished_at REAL,
    error_code TEXT,
    error_message TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS dua_jobs_one_active
    ON dua_jobs(kind, subject_id) WHERE status IN ('QUEUED', 'RUNNING');
CREATE TABLE IF NOT EXISTS dua_resolutions (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES dua_proposals(proposal_id) ON DELETE CASCADE,
    job_id TEXT REFERENCES dua_jobs(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('original', 'proposed')),
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS dua_resolutions_latest ON dua_resolutions(proposal_id, role, created_at);
"""


# Additive migrations for databases created before these columns existed.
MIGRATIONS = (
    ("dua_proposals", "proposal_json", "ALTER TABLE dua_proposals ADD COLUMN proposal_json TEXT"),
    ("dua_proposals", "consumed_at", "ALTER TABLE dua_proposals ADD COLUMN consumed_at REAL"),
    ("dua_approvals", "validation_fingerprint", "ALTER TABLE dua_approvals ADD COLUMN validation_fingerprint TEXT"),
    ("dua_approvals", "resolution_fingerprint", "ALTER TABLE dua_approvals ADD COLUMN resolution_fingerprint TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class StoredManifest:
    run_id: str
    index: int
    path: str
    manifest_type: str
    kind: str
    plannable: bool
    content: str
    analysis: Optional[ManifestAnalysisResponse]
    recommendations: Optional[RecommendationBatchResponse]
    proposal_id: Optional[str]
    note: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ProposalBinding:
    proposal_id: str
    run_id: str
    index: int
    diff_sha256: str
    created_at: float = 0.0
    consumed: bool = False


@dataclass
class ApprovalRecord:
    proposal_id: str
    diff_sha256: str
    note: Optional[str]
    approved_at: float
    validation_fingerprint: Optional[str] = None
    resolution_fingerprint: Optional[str] = None


@dataclass
class JobRecord:
    id: str
    kind: str
    subject_id: str
    status: JobStatus
    created_at: float
    current_role: Optional[str] = None
    cancel_requested: bool = False
    started_at: Optional[float] = None
    deadline_at: Optional[float] = None
    finished_at: Optional[float] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class StoredResolution:
    id: str
    proposal_id: str
    job_id: Optional[str]
    role: ResolutionRole
    fingerprint: str
    status: ResolutionStatus
    report: ResolutionReport
    created_at: float


# Columns a job transition may set; anything else is rejected (no dynamic SQL from callers).
_JOB_UPDATABLE = frozenset(
    {"current_role", "started_at", "deadline_at", "finished_at", "error_code", "error_message"}
)


class RunStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.DATABASE_PATH
        self._lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        try:
            if not self._initialized:
                with self._lock:
                    conn.executescript(SCHEMA)
                    _migrate(conn)
                    self._initialized = True
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- runs ---------------------------------------------------------------
    def create_run(
        self,
        source: str,
        repository: Optional[RepositoryInfo] = None,
        workspace_dir: Optional[str] = None,
        skipped: Optional[List[str]] = None,
        run_id: Optional[str] = None,
    ) -> str:
        run_id = run_id or f"run_{uuid.uuid4().hex}"
        with self._conn() as c:
            c.execute(
                "INSERT INTO dua_runs (id, source, repository, workspace_dir, skipped, created_at) VALUES (?,?,?,?,?,?)",
                (
                    run_id,
                    source,
                    repository.model_dump_json() if repository else None,
                    workspace_dir,
                    json.dumps(skipped or []),
                    time.time(),
                ),
            )
        return run_id

    def add_manifest(self, m: StoredManifest) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO dua_run_manifests
                   (run_id, idx, path, manifest_type, kind, plannable, note, error, content, analysis, recommendations, proposal_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    m.run_id, m.index, m.path, m.manifest_type, m.kind, int(m.plannable), m.note, m.error, m.content,
                    m.analysis.model_dump_json() if m.analysis else None,
                    m.recommendations.model_dump_json() if m.recommendations else None,
                    m.proposal_id,
                ),
            )

    def workspace_dir(self, run_id: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT workspace_dir FROM dua_runs WHERE id=?", (run_id,)).fetchone()
        return row["workspace_dir"] if row else None

    def get_manifest(self, run_id: str, index: int) -> Optional[StoredManifest]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM dua_run_manifests WHERE run_id=? AND idx=?", (run_id, index)).fetchone()
        return self._manifest_from_row(row) if row else None

    def set_recommendations(self, run_id: str, index: int, recs: RecommendationBatchResponse) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE dua_run_manifests SET recommendations=?, proposal_id=NULL WHERE run_id=? AND idx=?",
                (recs.model_dump_json(), run_id, index),
            )

    def get_run(self, run_id: str) -> Optional[RunResponse]:
        with self._conn() as c:
            run = c.execute("SELECT * FROM dua_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                return None
            rows = c.execute("SELECT * FROM dua_run_manifests WHERE run_id=? ORDER BY idx", (run_id,)).fetchall()
        manifests = []
        for row in rows:
            m = self._manifest_from_row(row)
            manifests.append(
                RunManifest(
                    index=m.index, path=m.path, manifest_type=m.manifest_type, kind=m.kind,
                    ecosystem=(m.analysis.ecosystem.value if m.analysis else None),
                    plannable=m.plannable, note=m.note, error=m.error, analysis=m.analysis,
                    recommendations=m.recommendations, proposal_id=m.proposal_id,
                )
            )
        workspace = run["workspace_dir"]
        return RunResponse(
            run_id=run["id"],
            source=run["source"],
            created_at=run["created_at"],
            repository=RepositoryInfo.model_validate_json(run["repository"]) if run["repository"] else None,
            manifests=manifests,
            skipped=json.loads(run["skipped"] or "[]"),
            has_workspace=bool(workspace),
        )

    # -- proposals and approvals --------------------------------------------
    def bind_proposal(
        self,
        proposal_id: str,
        run_id: str,
        index: int,
        unified_diff: str,
        proposal: Optional[UpgradeProposalResponse] = None,
    ) -> ProposalBinding:
        digest = sha256_text(unified_diff)
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO dua_proposals (proposal_id, run_id, idx, diff_sha256, created_at, proposal_json)"
                " VALUES (?,?,?,?,?,?)",
                (proposal_id, run_id, index, digest, now, proposal.model_dump_json() if proposal else None),
            )
            c.execute(
                "UPDATE dua_run_manifests SET proposal_id=? WHERE run_id=? AND idx=?", (proposal_id, run_id, index)
            )
        return ProposalBinding(proposal_id=proposal_id, run_id=run_id, index=index, diff_sha256=digest, created_at=now)

    def get_stored_proposal(self, proposal_id: str) -> Optional[UpgradeProposalResponse]:
        with self._conn() as c:
            row = c.execute("SELECT proposal_json FROM dua_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row or not row["proposal_json"]:
            return None
        return UpgradeProposalResponse.model_validate_json(row["proposal_json"])

    def mark_consumed(self, proposal_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE dua_proposals SET consumed_at=? WHERE proposal_id=? AND consumed_at IS NULL",
                      (time.time(), proposal_id))

    def get_binding(self, proposal_id: str) -> Optional[ProposalBinding]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM dua_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row:
            return None
        return ProposalBinding(
            proposal_id=row["proposal_id"], run_id=row["run_id"], index=row["idx"], diff_sha256=row["diff_sha256"],
            created_at=row["created_at"], consumed=row["consumed_at"] is not None,
        )

    def record_approval(
        self,
        proposal_id: str,
        diff_sha256: str,
        note: Optional[str],
        validation_fingerprint: Optional[str] = None,
        resolution_fingerprint: Optional[str] = None,
    ) -> ApprovalRecord:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO dua_approvals (proposal_id, diff_sha256, note, approved_at,"
                " validation_fingerprint, resolution_fingerprint) VALUES (?,?,?,?,?,?)",
                (proposal_id, diff_sha256, note, now, validation_fingerprint, resolution_fingerprint),
            )
        return ApprovalRecord(proposal_id=proposal_id, diff_sha256=diff_sha256, note=note, approved_at=now,
                              validation_fingerprint=validation_fingerprint,
                              resolution_fingerprint=resolution_fingerprint)

    def get_approval(self, proposal_id: str) -> Optional[ApprovalRecord]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM dua_approvals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row:
            return None
        return ApprovalRecord(
            proposal_id=row["proposal_id"], diff_sha256=row["diff_sha256"], note=row["note"], approved_at=row["approved_at"],
            validation_fingerprint=row["validation_fingerprint"],
            resolution_fingerprint=row["resolution_fingerprint"],
        )

    # -- resolution jobs (M12, ADR-012) ----------------------------------------
    def create_job(self, kind: str, subject_id: str) -> Optional[JobRecord]:
        """Create a QUEUED job, or return None if one is already active for the subject."""
        job = JobRecord(id=uuid.uuid4().hex, kind=kind, subject_id=subject_id, status=JobStatus.QUEUED,
                        created_at=time.time())
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO dua_jobs (id, kind, subject_id, status, created_at) VALUES (?,?,?,?,?)",
                    (job.id, kind, subject_id, job.status.value, job.created_at),
                )
        except sqlite3.IntegrityError:
            if self.active_job(kind, subject_id) is not None:
                return None
            raise
        return job

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM dua_jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_from_row(row) if row else None

    def active_job(self, kind: str, subject_id: str) -> Optional[JobRecord]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM dua_jobs WHERE kind=? AND subject_id=? AND status IN ('QUEUED','RUNNING')",
                (kind, subject_id),
            ).fetchone()
        return self._job_from_row(row) if row else None

    def latest_job(self, kind: str, subject_id: str) -> Optional[JobRecord]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM dua_jobs WHERE kind=? AND subject_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (kind, subject_id),
            ).fetchone()
        return self._job_from_row(row) if row else None

    def transition_job(self, job_id: str, expected, new_status: JobStatus, **fields) -> bool:
        """Compare-and-set: move the job to new_status only if it is still in one of the expected states."""
        expected = tuple(JobStatus(s).value for s in (expected if isinstance(expected, (tuple, list)) else (expected,)))
        unknown = set(fields) - _JOB_UPDATABLE
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        sets = ["status=?"] + [f"{k}=?" for k in fields]
        params = [JobStatus(new_status).value, *fields.values(), job_id, *expected]
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE dua_jobs SET {', '.join(sets)} WHERE id=? AND status IN ({','.join('?' * len(expected))})",
                params,
            )
            return cur.rowcount == 1

    def request_job_cancel(self, job_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE dua_jobs SET cancel_requested=1 WHERE id=? AND status IN ('QUEUED','RUNNING')", (job_id,)
            )
            return cur.rowcount == 1

    def interrupt_active_jobs(self, now: Optional[float] = None) -> List[str]:
        """Start-up recovery: QUEUED or RUNNING jobs become INTERRUPTED. Never stores evidence."""
        now = now or time.time()
        with self._conn() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM dua_jobs WHERE status IN ('QUEUED','RUNNING')")]
            for job_id in ids:
                c.execute(
                    "UPDATE dua_jobs SET status='INTERRUPTED', finished_at=?, error_code='interrupted'"
                    " WHERE id=? AND status IN ('QUEUED','RUNNING')",
                    (now, job_id),
                )
        return ids

    # -- resolution evidence (M12, ADR-011); written only by the supervisor ---------------
    def record_resolution(self, proposal_id: str, job_id: Optional[str], report: ResolutionReport) -> str:
        with self._conn() as c:
            return self._insert_resolution(c, proposal_id, job_id, report)

    def complete_job_with_evidence(self, job_id: str, proposal_id: str, report: ResolutionReport) -> bool:
        """Atomically mark a RUNNING job SUCCEEDED and store its proposed-role evidence (ADR-012).

        If the job is no longer RUNNING (cancelled, timed out or interrupted), nothing is written.
        """
        if report.role != ResolutionRole.PROPOSED:
            raise ValueError("only proposed-role evidence completes a job")
        with self._conn() as c:
            cur = c.execute(
                "UPDATE dua_jobs SET status='SUCCEEDED', finished_at=?, current_role=NULL"
                " WHERE id=? AND status='RUNNING' AND cancel_requested=0",
                (time.time(), job_id),
            )
            if cur.rowcount != 1:
                return False
            self._insert_resolution(c, proposal_id, job_id, report)
        return True

    def latest_resolution(self, proposal_id: str, role: ResolutionRole) -> Optional[StoredResolution]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM dua_resolutions WHERE proposal_id=? AND role=?"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (proposal_id, ResolutionRole(role).value),
            ).fetchone()
        if not row:
            return None
        return StoredResolution(
            id=row["id"], proposal_id=row["proposal_id"], job_id=row["job_id"], role=ResolutionRole(row["role"]),
            fingerprint=row["fingerprint"], status=ResolutionStatus(row["status"]),
            report=ResolutionReport.model_validate_json(row["report_json"]), created_at=row["created_at"],
        )

    @staticmethod
    def _insert_resolution(c: sqlite3.Connection, proposal_id: str, job_id: Optional[str],
                           report: ResolutionReport) -> str:
        rid = uuid.uuid4().hex
        c.execute(
            "INSERT INTO dua_resolutions (id, proposal_id, job_id, role, fingerprint, status, report_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rid, proposal_id, job_id, report.role.value, report.fingerprint, report.status.value,
             report.model_dump_json(), time.time()),
        )
        return rid

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"], kind=row["kind"], subject_id=row["subject_id"], status=JobStatus(row["status"]),
            created_at=row["created_at"], current_role=row["current_role"],
            cancel_requested=bool(row["cancel_requested"]), started_at=row["started_at"],
            deadline_at=row["deadline_at"], finished_at=row["finished_at"], error_code=row["error_code"],
            error_message=row["error_message"],
        )

    # -- retention (M11, F9) -------------------------------------------------
    def prune_expired(self, run_cutoff: float, plan_cutoff: float) -> int:
        """Delete runs created before run_cutoff whose plans were all created before plan_cutoff.

        Deleting a run cascades to its manifests, proposal bindings and approvals (foreign keys).
        Runs with any plan still inside the plan retention window are kept.
        Returns the number of runs deleted.
        """
        with self._conn() as c:
            rows = c.execute(
                """SELECT id FROM dua_runs r
                   WHERE r.created_at < ?
                     AND NOT EXISTS (
                       SELECT 1 FROM dua_proposals p WHERE p.run_id = r.id AND p.created_at >= ?
                     )""",
                (run_cutoff, plan_cutoff),
            ).fetchall()
            ids = [row["id"] for row in rows]
            for run_id in ids:
                c.execute("DELETE FROM dua_runs WHERE id=?", (run_id,))
        return len(ids)

    @staticmethod
    def _manifest_from_row(row: sqlite3.Row) -> StoredManifest:
        return StoredManifest(
            run_id=row["run_id"],
            index=row["idx"],
            path=row["path"],
            manifest_type=row["manifest_type"],
            kind=row["kind"],
            plannable=bool(row["plannable"]),
            content=row["content"],
            analysis=ManifestAnalysisResponse.model_validate_json(row["analysis"]) if row["analysis"] else None,
            recommendations=(
                RecommendationBatchResponse.model_validate_json(row["recommendations"]) if row["recommendations"] else None
            ),
            proposal_id=row["proposal_id"],
            note=row["note"],
            error=row["error"],
        )


_store: Optional[RunStore] = None


def get_run_store() -> RunStore:
    global _store
    if _store is None:
        _store = RunStore()
    return _store
