# Completion report: Dependency Upgrade Assistant 2.0

<!-- Gate G5. Honesty section first: what is NOT verified matters more than what is. -->

## Summary
DUA 2.0 is a local web application (FastAPI backend, React 19 frontend, SQLite) that takes a public
GitHub repository or a pasted manifest through analysis, recommendations, an upgrade plan and diff, a
**verified compatibility check** (Python), explicit human approval, sandbox validation and export.

History: a first G5 presentation after M8 was **rejected** on 2026-11-11 because the product did not meet
its approved intent (repository acquisition and multi-manifest discovery were missing from the 2.0 API and
UI, approval was client-side only, validation was not honest). Requirements Revision 2 re-planned M9–M11:
M9 redesigned the frontend, M10 delivered repository-first acquisition, server-held state and
server-enforced approval, and M11 hardened the backend (validation-policy binding, data-source honesty,
retention, request protections). Requirements Revision 4 added M12: complete Python dependency
resolution with pip in an isolated container, supervisor verification, original/proposed comparison,
SQLite jobs and fail-closed approval. All milestones M1–M12 are done and owner-approved (M12: event 331).

Delivered against intent:
- **Met:** repository acquisition and discovery (FR-1R/FR-2R), deterministic analysis with explicit data
  sources, recommendations with a deterministic fallback, plans and diffs, server-enforced approval bound
  to the diff, validation policy and verified resolution, honest sandbox validation, verified exports,
  the redesigned frontend, and the M12 compatibility engine (FR-22 to FR-32).
- **Deferred by approved scope decisions (Revision 2 and 4):** AST source inspection (FR-5), Monaco
  hunk-level review (FR-7, unified diff retained), SSE streaming (FR-12), private repositories and PATs,
  local filesystem paths, the history UI (FR-13), npm resolution (M13), `pyproject.toml` resolution,
  source builds, vulnerability advisories.

