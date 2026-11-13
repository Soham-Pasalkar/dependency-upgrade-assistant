"""Bounded retention for exports and persisted analysis runs (M11, findings F8 and F9).

Deterministic and idempotent. Runs at startup and opportunistically (at most every
RETENTION_INTERVAL_SECONDS) when runs or exports are created; no background-job framework.
Never deletes unexpired work: an export is kept while its plan is still valid, and a run is kept
while any of its plans is still within PLAN_RETENTION_HOURS.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from src.api.config import settings

logger = logging.getLogger("dua.retention")

EXPORT_DIR_RE = re.compile(r"^exp_[0-9a-f]{32}$")
STAGING_DIR_RE = re.compile(r"^tmp_exp_[0-9a-f]{32}$")
STALE_STAGING_SECONDS = 3600.0
RETENTION_INTERVAL_SECONDS = 900.0
MAX_SUMMARY_BYTES = 1024 * 1024

_last_run = 0.0
_lock = threading.Lock()


@dataclass
class RetentionResult:
    exports_removed: int = 0
    staging_removed: int = 0
    runs_removed: int = 0
    failures: int = 0


def _proposal_of(export_dir: Path) -> Optional[str]:
    summary = export_dir / "summary.json"
    try:
        if summary.is_file() and not summary.is_symlink() and summary.stat().st_size <= MAX_SUMMARY_BYTES:
            value = json.loads(summary.read_text(encoding="utf-8")).get("proposal_id")
            return value if isinstance(value, str) else None
    except (OSError, ValueError):
        return None
    return None


def cleanup_exports(
    store_dir: Path,
    max_age_seconds: float,
    plan_is_valid: Callable[[str], bool],
    now: Optional[float] = None,
    forget: Optional[Callable[[str], None]] = None,
) -> RetentionResult:
    """Remove expired export directories and stale staging directories. Unknown entries are left alone."""
    result = RetentionResult()
    now = now if now is not None else time.time()
    if not store_dir.is_dir():
        return result
    for entry in store_dir.iterdir():
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            age = now - entry.stat().st_mtime
            if STAGING_DIR_RE.match(entry.name):
                if age > STALE_STAGING_SECONDS:
                    shutil.rmtree(entry)
                    result.staging_removed += 1
                continue
            if not EXPORT_DIR_RE.match(entry.name) or age <= max_age_seconds:
                continue
            proposal_id = _proposal_of(entry)
            if proposal_id and plan_is_valid(proposal_id):
                continue  # still referenced by a valid plan
            shutil.rmtree(entry)
            if forget:
                forget(entry.name)
            result.exports_removed += 1
        except OSError as exc:
            result.failures += 1
            logger.warning("Export retention skipped %s: %s", entry.name, type(exc).__name__)
    return result


def run_retention(now: Optional[float] = None) -> RetentionResult:
    """Apply export and run retention once. Safe to call repeatedly."""
    from src.services.approval_service import is_expired
    from src.services.exporter import get_artifact_exporter
    from src.services.run_store import get_run_store

    now = now if now is not None else time.time()
    store = get_run_store()

    def plan_is_valid(proposal_id: str) -> bool:
        binding = store.get_binding(proposal_id)
        return binding is not None and not is_expired(binding, now)

    exporter = get_artifact_exporter()

    def forget(export_id: str) -> None:
        with exporter._lock:
            exporter._exports.pop(export_id, None)

    result = cleanup_exports(
        exporter.export_store_dir, settings.EXPORT_RETENTION_HOURS * 3600, plan_is_valid, now, forget
    )
    try:
        result.runs_removed = store.prune_expired(
            run_cutoff=now - settings.RUN_RETENTION_HOURS * 3600,
            plan_cutoff=now - settings.PLAN_RETENTION_HOURS * 3600,
        )
    except Exception as exc:  # never break the request that triggered retention
        result.failures += 1
        logger.warning("Run retention failed: %s", type(exc).__name__)
    if result.exports_removed or result.staging_removed or result.runs_removed or result.failures:
        logger.info(
            "Retention: removed %d export(s), %d stale staging dir(s), %d run(s); %d failure(s)",
            result.exports_removed, result.staging_removed, result.runs_removed, result.failures,
        )
    return result


def maybe_run_retention() -> Optional[RetentionResult]:
    """Run retention at most once per RETENTION_INTERVAL_SECONDS."""
    global _last_run
    now = time.time()
    with _lock:
        if now - _last_run < RETENTION_INTERVAL_SECONDS:
            return None
        _last_run = now
    return run_retention(now)
