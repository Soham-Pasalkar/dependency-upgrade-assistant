"""M11 / F8 and F9: bounded, idempotent retention that never removes unexpired or referenced work."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from src.services import retention
from src.services.retention import cleanup_exports
from src.services.run_store import RunStore, StoredManifest

DAY = 86400.0


def _export(store: Path, age: float, proposal_id: str | None = "prop_x", now: float = 0.0) -> Path:
    d = store / f"exp_{uuid.uuid4().hex}"
    d.mkdir()
    if proposal_id is not None:
        (d / "summary.json").write_text(json.dumps({"proposal_id": proposal_id}))
    (d / "x.patch").write_text("diff")
    os.utime(d, (now - age, now - age))
    return d


def test_export_retention_rules(tmp_path):
    now = time.time()
    old_unreferenced = _export(tmp_path, 2 * DAY, "prop_gone", now)
    old_still_valid = _export(tmp_path, 2 * DAY, "prop_valid", now)
    old_no_summary = _export(tmp_path, 2 * DAY, None, now)
    fresh = _export(tmp_path, 60, "prop_gone", now)
    stale_stage = tmp_path / f"tmp_exp_{uuid.uuid4().hex}"
    stale_stage.mkdir()
    os.utime(stale_stage, (now - 7200, now - 7200))
    fresh_stage = tmp_path / f"tmp_exp_{uuid.uuid4().hex}"
    fresh_stage.mkdir()
    os.utime(fresh_stage, (now - 60, now - 60))
    unknown = tmp_path / "something_else"
    unknown.mkdir()
    os.utime(unknown, (now - 10 * DAY, now - 10 * DAY))
    outside = tmp_path.parent / f"outside_{uuid.uuid4().hex}"
    outside.mkdir()
    link = tmp_path / f"exp_{uuid.uuid4().hex}"
    link.symlink_to(outside)

    forgotten = []
    result = cleanup_exports(tmp_path, DAY, lambda pid: pid == "prop_valid", now, forgotten.append)

    assert not old_unreferenced.exists() and not old_no_summary.exists()
    assert old_still_valid.exists(), "an export of a still-valid plan must be kept"
    assert fresh.exists() and fresh_stage.exists() and unknown.exists()
    assert not stale_stage.exists()
    assert link.is_symlink() and outside.exists(), "symlinks are never followed or deleted"
    assert result.exports_removed == 2 and result.staging_removed == 1
    assert sorted(forgotten) == sorted([old_unreferenced.name, old_no_summary.name])

    again = cleanup_exports(tmp_path, DAY, lambda pid: pid == "prop_valid", now)
    assert (again.exports_removed, again.staging_removed) == (0, 0)  # idempotent


def test_export_age_boundary(tmp_path):
    now = time.time()
    at_limit = _export(tmp_path, DAY, "p", now)
    just_over = _export(tmp_path, DAY + 1, "p", now)
    cleanup_exports(tmp_path, DAY, lambda pid: False, now)
    assert at_limit.exists() and not just_over.exists()


def test_export_recovered_after_restart_then_expired(tmp_path):
    from src.services.exporter import ArtifactExporter

    exporter = ArtifactExporter(export_store_dir=tmp_path)
    d = _export(tmp_path, 0, "prop_r", time.time())
    (d / "summary.json").write_text(json.dumps({"proposal_id": "prop_r", "validation": {"status": "NOT_PERFORMED"}}))
    assert exporter.get_export(d.name) is not None  # recovered from disk (fresh process, empty index)
    later = time.time() + 2 * DAY
    cleanup_exports(tmp_path, DAY, lambda pid: False, later, lambda eid: exporter._exports.pop(eid, None))
    assert exporter.get_export(d.name) is None


def _run(store: RunStore, created_at: float, plan_created: list[float]) -> str:
    run_id = store.create_run("manifest")
    with store._conn() as c:
        c.execute("UPDATE dua_runs SET created_at=? WHERE id=?", (created_at, run_id))
    store.add_manifest(StoredManifest(run_id=run_id, index=0, path="requirements.txt", manifest_type="requirements.txt",
                                      kind="requirements", plannable=True, content="x==1\n", analysis=None,
                                      recommendations=None, proposal_id=None))
    for i, ts in enumerate(plan_created):
        pid = f"prop_{run_id}_{i}"
        store.bind_proposal(pid, run_id, 0, f"diff{i}")
        store.record_approval(pid, "h", None, "f")
        with store._conn() as c:
            c.execute("UPDATE dua_proposals SET created_at=? WHERE proposal_id=?", (ts, pid))
    return run_id


def _count(store, table):
    with store._conn() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_run_retention_cascades_and_keeps_active_work(tmp_path):
    store = RunStore(db_path=str(tmp_path / "r.db"))
    now = time.time()
    expired = _run(store, now - 10 * DAY, [now - 9 * DAY])
    old_run_recent_plan = _run(store, now - 10 * DAY, [now - 9 * DAY, now - 3600])
    recent = _run(store, now - 3600, [])
    removed = store.prune_expired(run_cutoff=now - 7 * DAY, plan_cutoff=now - DAY)
    assert removed == 1
    assert store.get_run(expired) is None
    assert store.get_binding(f"prop_{expired}_0") is None and store.get_approval(f"prop_{expired}_0") is None
    assert store.get_run(old_run_recent_plan) is not None, "a run with a still-valid plan is kept"
    assert store.get_run(recent) is not None
    # Cascade removed exactly the expired run's children.
    assert _count(store, "dua_run_manifests") == 2
    assert _count(store, "dua_proposals") == 2 and _count(store, "dua_approvals") == 2
    assert store.prune_expired(run_cutoff=now - 7 * DAY, plan_cutoff=now - DAY) == 0  # idempotent


def test_run_retention_boundary(tmp_path):
    store = RunStore(db_path=str(tmp_path / "b.db"))
    now = time.time()
    exactly = _run(store, now - 7 * DAY, [])
    store.prune_expired(run_cutoff=now - 7 * DAY, plan_cutoff=now - DAY)
    assert store.get_run(exactly) is not None  # strictly older than the cutoff is required


def test_retention_is_rate_limited(monkeypatch):
    calls = []
    monkeypatch.setattr(retention, "run_retention", lambda now=None: calls.append(now) or retention.RetentionResult())
    monkeypatch.setattr(retention, "_last_run", 0.0)
    assert retention.maybe_run_retention() is not None
    assert retention.maybe_run_retention() is None
    assert len(calls) == 1


def test_run_retention_end_to_end_uses_settings(isolated_run_store, tmp_path, monkeypatch):
    from src.api.config import settings
    from src.services import exporter as exporter_module

    exp = exporter_module.ArtifactExporter(export_store_dir=tmp_path / "exports")
    monkeypatch.setattr(exporter_module, "get_artifact_exporter", lambda: exp)
    now = time.time()
    old = _run(isolated_run_store, now - 30 * DAY, [now - 30 * DAY])
    d = _export(exp.export_store_dir, 30 * DAY, f"prop_{old}_0", now)
    result = retention.run_retention(now)
    assert result.runs_removed == 1 and result.exports_removed == 1 and not d.exists()
    assert settings.RUN_RETENTION_HOURS == 24 * 7 and settings.EXPORT_RETENTION_HOURS == 24
