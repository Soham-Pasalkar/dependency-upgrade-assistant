"""SQLite-backed resolution jobs (FR-29, FR-32b, NFR-13, NFR-15; ADR-012).

A job resolves one plan: first the original manifest (the informational FR-32b baseline), then the
proposed manifest (the authoritative evidence), each in a fresh container, sequentially, under the same
environment. Timeouts (one setting, T = RESOLVER_TIMEOUT_SECONDS):
- job: ``started_at + 2T``; time spent QUEUED is excluded;
- run: ``min(run_start + T, job deadline)``, covering the container, verification and upload dates;
- cleanup: up to 30 s after the terminal state is recorded, then the labelled sweep.

Evidence rules: a row is written only when a run completed and the supervisor classified it.
The original row is written when run 1 completes; the proposed row is written in the same
transaction that marks the job SUCCEEDED. Cancelled, timed-out, interrupted and resolver-unavailable
runs store nothing. A failed original never stops the proposed run.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from src.api.config import settings
from src.resolution.comparison import apply_comparison, compare
from src.resolution.contexts import (
    build_contexts,
    check_runtime_facts,
    environment_from_report,
    host_environment,
)
from src.resolution.fingerprint import ResolutionEnvironmentError, resolution_environment
from src.resolution.metadata_client import Deadline, DeadlineExceeded, PyPIMetadataClient
from src.resolution.models import (
    JobStatus,
    ResolutionReport,
    ResolutionRole,
    ResolutionStatus,
)
from src.resolution.python_resolver import RunOutcome, evaluate_run
from src.resolution.resolver_runtime import ResolverRuntime, RuntimeUnavailable, default_runtime
from src.resolution.target import marker_environment
from src.resolution.verifier import ResolutionContext, check_binding, failed_report, unsupported_report
from src.services.approval_service import ensure_registered, is_expired
from src.services.proposal_registry import ProposalRegistry, get_proposal_registry
from src.services.run_store import JobRecord, RunStore, StoredResolution, get_run_store

logger = logging.getLogger(__name__)
KIND = "resolution"
UNAVAILABLE_MESSAGE = ("The isolated resolver is unavailable: Docker, the pinned resolver image or the resolver "
                       "network could not be used. Check that Docker is running and try again.")


class JobServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass
class SubmitResult:
    job: Optional[JobRecord] = None
    report: Optional[ResolutionReport] = None  # immediate UNSUPPORTED result (no job)


@dataclass
class ResolutionState:
    """What the server currently knows about a plan's compatibility (FR-23, ADR-012)."""

    status: ResolutionStatus
    reason: Optional[str]
    proposed: Optional[StoredResolution]
    original: Optional[StoredResolution]
    job: Optional[JobRecord]


def _load_plan(proposal_id: str, store: RunStore, registry: ProposalRegistry):
    binding = store.get_binding(proposal_id)
    if binding is None:
        raise JobServiceError(404, "not_found", "This plan was not found.")
    if is_expired(binding):
        raise JobServiceError(410, "proposal_expired", "This plan has expired. Build it again.")
    stored = ensure_registered(proposal_id, store, registry)
    if stored is None:
        raise JobServiceError(410, "proposal_expired", "This plan has expired. Build it again.")
    return binding, stored.proposal


def resolution_state(proposal_id: str, store: RunStore, registry: ProposalRegistry) -> ResolutionState:
    """The plan's current status. Only proposed evidence that still binds to today's inputs counts."""
    binding, proposal = _load_plan(proposal_id, store, registry)
    workspace = store.workspace_dir(binding.run_id)
    job = store.latest_job(KIND, proposal_id)
    proposed = store.latest_resolution(proposal_id, ResolutionRole.PROPOSED)
    original = store.latest_resolution(proposal_id, ResolutionRole.ORIGINAL)
    if proposed is not None:
        try:
            env = environment_from_report(proposed.report)
            _, p_ctx = build_contexts(proposal_id, proposal, workspace, env)
            mismatch = check_binding(proposed.report, p_ctx)
        except ResolutionEnvironmentError as err:
            mismatch = f"environment: {err}"
        if mismatch is None:
            if original is not None and original.job_id != proposed.job_id:
                original = None
            return ResolutionState(proposed.status, proposed.report.failure, proposed, original, job)
        stale_reason = f"evidence_stale ({mismatch})"
    else:
        stale_reason = "not_resolved"
    if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        return ResolutionState(ResolutionStatus.UNKNOWN, f"job_{job.status.value.lower()}", None, None, job)
    if job is not None and (proposed is None or job.created_at > proposed.created_at):
        if job.status == JobStatus.TIMED_OUT:
            return ResolutionState(ResolutionStatus.RESOLUTION_FAILED, "timeout", None, None, job)
        if job.status == JobStatus.FAILED and job.error_code == "resolver_unavailable":
            return ResolutionState(ResolutionStatus.RESOLVER_UNAVAILABLE, job.error_message, None, None, job)
        if job.status == JobStatus.FAILED:
            return ResolutionState(ResolutionStatus.RESOLUTION_FAILED, job.error_code, None, None, job)
        return ResolutionState(ResolutionStatus.UNKNOWN, f"job_{job.status.value.lower()}", None, None, job)
    return ResolutionState(ResolutionStatus.UNKNOWN, stale_reason, None, None, job)


