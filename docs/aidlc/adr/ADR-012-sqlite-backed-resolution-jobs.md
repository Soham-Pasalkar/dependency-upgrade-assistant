# ADR-012: SQLite-backed resolution jobs

- Status: proposed (pending G2)
- Date: 2026-11-12
- Requirements: FR-29, FR-32b, NFR-13, NFR-15. Threats: T-30, T-34.

## Context
- **Duration:** each job makes two sequential resolver runs (original, then proposed), each bounded by `RESOLVER_TIMEOUT_SECONDS` (default 180 s, ceiling 600). Long synchronous requests make cancellation ineffective (F11).
- **Deployment:** a single machine.
- **Owner decisions:** jobs are for resolution only in M12; validation stays synchronous; no general-purpose job framework.

## Options considered
1. Synchronous resolution.
2. An external queue (Celery with Redis).
3. A `dua_jobs` table in the existing SQLite database plus a bounded in-process `ThreadPoolExecutor`, used for the `resolution` job kind only.

## Decision
Option 3.

**Job creation.** `POST /api/proposals/{id}/resolve` first classifies the input synchronously (FR-23).
- **No job:** npm and other non-Python plans, and Python inputs with UNSUPPORTED items, get an immediate UNSUPPORTED report and no job. The supervisor stores UNSUPPORTED records for both roles.
- **Otherwise:** a QUEUED job is created and 202 `{job_id}` is returned. A partial unique index allows one active job per (kind, subject). The pool size is `RESOLVER_MAX_CONCURRENT` (default 2, ceiling 4).

**States and transitions.** Every transition is a single-statement compare-and-set (`UPDATE … WHERE id=? AND status=?`). Terminal states never change.

| From | To | Trigger |
|------|----|---------|
| QUEUED | RUNNING | A worker claims the job. It sets `started_at` and `deadline_at = started_at + 2 × RESOLVER_TIMEOUT_SECONDS`. |
| QUEUED | CANCELLED | Cancel before start |
| RUNNING | SUCCEEDED | The proposed run's evidence row is inserted **in the same transaction** |
| RUNNING | FAILED | Resolver unavailable (Docker, image, network or proxy) or an internal error; `error_code` is set |
| RUNNING | CANCELLED | Cancel during either run |
| RUNNING | TIMED_OUT | A run deadline or the job deadline is reached |
| QUEUED or RUNNING | INTERRUPTED | Application start-up only |

**Execution (sequential, two runs).**
1. Job setup: create `dua-res-<job>` and start the proxy (ADR-010).
2. Run 1 (`original`): fresh container (ADR-009), then supervisor verification and upload dates (ADR-011).
3. Run 2 (`proposed`): fresh container, verification and dates.
4. Comparison (ADR-011).
5. Teardown.

Run 2 always takes place unless the job was cancelled, timed out or failed. An original result of CONFLICT, UNSUPPORTED or RESOLUTION_FAILED does not stop it.

**Timeout hierarchy** (one setting, `RESOLVER_TIMEOUT_SECONDS` = T, default 180, range 1..600; everything else is derived):

| Level | Bound | Covers | On expiry |
|-------|-------|--------|-----------|
| Job | `deadline_at = started_at + 2T` (default 360 s, maximum 1200 s). Time spent QUEUED is not counted. | Setup, both runs, comparison, the final transaction | The current run is aborted as below and the job becomes TIMED_OUT |
| Run | `run_deadline = min(run_start + T, deadline_at)` | Input write, container start, pip execution, stdout read, supervisor verification and upload-date lookup for that role | The job becomes TIMED_OUT and no evidence is stored for this run. After run 1, run 2 is not started. |
| Container | Killed (`docker kill`, SIGKILL; `--init` reaps children) at `run_deadline` if still running. No separate setting. | pip inside the container | As for the run |
| Supervisor request | Each metadata request uses `min(8 s registry default, run_deadline − now)`. A retry is attempted only if time remains. | Index, `.metadata` and JSON API fetches | `fetch_failed`. During verification this means RESOLUTION_FAILED for the run (a completed classification). For an upload date it means `date unavailable`. Reaching `run_deadline` means TIMED_OUT. |
| Cleanup | Fixed internal bound of 30 s (not configurable), started after the terminal state is recorded | Removing resolver containers, the proxy and the network | Logged with the job ID. Leftovers are removed by the labelled `dua-res-*` sweep, which runs at start-up and before each job starts. The sweep removes only resources whose `dua.job` label names a job that is not QUEUED or RUNNING, so it never touches a concurrent job's containers. |

Therefore:
- the terminal state is recorded no later than `deadline_at`, so NFR-13's job bound of twice the run timeout holds;
- containers and networks live at most `deadline_at + 30 s` unless cleanup fails, in which case the sweep removes them;
- run 2 gets the full T unless setup and run 1 together exceed T, in which case the job deadline shortens run 2.

**Cancellation propagation.**
1. `POST /api/jobs/{id}/cancel` sets `cancel_requested` with a compare-and-set.
2. A QUEUED job becomes CANCELLED at once.
3. For a RUNNING job, the runner's watcher checks the flag at least once per second. It kills the current container, abandons in-flight supervisor requests (their remaining time is set to zero), skips any remaining run, records CANCELLED, then cleans up.
4. Target: the terminal state is recorded within 5 s of the request.

**Evidence persistence.**
- **When a row is written:** only when a run *completes*, meaning the supervisor reached a classification for that role within its run deadline: COMPATIBLE, CONFLICT, UNSUPPORTED or RESOLUTION_FAILED (`verification_failed` or `fetch_failed`).
- **Original row:** inserted when run 1 completes.
- **Proposed row:** inserted together with the SUCCEEDED transition.
- **When nothing is written:** cancelled, timed-out, interrupted and resolver-unavailable runs store nothing for the unfinished run. A completed original row stays as informational evidence, but approval can never use it.

**Plan status shown after a job:**
- the latest matching `proposed` record's status if one exists;
- otherwise, from the job: TIMED_OUT gives RESOLUTION_FAILED (`timeout`); FAILED for an unavailable resolver gives RESOLVER_UNAVAILABLE; CANCELLED or INTERRUPTED gives UNKNOWN.

Only a COMPATIBLE `proposed` record with a matching fingerprint permits approval (ADR-011).

**Restart.** At start-up, QUEUED and RUNNING rows become INTERRUPTED, with no evidence written for unfinished runs. Then the `dua-res-*` sweep runs.

**Retention.** Jobs and resolution records cascade with the run (M11).

## Consequences
- **Gained:** requests return immediately, cancel works, and no job claims success after a crash.
- **Cost:** a job takes up to twice as long as a single resolution. Throughput is bounded by design (at most 2 concurrent jobs by default, so 2 resolver containers and 2 proxies).
- **Unchanged:** validation stays synchronous, and F11 remains for validation.
- **Not yet verified:** the timeout and cancel figures are design targets until M12 criteria 9 and 10 pass.
