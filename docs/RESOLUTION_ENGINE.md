# Python compatibility resolution engine (M12)

A package version existing on PyPI does not show that it works with the rest of your project. Before
a Python upgrade plan can be approved, validated, applied or exported, DUA has pip's own resolver
resolve the **complete proposed dependency set** in an isolated container, and a trusted part of the
server verifies the result independently. Anything short of a verified successful resolution blocks
the plan. There is no override.

Design records: requirements Revision 4 (FR-22 to FR-32), architecture Revision 4, threat model
Revision 4, ADR-009 to ADR-012 in [docs/aidlc](aidlc/).

## What "Compatible" means, and what it does not

**Compatible** means: pip resolved the complete proposed set for the validation target (CPython
3.11.18 on Linux, the pinned `python:3.11-slim` image), and the supervisor verified every selected
wheel against PyPI's own index and metadata.

It does **not** mean the packages have been vetted or are safe. DUA does not perform malware detection
or check vulnerability advisories (advisories are planned for M13). A compromised maintainer account or
a malicious new dependency would still resolve. To support your review, DUA shows:

- the packages the upgrade **introduces**, compared with the resolved set of your current manifest;
- the exact upload date of each selected file (or `date unavailable`);
- the complete resolved list, in the UI and in `compatibility-report.md`.

Compatibility also comes from metadata, not from running the code. Sandbox validation remains the
runtime check (for example, pandas 1.5.3 declares no upper bound on NumPy but is not
binary-compatible with NumPy 2).

## Statuses

| Status | Meaning | Approval |
|---|---|---|
| `COMPATIBLE` | The proposed set resolves and was verified | Allowed |
| `CONFLICT` | pip found conflicting requirements among available wheels, or a `Requires-Python` mismatch | Blocked |
| `UNSUPPORTED` | Input M12 cannot resolve: npm, `pyproject.toml`, `-r`, `-e`, URL/VCS/path requirements, index or other pip options, environment variables, constraints outside the repository, source-only packages, conflicts involving source-only releases | Blocked |
| `RESOLUTION_FAILED` | The check produced no verifiable evidence (time-out, malformed or forged output, verification failure, metadata unreachable, unverifiable marker) | Blocked |
| `RESOLVER_UNAVAILABLE` | Docker, the pinned image or the resolver network could not be used | Blocked |
| `UNKNOWN` | Not checked yet, cancelled, interrupted by a restart, or the plan/constraints/image/resolver policy changed since the last check | Blocked |

Approval binds the exact resolution fingerprint. Validation, apply and export re-check it, so a later
change to the plan's inputs, constraints files, the pinned image, pip policy or allow-list requires a new
check and a new approval. Approvals recorded before M12 must be renewed.

## How a check runs

1. `POST /api/proposals/{id}/resolve`. The supervisor classifies the input. Unsupported input is
   answered at once (`200`, no job). Otherwise a job is queued (`202`); one active job per plan.
2. A worker (pool of `RESOLVER_MAX_CONCURRENT`) creates a per-job Docker network and egress proxy, then
   runs **two** resolutions in fresh containers, one after the other: the current (original) manifest,
   then the proposed manifest. A failed original never stops the proposed run.
3. For each run the supervisor verifies the report against PyPI and stores evidence with its own
   fingerprint. Only the proposed record decides approval; the original record is the baseline for the
   introduced-package comparison.
4. `GET /api/jobs/{job_id}` shows progress (`current_role` original/proposed); `POST
   /api/jobs/{job_id}/cancel` cancels. `GET /api/proposals/{id}/resolution` returns the status, both
   records, the comparison and the disclosure.

### Time limits (one setting, `RESOLVER_TIMEOUT_SECONDS` = T)

| Level | Bound |
|---|---|
| Job | `started_at + 2T` (queued time excluded) |
| Run | `min(run_start + T, job deadline)`, covering the container, verification and upload dates |
| Container | killed (SIGKILL) at the run deadline |
| Supervisor request | `min(8 s, time left in the run)` |
| Cleanup | 30 s after the job ends, then a labelled sweep (at start-up and before each job) |

A time-out is reported as `RESOLUTION_FAILED (timeout)`, never as a conflict, and stores no evidence
for the unfinished run. On restart, queued and running jobs become `INTERRUPTED` with no evidence.

## Isolation and trust model

- **Resolver container** (per run): pinned `python:3.11-slim` by digest, uid 65534, read-only root,
  all capabilities dropped, `no-new-privileges`, `--init`, memory/CPU/process/tmpfs limits, DNS
  disabled, only a read-only input directory mounted, no writable host mount, no Docker socket, no
  application environment.
- **Command:** `pip install --dry-run --ignore-installed --only-binary=:all: --isolated --no-input
  --disable-pip-version-check --index-url https://pypi.org/simple --proxy … -r /in/requirements.txt`.
  Binary-only: no build backend, `setup.py` or package code runs; source-only packages are UNSUPPORTED.