class ResolutionJobService:
    def __init__(
        self,
        store: Optional[RunStore] = None,
        registry: Optional[ProposalRegistry] = None,
        runtime_factory: Callable[[], ResolverRuntime] = default_runtime,
        client_factory: Callable[[], PyPIMetadataClient] = PyPIMetadataClient,
        max_workers: Optional[int] = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._registry = registry
        self.runtime_factory = runtime_factory
        self.client_factory = client_factory
        self.clock = clock
        self.monotonic = monotonic
        self._pool = ThreadPoolExecutor(max_workers or settings.RESOLVER_MAX_CONCURRENT,
                                        thread_name_prefix="dua-resolve")
        self._cancel: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    @property
    def store(self) -> RunStore:
        return self._store or get_run_store()

    @property
    def registry(self) -> ProposalRegistry:
        return self._registry or get_proposal_registry()

    # -- lifecycle ---------------------------------------------------------------------------------
    def recover(self) -> None:
        """Start-up: QUEUED/RUNNING jobs become INTERRUPTED (no evidence), then the sweep runs."""
        interrupted = self.store.interrupt_active_jobs(self.clock())
        if interrupted:
            logger.warning("interrupted %d resolution job(s) left by a previous process", len(interrupted))
        try:
            self.runtime_factory().sweep(active_job_ids=set())
        except RuntimeUnavailable as err:
            logger.info("resolver sweep skipped: %s", err)

    def shutdown(self, wait: bool = False) -> None:
        for event in list(self._cancel.values()):
            event.set()
        self._pool.shutdown(wait=wait, cancel_futures=True)

    # -- API operations ----------------------------------------------------------------------------
    def submit(self, proposal_id: str) -> SubmitResult:
        binding, proposal = _load_plan(proposal_id, self.store, self.registry)
        workspace = self.store.workspace_dir(binding.run_id)
        o_ctx, p_ctx = build_contexts(proposal_id, proposal, workspace, host_environment())
        if not p_ctx.manifest.supported:
            # Never sent to a resolver: record UNSUPPORTED evidence for both roles at once (FR-23).
            if not o_ctx.manifest.supported:
                self.store.record_resolution(proposal_id, None, unsupported_report(o_ctx, self.clock()))
            report = unsupported_report(p_ctx, self.clock())
            self.store.record_resolution(proposal_id, None, report)
            return SubmitResult(report=report)
        job = self.store.create_job(KIND, proposal_id)
        if job is None:
            active = self.store.active_job(KIND, proposal_id)
            raise JobServiceError(409, "job_active",
                                  f"A compatibility check is already running for this plan ({active.id if active else ''}).")
        with self._lock:
            self._cancel[job.id] = threading.Event()
        self._pool.submit(self._run_safely, job.id)
        return SubmitResult(job=job)

    def cancel(self, job_id: str) -> JobRecord:
        job = self.store.get_job(job_id)
        if job is None:
            raise JobServiceError(404, "not_found", "This job was not found.")
        if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            self.store.request_job_cancel(job_id)
            with self._lock:
                event = self._cancel.get(job_id)
            if event is not None:
                event.set()
            self.store.transition_job(job_id, JobStatus.QUEUED, JobStatus.CANCELLED, finished_at=self.clock(),
                                      error_code="cancelled", error_message="Cancelled before it started.")
        return self.store.get_job(job_id)

    def get(self, job_id: str) -> JobRecord:
        job = self.store.get_job(job_id)
        if job is None:
            raise JobServiceError(404, "not_found", "This job was not found.")
        return job

    # -- execution ---------------------------------------------------------------------------------
    def _run_safely(self, job_id: str) -> None:
        try:
            self.run_job(job_id)
        except Exception:  # pragma: no cover - defensive; run_job finishes the job itself
            logger.exception("resolution job %s crashed", job_id)
            self._finish(job_id, JobStatus.FAILED, "internal_error", "The compatibility check failed unexpectedly.")
        finally:
            with self._lock:
                self._cancel.pop(job_id, None)

    def _finish(self, job_id: str, status: JobStatus, code: Optional[str], message: Optional[str]) -> bool:
        return self.store.transition_job(job_id, JobStatus.RUNNING, status, finished_at=self.clock(),
                                         current_role=None, error_code=code, error_message=message)

    def _unavailable(self, job_id: str, err: Exception) -> None:
        # Docker's own error text can contain host paths; it goes to the server log only.
        logger.warning("resolution job %s: resolver unavailable: %s", job_id, str(err)[:500])
        self._finish(job_id, JobStatus.FAILED, "resolver_unavailable", UNAVAILABLE_MESSAGE)

    def run_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None or job.status != JobStatus.QUEUED:
            return
        with self._lock:
            event = self._cancel.setdefault(job_id, threading.Event())
        if job.cancel_requested:
            self.store.transition_job(job_id, JobStatus.QUEUED, JobStatus.CANCELLED, finished_at=self.clock(),
                                      error_code="cancelled")
            return
        T = settings.RESOLVER_TIMEOUT_SECONDS
        started = self.clock()
        if not self.store.transition_job(job_id, JobStatus.QUEUED, JobStatus.RUNNING, started_at=started,
                                         deadline_at=started + 2 * T):
            return
        job_deadline = self.monotonic() + 2 * T

        def cancelled() -> bool:
            if event.is_set():
                return True
            current = self.store.get_job(job_id)
            return current is None or current.cancel_requested

        runtime, net = None, None
        try:
            try:
                binding, proposal = _load_plan(job.subject_id, self.store, self.registry)
            except JobServiceError as err:
                self._finish(job_id, JobStatus.FAILED, err.code, err.message)
                return
            try:
                runtime = self.runtime_factory()
                env = resolution_environment(runtime.architecture())
                marker_environment(env)  # fails closed if the image is not the one with verified facts
                o_ctx, p_ctx = build_contexts(job.subject_id, proposal, self.store.workspace_dir(binding.run_id), env)
                net = runtime.start_job(job_id, started + 2 * T)
            except (RuntimeUnavailable, ResolutionEnvironmentError) as err:
                self._unavailable(job_id, err)
                return
            original_report: Optional[ResolutionReport] = None
            seen_events = 0
            for ctx in (o_ctx, p_ctx):
                role = ctx.role.value
                if cancelled():
                    self._finish(job_id, JobStatus.CANCELLED, "cancelled", "Cancelled.")
                    return
                if self.monotonic() >= job_deadline:
                    self._finish(job_id, JobStatus.TIMED_OUT, "timeout", f"The job deadline passed before the {role} run.")
                    return
                self.store.transition_job(job_id, JobStatus.RUNNING, JobStatus.RUNNING, current_role=role)
                outcome = self._run_role(runtime, net, ctx, env, job_deadline, cancelled)
                if outcome in ("cancelled", "timed_out"):
                    if outcome == "cancelled":
                        self._finish(job_id, JobStatus.CANCELLED, "cancelled", "Cancelled.")
                    else:
                        self._finish(job_id, JobStatus.TIMED_OUT, "timeout",
                                     f"The {role} resolution did not finish in time.")
                    return
                report = outcome
                events = runtime.proxy_events(net)
                denials = [f"egress_denied: {e.get('host')}:{e.get('port')} {e.get('reason')}"
                           for e in events[seen_events:] if e.get("decision") == "deny"]
                seen_events = len(events)
                if denials:
                    report = report.model_copy(update={"warnings": report.warnings + denials[:20]})
                    logger.warning("job %s %s egress denials: %s", job_id, role, denials[:5])
                if ctx.role == ResolutionRole.ORIGINAL:
                    if not self.store.record_job_evidence(job_id, job.subject_id, report):
                        # Cancelled, or no longer this worker's job (e.g. interrupted by a restart).
                        self._finish(job_id, JobStatus.CANCELLED, "cancelled", "Cancelled.")
                        return
                    original_report = report
                else:
                    report = apply_comparison(report, compare(original_report, report, o_ctx, p_ctx))
                    if not self.store.complete_job_with_evidence(job_id, job.subject_id, report):
                        self._finish(job_id, JobStatus.CANCELLED, "cancelled", "Cancelled.")
                    return
        except RuntimeUnavailable as err:
            self._unavailable(job_id, err)
        finally:
            if runtime is not None and net is not None:
                try:
                    runtime.stop_job(net)
                except Exception:  # pragma: no cover - cleanup is best effort; the sweep is the backstop
                    logger.exception("cleanup failed for job %s", job_id)

    def _run_role(self, runtime, net, ctx: ResolutionContext, env, job_deadline: float, cancelled):
        """One role. Returns a report, or 'cancelled' / 'timed_out' (which store nothing)."""
        if not ctx.manifest.supported:
            return unsupported_report(ctx, self.clock())
        run_deadline = min(self.monotonic() + settings.RESOLVER_TIMEOUT_SECONDS, job_deadline)
        result = runtime.run(net, ctx.role.value, ctx.manifest, run_deadline, cancelled)
        if result.state in ("cancelled", "timed_out"):
            return result.state
        problem = check_runtime_facts(result.facts, env)
        if problem:
            return failed_report(ctx, "environment_mismatch", problem, now=self.clock())
        try:
            return evaluate_run(ctx, RunOutcome(result.exit_code, result.stdout, result.stderr),
                                self.client_factory(), Deadline(at=run_deadline, clock=self.monotonic),
                                now=self.clock())
        except DeadlineExceeded:
            return "timed_out"


_service: Optional[ResolutionJobService] = None
_service_lock = threading.Lock()


def get_job_service() -> ResolutionJobService:
    global _service
    with _service_lock:
        if _service is None:
            _service = ResolutionJobService()
        return _service


def set_job_service(service: Optional[ResolutionJobService]) -> None:
    """Replace the process-wide service (tests and application start-up)."""
    global _service
    with _service_lock:
        if _service is not None and _service is not service:
            _service.shutdown()
        _service = service