## Verification performed
| Check | Command | Result |
|-------|---------|--------|
| Clean-checkout install and run | Fresh clone of `feature/dependency-upgrade-assistant-2.0` at `ff99367` from GitHub; `python3 -m venv`; `pip install -e ".[dev]"`; `npm --prefix frontend ci`; `cp .env.example .env`; `python -m uvicorn src.api.main:app` | Install succeeded; `/api/health` healthy; `/docs` HTTP 200 |
| Full regression (clean checkout) | `python -m pytest -q -m "not live_resolution"` | 1234 passed, 9 skipped (Docker-only), 0 failed |
| Frontend (clean checkout) | `npx vitest run`; `npx tsc -b`; `npm run build`; `npm run lint` | 56 passed; type check pass; build pass; lint pass (2 warnings, 1 pre-existing) |
| CI, last code change | GitHub Actions [43285683462](https://github.com/Soham-Pasalkar/dependency-upgrade-assistant/actions/runs/43285683462) on `2d51424` | All 7 jobs green: Python 3.10–3.14 1242 passed each; frontend 56; Docker job: sandbox 74 + staging 45 + **M12 live 36** (`DUA_REQUIRE_DOCKER=1`) + headless-Chrome journey to a verified COMPATIBLE plan, approval, validation and ZIP download at 1440 and 390 px |
| CI, M12 submission | [43363543945](https://github.com/Soham-Pasalkar/dependency-upgrade-assistant/actions/runs/43363543945) on `ff99367` | All 7 jobs green |
| Live isolation (CI Docker) | `tests/test_egress_live.py` | Resolver has no direct network and no DNS; host gateway, docker0, host IP and loopback unreachable; proxy refuses other hosts, metadata address, other ports and plain HTTP (403) and missing/wrong tokens (407); proxy unreachable from other networks |
| Dependency audit (Python) | `pip-audit --skip-editable` in the clean-checkout venv | No known vulnerabilities in project dependencies. The only findings concern `pip` 26.0 itself (the venv's bootstrap tool, not a project dependency) |
| Dependency audit (npm) | `npm audit` | 0 vulnerabilities |
| Secrets scan, tree | `scan_secrets.py .` | PASS, 0 findings |
| Secrets scan, history | `scan_secrets.py . --history` | 4 findings, all triaged as synthetic test fixtures and detection patterns (redaction-test strings like `ghp_Secret…7890`, a truncated example key `MIIEow…`, scanner regexes) introduced on `main` in `4258719` and earlier milestones; no real credential. History was not rewritten |
| Tracked files | `git ls-files` review | No databases, `.env`, build output, caches or logs; largest files are milestone screenshot evidence (≤ 436 KB) |
| Documentation links | Relative links in README and docs | All resolve |
| README on GitHub | Headless Chrome on `github.com/…/blob/314e529/README.md` at 1280 and 390 px | Heading, logo, badges, tables and the Mermaid diagram render; no broken images; no horizontal overflow |
| Governance | `aidlc.py verify`; `validate_artifacts.py` | PASS; PASS |
| Performance (NFR-5) | 300 sequential GETs per endpoint against a local backend (Apple M4, macOS) | P95: health 1.6 ms, stored analysis run 1.3 ms, resolution status 3.6 ms (requirement: < 200 ms) |

## Not verified
- **Docker Desktop (macOS/Windows):** the resolver's network isolation (`--internal` plus `inhibit_ipv4`)
  is verified only on Linux with Docker 29 in CI. The development machine has no Docker.
- **arm64 hosts:** target facts for arm64 come from the pinned image's configuration; live tests ran only
  on x86_64 CI runners.
- **Production deployment:** DUA is designed as a local tool bound to `127.0.0.1`; no multi-user,
  authenticated or internet-facing deployment was designed or tested.
- **Load:** no load or soak testing; concurrency is covered by unit tests (pool bound, one job per plan)
  and two-job live runs only.
- **Advisory AI quality:** the Ollama advisory path is tested for availability, fallback and privacy, not
  for the quality of model output.
- **Repositories with real test suites through the UI:** the CI browser journey validates a pasted
  manifest ("No tests found"); repository validation with real tests is covered by the existing Docker
  staging suites, not by the browser journey.
- **pip inside the resolver image:** the image's own pip (24.0) was not separately audited for
  advisories; it runs isolated, binary-only and without credentials.
- **Rollback rehearsal and schema downgrade:** migrations are additive and tested forward on an M11-era
  database; downgrade and a rehearsed rollback were not performed.

## Security review
- **Threat model coverage:** M12 threats T-27 to T-38 each map to passing tests. Egress and SSRF
  (T-27/28/29/36/37) are covered by the live egress tests and 67 proxy unit tests. Evidence forgery
  (T-31/32/33/38) is covered by the forgery matrix on real reports, the forged report from inside the real
  container, the binding and replay tests, stale-evidence tests and parser bounds. Resource exhaustion and
  orphans (T-30/34) are covered by limit, timeout, cancel and sweep tests. Pre-M12 threats are covered by
  the M9–M11 suites (request protection, host/origin checks, validation-policy binding, retention, safe
  clone).
- **T-35 (malicious package content):** accepted by the owner at G2 at High inherent severity and Medium
  residual risk. Safeguards in place: compatibility disclosure in the UI and exports, introduced-package
  highlighting, exact-file upload dates, complete resolved lists. DUA does not detect malware or check
  advisories.
- **Configuration review:** the application binds `127.0.0.1`; `CORS_ORIGINS` is required in production
  and `*` is refused; `Host` and `Origin` are checked; `DEBUG` defaults to false. `.env.example` set
  `DEBUG=true`, which would add exception details to error responses; it now sets `DEBUG=false`.
  No secrets are required and `.env` is gitignored.
- **Open items:** see Residual risks.

## Performance
NFR-5 (P95 < 200 ms for cached queries) is met with large margin on the development machine (P95 ≤ 3.6 ms
for health, stored runs and resolution status; method above). NFR-9 and NFR-13 limits are enforced and
tested (clone, quotas, staging and resolver limits with ceilings). Resolution itself takes seconds to
tens of seconds per run, depending on the set and PyPI; it runs as a background job bounded by 2T
(default 360 s). No throughput target exists beyond the bounded pool (default 2 concurrent jobs).

## Documentation
- `README.md`: product overview, compatibility versus security, features, architecture diagram with trust
  boundaries, workflow, technology, install/run, configuration reference, Docker isolation, API, testing,
  scope and limitations.
- `docs/RESOLUTION_ENGINE.md`: statuses, time limits, isolation and trust model, configuration, tests and
  troubleshooting (runbook for the resolver).
- `docs/SETUP_AND_DEMO_GUIDE.md`: setup, configuration and step-by-step demo.
- `docs/aidlc/`: requirements, architecture, threat model, ADR-001 to ADR-012, milestone plan and reports.
- API reference: OpenAPI at `/docs` when the backend runs.
- Start/stop: README "Run". Rollback: revert the release merge on `main` (the feature branch is retained).

## Residual risks
- **T-35, malicious or compromised packages** (Medium, accepted at G2). Owner: project owner. Follow-up:
  vulnerability advisory integration (planned M13) and continued review of introduced packages.
- **Live PyPI dependence:** results and live tests depend on PyPI at run time; a removed release broke a
  live test once (numpy release candidates). Owner: maintainers. Follow-up: watch the CI Docker job and
  pin live test data to long-lived releases.
- **Docker Desktop isolation not verified.** Owner: maintainers. Follow-up: a verification run on Docker
  Desktop before recommending it, or document Linux as the supported resolver host (done in README).
- **Pinned image facts:** changing the image digest fails closed until `src/resolution/target.py` is
  re-verified. Owner: maintainers. Follow-up: re-run the live tests whenever the pin changes.
- **Version and license metadata:** the package and API report version `0.1.0`, and the public repository
  has no LICENSE file. Owner: project owner. Follow-up: decide the release version/tag and a license.
- **Historical synthetic secrets in git history** (scanner noise only). Owner: maintainers. Follow-up:
  none required; keep `aidlc:allow-secret` annotations on fixtures.

## Release recommendation
**Ship with conditions:**
1. Merge `feature/dependency-upgrade-assistant-2.0` into `main` only after G5 is approved and CI is green
   on the exact commit being merged.
2. Owner decision before tagging a public release: version number (metadata still says `0.1.0`) and a
   license (none is present; README states this).
3. Treat Linux with Docker Engine as the verified resolver host until Docker Desktop isolation is verified.