- **Report channel:** pip writes `--report` to the container's tmpfs and its own output to stderr; the
  report is copied to stdout only when pip exits 0. (ADR-009 shows `--report -`; measured on
  2026-11-12, that mixes pip's progress into stdout, and `--quiet` hides conflict causes. This is the
  implemented refinement; stdout is still the only report channel.)
- **Network:** a per-job `--internal` Docker network with no host address
  (`com.docker.network.bridge.inhibit_ipv4`); the only way out is the job's proxy, which is also on the
  dedicated `dua-egress` bridge. The proxy accepts one `CONNECT host:443` per connection to
  `pypi.org` or `files.pythonhosted.org`, requires the job's token, resolves DNS itself, refuses any
  non-public address and connects to the address it validated; denials are logged and recorded as
  warnings.
- **Supervisor (trusted, in the backend):** treats the resolver's stdout and stderr as untrusted. It
  parses the report strictly, checks the runtime facts the container reports (Python 3.11.18, glibc
  2.41, uid 65534, architecture), matches every selected wheel by filename, URL and SHA-256 in PyPI's
  index, fetches PEP 658 metadata itself, and proves the closure (direct requirements, markers, extras,
  `--hash`, constraints, `Requires-Dist`, `Requires-Python`, nothing missing or unrelated, yanked and
  prerelease policy). Only exit 0 plus a verified report can be `COMPATIBLE`. Only the supervisor
  computes fingerprints and writes evidence.

## Configuration

All settings are server-side; values outside `1..ceiling` stop the backend from starting.

| Setting | Default | Ceiling |
|---|---|---|
| `RESOLVER_TIMEOUT_SECONDS` | 180 | 600 |
| `RESOLVER_MEMORY_MB` | 1024 | 4096 |
| `RESOLVER_CPUS` | 1 | 4 |
| `RESOLVER_PIDS_LIMIT` | 256 | 1024 |
| `RESOLVER_TMPFS_MB` | 512 | 2048 |
| `RESOLVER_MAX_CONCURRENT` | 2 | 4 |
| `RESOLVER_MAX_REPORT_MB` | 10 | 50 |
| `RESOLVER_MAX_PACKAGES` | 500 | 2000 |

Requirements: Docker reachable by the backend user; outbound HTTPS from the Docker host to
`pypi.org`/`files.pythonhosted.org` (proxy) and from the backend host (verification); the subnet pool
`172.30.252.0/22` free for per-job `/29` networks.

## Running the tests

```bash
# Unit, API and supervisor tests (no Docker needed; resolver doubles feed the real supervisor)
python -m pytest -q -m "not live_resolution"

# Live tests: real Docker, real pip in the pinned image, real proxy, live PyPI
DUA_REQUIRE_DOCKER=1 python -m pytest -v tests/test_egress_live.py tests/test_resolution_live.py
```

Without `DUA_REQUIRE_DOCKER=1`, live tests skip when Docker is unavailable. In CI they run once, in the
`Sandboxed Docker Container Validation` job, where Docker is required and its absence fails the job.

Recorded fixtures for the supervisor tests live in `tests/fixtures/resolution/` and can be re-captured
with `python3 tests/fixtures/resolution/capture_fixtures.py` (network required; runs no package code).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| *Resolver unavailable* | Docker is not running, the backend user cannot use it, or a network could not be created. Check `docker info`; the server log has the Docker error. |
| `cannot create the job network: … overlap` on every subnet | Another network uses `172.30.252.0/22`. Free it, or change `SUBNET_POOL` in `src/resolution/resolver_runtime.py`. |
| `environment_mismatch` | The pinned image changed without updating the verified facts in `src/resolution/target.py` (fails closed by design). |
| `unverifiable_marker` | A dependency's marker uses `platform_release`/`platform_version`, which depend on the host kernel and cannot be verified. |
| `fetch_failed` | The backend could not reach PyPI to verify the report. Check outbound HTTPS from the backend host. |
| `Not supported` for a Python plan | The manifest uses something M12 does not resolve; the panel lists each item with its line. |
| Check stays *Not verified* after a restart | The job was interrupted; run the check again. |
| Leftover `dua-res-*` containers or networks | Removed by the sweep at start-up and before each job; `docker ps -a --filter label=dua.resolution` lists them. |

## Known limitations

- Python `requirements*.txt` only; npm resolution is M13; `pyproject.toml` resolution, private indexes
  and other Python versions/platforms are not supported.
- Packages available only as source distributions cannot be `COMPATIBLE` in M12.
- Results depend on PyPI at the time of the check; evidence carries timestamps and expires with the
  plan. A release published between the two runs can change the comparison.
- The supervisor's source-only conflict check examines the packages pip names in the conflict, plus the
  user's own requirements.
- Docker Desktop on macOS/Windows has not been verified; CI on Linux (Docker 29) is the verified
  environment.
- Each job makes two resolver runs and their metadata fetches.
