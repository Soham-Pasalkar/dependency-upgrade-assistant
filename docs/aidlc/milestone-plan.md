# Milestone plan: Dependency Upgrade Assistant 2.0

<!-- Gate G3. One milestone = one bolt: independently verifiable, ideally hours to a day or two.
     Order by risk. Every milestone must also be registered with `aidlc.py milestone add`. -->

## Scope and Delivery Cutline

### P0 — Required for Demonstration (Target Delivery: Today, 10 November 2026)
- **FastAPI Backend Application:** Running with structured configuration, CORS, and `/api/health` endpoint.
- **React + TypeScript Frontend:** Scaffolded with Vite and connected to backend via unified API client.
- **Multi-Ecosystem Manifest Ingestion:**
  - `requirements.txt`: Safe parsing of PEP 508 / pip requirements (package names, version specifiers `==`, `>=`, `<=`, `~=`, `!=`, `<`, `>`, environment markers `; python_version >= ...`, inline comments `# ...`). Unsupported directives (`-e`, `-r`, `--find-links`, raw URLs) safely rejected with structured diagnostic warnings without executing untrusted code on the host.
  - `package.json`: Safe JSON parsing of `dependencies` and `devDependencies`. Preserves all unrelated manifest fields (`name`, `version`, `scripts`, `peerDependencies`, `license`) and formats with standard 2-space indentation. (Note: Standard JSON per RFC 8259 does not support comments; no JSON comment preservation is claimed).
- **Authoritative Package Registry Lookup:** Querying PyPI JSON API and npm registry API with in-memory TTL caching to determine latest releases and deprecation metadata.
- **Dependency Classification & Upgrade Recommendations:** Classifying dependencies into Up to Date, Upgrade Available, or Preserved. Generating semantic version recommendations with deterministic fallback active when AI is offline.
- **Surgical Manifest Modification & Unified Diff Generation:** Producing standard unified diffs (`.diff` / `.patch`) while preserving surrounding whitespace, environment markers, and inline comments in `requirements.txt`, and standard JSON formatting in `package.json`.
- **Human Review & Approval Gate:** Enforcing explicit user review and confirmation before workspace modifications are committed to the session.
- **Sanitized Artifact Export:** Generating downloadable unified `.patch` bundles, execution summaries, and SHA-256 checksums with automated secret scanning.
- **Minimum Working Demonstration Definition:**
  $$\text{Upload Manifest} \longrightarrow \text{Scan Dependencies} \longrightarrow \text{Query PyPI/npm} \longrightarrow \text{Recommend Upgrades} \longrightarrow \text{Review Diff} \longrightarrow \text{Human Approve} \longrightarrow \text{Export Patch}$$
  Successful delivery is established when a user can complete this complete end-to-end loop in the browser.

### P1 — Implement with Graceful Degradation (Feasible Today)
- **Ollama AI Integration (M3):** Provider-independent AI abstraction, dynamic model discovery (`/api/tags`), and prompt formatting. When Ollama daemon is inactive, automatically activates the deterministic SemVer fallback without blocking the user.
- **Docker Sandboxed Validation (M5):** Implemented with `--network none`, resource caps, and read-only mounts. Unit-tested with mocks. In the current host environment where Docker is unavailable, the environmental probe returns a truthful, diagnostic `NOT_RUNNABLE` status with actionable explanations.
- **Automated Secret Scanning (M6/M8):** Ensuring exported artifacts contain zero credentials, API keys, or sensitive environment tokens.

### P2 — Explicitly Deferred
- Complex multi-manifest lockfile graph solvers (`poetry.lock`, `package-lock.json`, `pnpm-lock.yaml`).
- Multi-container distributed test suites (Docker Compose orchestrations).
- Cloud AI provider integrations (OpenAI, Anthropic, Gemini BYOK).
- In-browser free-form line-by-line interactive patch editing.
- Production deployment infrastructure (Kubernetes, Celery, Redis).

## Delivery Schedule & Realistic Time Allocation (10 November 2026)

| Milestone | Scope & Priority | Target Duration | Window (Local Time) | Feasibility on Current Workstation |
|:----------|:-----------------|:----------------|:--------------------|:------------------------------------|
| **M1** | Application foundation (`fastapi`, React scaffold, SQLite) [P0] | 35 min | 19:30 – 20:05 | Fully feasible; installs `fastapi` in `.venv`; Node/npm available |
| **M2** | Manifest ingestion & dependency analysis (PyPI/npm) [P0] | 40 min | 20:05 – 20:45 | Fully feasible; real HTTP calls to public registries with caching |
| **M3** | Upgrade recommendations & AI provider (Ollama + Fallback) [P0/P1] | 35 min | 20:45 – 21:20 | Fully feasible; mock tests for Ollama; deterministic fallback active |
| **M4** | Upgrade planning, surgical diff & human review gate [P0] | 35 min | 21:20 – 21:55 | Fully feasible; pure Python AST/regex modification & unified diff |
| **M5** | Sandboxed validation (`--network none` runner & probe) [P1] | 30 min | 21:55 – 22:25 | Fully feasible; mock unit tests; probe reports `NOT_RUNNABLE` |
| **M6** | Artifact export (sanitized patch, checksums, secret scan) [P0] | 25 min | 22:25 – 22:50 | Fully feasible; file serialization, checksums, secret scanner |
| **M7** | Frontend integration & end-to-end interactive demo [P0] | 40 min | 22:50 – 23:30 | Fully feasible; React UI connecting all P0 endpoints |
| **M8** | Final verification, regression pass & demo guide [P0] | 25 min | 23:30 – 23:55 | Fully feasible; 246 baseline tests + new unit tests |
| **Total** | **Complete Working Prototype** | **4h 25m** | **Finishes 23:55 (Today)** | **100% within 10 November 2026 deadline** |

## Docker Sandbox Execution Design

To enforce defense-in-depth while allowing dependencies to be tested under strict network isolation (`--network none`), the sandbox runner utilizes a **2-stage decoupled execution model**:

```
[ Stage 1: Preparation / Staging ]
   (Controlled staging environment with outbound network access)
   pip download --dest /staging/wheels -r requirements.txt (or npm pack/cache)
                       │
                       ▼
[ Stage 2: Sandboxed Test Execution ]
   docker run --network none \
              --read-only \
              -v /staging/wheels:/cache:ro \
              -v /workspace/repo:/workspace/repo:ro \
              --tmpfs /tmp:rw,noexec,nosuid,size=64m \
              --tmpfs /workspace/scratch:rw,size=128m \
              --memory=512m --cpus=1.0 --pids-limit=64 \
              --security-opt=no-new-privileges --cap-drop=ALL \
              --user 1000:1000 \
              test-image pytest
```

1. **Dependency Acquisition (Stage 1):** Dependencies are downloaded in a separate preparation stage into a local staging directory (`/staging/wheels` for Python, local cache for npm) without executing target project test suites or arbitrary build scripts.
2. **Offline Dependency Availability (Stage 2):** The prepared wheel/tarball cache is mounted into the test container as a **read-only volume** (`-v /staging/wheels:/cache:ro`). The test container installs dependencies strictly from local files (`pip install --no-index --find-links=/cache -r requirements.txt`) under `--network none`.
3. **Read-Only Mounts:** Root filesystem (`--read-only`), repository source reference (`/workspace/repo:ro`), and dependency cache (`/cache:ro`) are mounted strictly read-only.
4. **Writable Mounts:** Only isolated RAM tmpfs mounts (`--tmpfs /tmp:rw,noexec,nosuid,size=64m` and `--tmpfs /workspace/scratch:rw,size=128m`) are writable. Host repository files are never mounted read-write.
5. **Resource Limits & Containment:** `--memory=512m`, `--cpus=1.0`, `--pids-limit=64`, `--security-opt=no-new-privileges`, `--cap-drop=ALL`, `--user 1000:1000`.
6. **Timeouts & Cleanup:** Hard 60-second execution timeout via Python subprocess wrapper (`SIGTERM` followed by `SIGKILL` after 5s grace period). Container cleanup runs fail-closed via `docker rm -f <id>` inside `try...finally`, integrated with the Universal Lock-First protocol.
7. **Truthful Environment Reporting:** When Docker is not installed or the daemon socket is inaccessible, the runner probes the environment and returns an explicit, truthful `NOT_RUNNABLE` status with diagnostic explanation (`"Docker daemon not detected on host workstation. Sandboxed test execution skipped."`). Mocked tests are never represented as live container runs.

## Current Environment Limitations & Operational Impact

1. **Python Environment:** Python 3.12 active in `.venv`. `fastapi` and `python-multipart` are not yet installed. M1 includes installing them into `.venv`.
2. **Docker Daemon:** Command `docker` is not installed on PATH. Live container execution cannot run on this workstation. M5 implements the runner, tests it with unit mocks, and verifies that the live environment probe returns `NOT_RUNNABLE`.
3. **Ollama Service:** Ollama client v0.40.14 is installed, but daemon is inactive. M3 implements the client with mock tests, and the deterministic fallback provides immediate, zero-downtime version recommendations.
4. **Node.js & npm:** Node v25.9.0 and npm 11.12.1 are present and fully operational for scaffolding, building, and serving the React frontend.

## Milestones
| ID | Title | Depends on | Acceptance criteria | Test approach | Risk |
|----|-------|------------|---------------------|---------------|------|
| M1 | Application foundation | none | FastAPI backend initialized with configuration, CORS, and `/api/health` endpoint; React frontend scaffolded with Vite; SQLite persistence foundation initialized; backend test suite passes | Unit and integration tests for FastAPI startup, health checks, config loading, and SQLite connection | Fast-paced full-stack setup overhead; mitigated by lightweight Vite scaffold and modular FastAPI routers |
| M2 | Manifest ingestion and dependency analysis | M1 | Multipart and raw upload endpoints accept `requirements.txt` and `package.json`; PEP 508 requirements safely parsed; PyPI and npm registry clients query available versions with caching; outdated packages classified; unsupported directives and malformed manifests fail gracefully with diagnostic errors | Unit tests on manifest fixtures, mock PyPI/npm API responses, invalid syntax error handling, and registry timeout simulation | Registry rate limits or network latency; mitigated by in-memory caching and mock fallbacks |
| M3 | Upgrade recommendations and AI provider | M2 | Provider-independent AI interface defined; Ollama service implemented with dynamic model discovery; structured recommendation output generated; AI versions validated against registry ground truth; deterministic fallback operates when Ollama is offline | Unit tests for Ollama client mocking, model discovery, prompt construction, structured parsing, registry verification, and fallback activation | Ollama daemon offline; mitigated by deterministic semantic-versioning fallback ensuring continuous operation |
| M4 | Upgrade planning and patch generation | M3 | Upgrade plan endpoint accepts selected candidate packages; manifest modifier applies changes preserving formatting and comments in `requirements.txt` and standard formatting in `package.json`; unified diff generator produces standard diffs; human approval gate enforced prior to workspace mutation | Unit tests for manifest modification accuracy, formatting preservation, unified diff syntax validation, and approval gate enforcement | Formatting corruption; mitigated by AST/regex surgical line replacements preserving whitespace and comments |
| M5 | Sandboxed validation | M4 | Docker runner executes test commands inside container with `--network none` and resource caps; execution logs and exit codes captured; truthful reporting of `PASS`, `FAIL`, and `NOT_RUNNABLE` when Docker is unavailable; fail-closed container cleanup | Integration tests mocking Docker CLI/socket, container execution timeout simulation, exit code parsing, and verification of truthful `NOT_RUNNABLE` reporting | Docker daemon not installed or running; mitigated by environment probe returning diagnostic `NOT_RUNNABLE` status |
| M6 | Artifact export | M5 | Export service bundles changes into downloadable `.patch` files and JSON execution summaries; complete execution logs and SHA-256 checksums generated; secret sanitization scanner verifies zero credential leakage; download endpoints functional | Unit tests for export file integrity, checksum calculation, secret redaction filter verification, and export endpoint testing | Accidental secret leakage in logs; mitigated by pre-export secret scanner and header/token redaction filters |
| M7 | Frontend integration and end-to-end demonstration | M6 | React UI connects to FastAPI across complete workflow (Upload -> Scan -> Recommend -> Diff -> Review -> Test -> Export); displays dependency badges, metadata, and diffs; displays test logs and truthful status badges; implements responsive loading, empty, and error states | Component tests, mock API integration tests, responsive state checks, and accessibility contrast verification | Integration mismatches or CORS errors; mitigated by shared TypeScript interfaces and unified Vite proxy |
| M8 | Final verification | M7 | Full test suite passes 100% green with zero regressions across pre-existing and new tests; end-to-end workflow verified against Python and Node.js fixtures; secret scanning and artifact validation pass with 0 errors; setup and demo guide documented | Full pytest test suite execution, secret scanner audit, end-to-end verification script, and artifact verification | Time pressure before end-of-day deadline; mitigated by tightly scoped vertical slices and continuous verification |

## Test strategy
The verification strategy distinguishes six distinct testing layers to ensure honest reporting without conflating mock validations with live environment capabilities:

1. **Existing Baseline Tests (246 tests in `tests/`):** All pre-existing tests covering catalog loading, AST parsing, diff generation, and concurrency reference models must remain 100% green across all development activities.
2. **New Unit Tests:** Fast, isolated unit tests covering manifest parsing (`requirements.txt`, `package.json`), surgical line modification, registry response caching, prompt formatting, unified diff synthesis, and checksum calculations.
3. **Mocked Integration Tests:** FastAPI `TestClient` endpoint tests verifying request validation, session state, and error handling with mocked external dependencies (PyPI, npm, Ollama, Docker socket). Mocked tests prove code correctness and error handling, but are never represented as proof of live external service availability.
4. **Real Environment Verification (Host Workstation):**
   - **Docker:** Probed via environment capability check; correctly and truthfully reports `NOT_RUNNABLE` due to absent daemon.
   - **Ollama:** Probed via local HTTP check; correctly and truthfully activates deterministic fallback when daemon is inactive.
   - **Package Registries:** Real outbound HTTP GET queries to PyPI JSON API and npm registry API for public metadata.
5. **Real End-to-End Browser Verification:** Manual and automated localhost walkthrough of the complete user workflow (Upload -> Scan -> Recommend -> Diff -> Review -> Test -> Export) on `http://localhost:5173`.
6. **Secret Scanning Verification:** Automated scanning via `scan_secrets.py` verifying that all newly created code, manifests, and exported artifacts contain zero credentials, while recognizing the 4 pre-existing mock test fixtures in `tests/test_code_inspector.py`.

## Risks
- Docker daemon unavailable or unprivileged: Target environments (such as developer workstations without Docker or restricted CI environments) cannot run Docker containers. Mitigation: Environmental detection probe checks Docker daemon connectivity and returns an honest, diagnostic `NOT_RUNNABLE` status with actionable explanations, never falsely claiming test success.
- Package registry downtime or rate limiting: External PyPI or npm APIs may be down, throttled, or unreachable. Mitigation: In-memory TTL caching prevents duplicate queries, and resilient timeout handling provides cached or diagnostic error responses.
- Local Ollama instance offline or model missing: Developers may not have Ollama running or the requested model pulled. Mitigation: The AI provider abstraction automatically falls back to deterministic heuristic version recommendations based on semantic versioning rules, ensuring the entire workflow functions without blocking.
- LLM version hallucination: AI models may recommend nonexistent, vulnerable, or malicious package versions. Mitigation: Strict ground-truth validation checks all AI recommendations against verified package versions retrieved from PyPI/npm registries, discarding any unverified version before presenting it to the user.
- Secret or credential leakage: Uploaded manifests, environment variables, or test outputs might contain sensitive tokens or keys. Mitigation: In-memory-only token handling (`GIT_ASKPASS`), automated regex-based secret scanning on all exported files, and strict `.gitignore` enforcement.
- Manifest formatting clobbering: Parsing and serializing manifests might inadvertently strip comments, whitespace, or environment markers. Mitigation: Surgical line-replacement modifiers preserve surrounding lines, indentation, and inline comments in `requirements.txt`, and standard 2-space indentation in `package.json` (noting standard JSON does not support comments).

## Revision 2 milestones — one-day sprint (2026-11-11)
Owner direction: deliver today, with the frontend first and receiving most of the iteration time. There are two milestones, and so two G4 checkpoints. M1–M8 remain `done` as the baseline. Supersedes the earlier ordering (backend first) presented on 2026-11-11.

| ID | Title | Depends on | Acceptance criteria | Test approach | Risk |
|----|-------|------------|---------------------|---------------|------|
| M9 | Premium frontend redesign and existing workflow integration | M8 | (1) Baseline backend and frontend tests re-run and recorded before changes. (2) Design token system (ADR-008) and reusable components on React 19 + Vite, with no stack change. (3) Product landing page per FR-19: navigation, hero, working primary and secondary actions, product visual built from real interface components, workflow and capability sections, and a smooth entry into the app. (4) Redesigned application covering the existing working journey (manifest analysis → health → recommendations → plan/diff → approve → validate → export) against the current APIs, with no fake controls. (5) FR-21 content discipline: no internal development content, and verified results distinguished from AI and heuristic suggestions. (6) Real-browser screenshots at 1440 px and 390 px reviewed and defects fixed; NFR-11/NFR-12 checks. (7) Frontend tests, lint and build pass, and backend suite unchanged. | Vitest component tests updated, keeping behaviour assertions. Live browser run against the running backend; screenshots reviewed after each design iteration and stored under `docs/aidlc/reports/m9-screens/`. | Medium: visual quality. Mitigated by repeated screenshot critique cycles. |
| M10 | Repository-first backend integration and end-to-end completion | M9 | (1) `POST /repositories/analyze` performs FR-1R/FR-2R against a real public GitHub repo, with NFR-9 limits enforced during acquisition and NFR-10 / T-15–T-18 mitigations. (2) Server-authoritative RunStore and server-recorded approval; validation, apply and export return 409 without approval (FR-15/FR-16). (3) PyPI large-metadata fix and error taxonomy (FR-17). (4) Host-header validation (NFR-1R). (5) Workspace-based validation with honest statuses; offline Python and npm staging per ADR-007, with any unverified part reported as a limitation (FR-18). (6) Redesigned frontend is repository-first and completes the flow from GitHub URL to downloaded artifacts (FR-19/FR-20). (7) Browser-level end-to-end run with responsive fixes. (8) Full backend and frontend suites green per NFR-7R. | New unit tests for URL/SSRF validation, clone flags, watchdog limits, RunStore, approval gating, registry parsing and Host header. Live analysis of a small public repo and of numpy metadata. Browser end-to-end run with screenshots. Docker paths covered by unit tests locally and by the GitHub Actions Docker job when pushed with consent. | High: security-sensitive acquisition. Mitigated by reusing tested code and a test per threat. |

### Test strategy (Revision 2)
Each milestone keeps the full backend and frontend suites green. Every new threat (T-15–T-22) gets at least one automated test. Live evidence comes from real commands: public GitHub repo analysis, a real browser session, and CI for Docker. Anything that could not be run is marked "not verified" in the milestone report.

### Risks (Revision 2)
- **One-day schedule.** Scope is held by the Revision 2 deferrals. The frontend gets most of the time.
- **M9 depends on the current API, which M10 changes** (IDs and approval). The frontend API layer is kept in `api/client.ts` so the switch is localised.
- **No local Docker.** Live sandbox evidence depends on GitHub Actions and needs the owner's consent to push.
- **Network dependence.** The demo needs GitHub and the registries; failures surface as typed errors.

## Revision 3 milestones — post-M10 stabilization (2026-11-12)
Owner direction: two separate milestones. M11 establishes a verified backend checkpoint. The Dependency Compatibility & Resolution Engine follows as M12. M12 will be registered separately with its own requirements and architecture amendments; it is not part of this plan revision. The uncommitted post-M10 fixes F1–F7 (note N8) are carried into M11's verification scope.

| ID | Title | Depends on | Acceptance criteria | Test approach | Risk |
|----|-------|------------|---------------------|---------------|------|
| M11 | Backend security and reliability stabilization | M10 | (1) F13: the server alone determines the validation image and test command from trusted configuration; clients cannot supply either; the validation configuration is bound into the approval fingerprint and re-verified at approval, validation and export; a no-op or substituted command can never yield PASSED; missing, skipped, failed and passed tests are distinguishable. (2) F10: explicit registry data source and status (live registry, offline catalog, unavailable, unknown) in the analysis and recommendation APIs and the UI; offline data is never labelled registry-verified and never counts as compatibility evidence; API documentation updated. (3) F8: bounded, idempotent export retention that survives restarts, removes orphans and keeps unexpired or referenced artifacts. (4) F9: bounded SQLite retention for expired runs, manifests, plans and approvals via cascading deletes, never touching unexpired work. (5) F11 and F12 limitations documented with the M12 direction. (6) F1–F7 re-verified. (7) Full backend and frontend suites, lint and build pass; Docker integration tests run in CI on the M11 commit. | Regression tests: the former false-PASSED scenario; approval invalidation on validation-configuration change; offline-status API and UI tests; retention boundary, restart and relationship tests. Full suites locally. CI Docker job on the exact commit once the owner authorizes a push. | Medium: F10 and F13 change public API fields; mitigated by explicit documentation and contract tests. |

### Test strategy (Revision 3)
Every fix has a regression test that fails before the fix. Docker-dependent evidence comes from CI on the exact commit. Skipped and not-run checks are reported as such.

### Risks (Revision 3)
- **API changes:** F13 removes client-supplied `custom_test_cmd` and `image_override`; F10 adds an explicit data-source field. These are documented, and the frontend is updated where it consumes them.
- **No local Docker:** Docker evidence depends on CI and needs the owner's push authorization.

## Revision 4 milestones — M12 (2026-11-12)
This section adds to the earlier revisions, and M1 to M11 are unchanged. It implements requirements Revision 4 (G1, event 299) and architecture and threat model Revision 4 with ADR-009 to ADR-012 (G2, event 301). M12 is registered (event 302), depends on M11, and is not started. Construction requires G3 approval.

| ID | Title | Depends on | Acceptance criteria | Test approach | Risk |
|----|-------|------------|---------------------|---------------|------|
| M12 | Python Dependency Compatibility & Resolution Engine | M11 | Criteria 1–14 below, each with named evidence | Unit, API, security, live-Docker integration in CI (Docker required), frontend, browser end-to-end | High: new network-egress boundary and evidence trust model |

### M12 acceptance criteria and required evidence
"Docker" means the test needs a live Docker daemon. In CI those tests run in the Docker job with `DUA_REQUIRE_DOCKER=1`, so they fail rather than skip. A criterion counts as met only when its listed evidence has passed.

| # | Criterion | What is verified | Evidence (test) | Type | Docker |
|---|-----------|------------------|-----------------|------|--------|
| 1 | Real pip resolution, compatible and conflicting | `numpy==1.24.0`, `scipy==1.10.0`, `pandas==1.5.3` gives COMPATIBLE; `numpy==2.1.0` with `scipy==1.10.0` gives CONFLICT naming `scipy 1.10.0 depends on numpy<1.27.0` | `tests/test_resolution_live.py::test_numpy_scipy_compatible` and `::test_numpy_scipy_conflict` | Integration | Yes |
| 2 | Transitive dependencies, markers, extras, constraints, `Requires-Python` | Transitive packages reported and verified; a marker-excluded dependency is absent; extras' dependencies present; a repository constraints file changes the outcome; a `Requires-Python` conflict gives CONFLICT; a transitive conflict between two direct dependencies is detected | `test_resolution_live.py::test_transitive`, `::test_markers`, `::test_extras`, `::test_constraints_file`, `::test_requires_python_conflict`, `::test_transitive_conflict`; supervisor closure unit tests on captured real reports (`tests/test_resolution_verifier.py`) | Integration + unit | Live: yes; unit: no |
| 3 | Prerelease and yanked policy | No prerelease unless pinned; a pinned prerelease is allowed; a yanked `==` pin resolves with a warning; yanked releases are not chosen for ranges | `test_resolution_live.py::test_prerelease_policy`, `::test_yanked_pin_warning`; unit tests on captured reports | Integration + unit | Live: yes |
| 4 | Source-build handling | An sdist-only requirement (e.g. `pycrypto==2.6.1`) gives UNSUPPORTED `source_build_required`, never CONFLICT or COMPATIBLE; pip runs with `--only-binary=:all:`; no build backend runs (pip stderr has no build steps; the input mount stays unmodified); a conflict involving source-only releases gives UNSUPPORTED | `test_resolution_live.py::test_source_only_unsupported`; `tests/test_resolution_input.py`, `test_resolver_runtime.py::test_pip_args_binary_only` | Integration + unit + security | Live: yes |
| 5 | Structured report and server-computed fingerprint | All report fields present; the fingerprint is computed by the supervisor; resolver stdout cannot set the status or fingerprint; **forgery tests:** a report claiming an unsatisfied `Requires-Dist` closure, wrong hashes, a non-allow-listed artifact host, extra packages, malformed JSON or excess size each give RESOLUTION_FAILED | `tests/test_resolution_verifier.py` (forgery matrix on captured real reports); `tests/test_resolution_fingerprint.py` | Unit + security | No |
| 6 | Input-change invalidation | Changing the proposal diff, original or proposed manifest, constraints, image digest, pip policy arguments or allow-list version changes the fingerprint; stored evidence then counts as UNKNOWN and approval is blocked | `test_resolution_fingerprint.py` (one case per input); `tests/test_resolution_enforcement.py::test_stale_evidence_blocks` | Unit + API | No |
| 7 | Fail-closed enforcement | 6 statuses × {approve, validate, apply, export}: only COMPATIBLE with a matching fingerprint passes; client-supplied status or fingerprint fields are ignored; pre-M12 plans are UNKNOWN and blocked; M11 validation-policy enforcement is unchanged; npm plans are UNSUPPORTED and blocked | `tests/test_resolution_enforcement.py` (matrix), existing M11 suites | API + security | No |
| 8 | Egress isolation in the real Docker setup | From inside a resolver container built by the production runtime: direct TCP to a public IP fails; DNS lookup fails; `CONNECT` to a non-allow-listed host is refused and logged; `CONNECT` to 169.254.169.254, the Docker gateway, the host IP and an RFC 1918 address is refused; `CONNECT pypi.org:22` is refused; plain HTTP is refused; `CONNECT pypi.org:443` succeeds; the proxy is not reachable from a container on another network. Proxy unit tests: rebinding (the validated IP is used), allow-list, ports, methods, denial log format | `tests/test_egress_live.py` (Docker), `tests/test_egress_proxy.py` (unit) | Security integration + unit | Live: yes |
| 9 | Resource limits and timeout | Generated `docker run` arguments include non-root, read-only, capabilities dropped, no-new-privileges, `--init`, memory, CPU, processes, tmpfs size, read-only input mount, no writable host mount, no socket, internal network only, DNS disabled; timeout hierarchy (ADR-012): a run exceeding T gives TIMED_OUT and RESOLUTION_FAILED (`timeout`), with containers killed at the run deadline and no evidence for that run; a run-1 timeout prevents run 2; the job's terminal state is recorded by `started_at + 2T`; supervisor requests are bounded by the remaining run time; cleanup finishes within 30 s or the sweep removes leftovers; limit settings outside their ceilings are refused at startup | `tests/test_resolver_runtime.py` (args), `tests/test_resolution_config.py` (ceilings), `test_resolution_live.py::test_timeout_kills_containers`, `tests/test_job_service.py::test_timeout_hierarchy` (fake clock: run deadline, job deadline, run-2 budget shortened by slow setup) | Unit + integration | Live: yes |
| 10 | SQLite job lifecycle, cancellation and restart recovery | Two runs per job (original, then proposed), each with its own NFR-13 limits; cancel or timeout during either run stores no evidence for that run. Each state transition, with concurrent-transition rejection (compare-and-set); one active job per plan; pool bound; cancel: a QUEUED job is cancelled at once; a RUNNING job's watcher (≤ 1 s) kills the current container, skips the remaining run and records CANCELLED within 5 s, and removes the network; evidence is stored only for completed runs (an original row may remain, a proposed row never without SUCCEEDED); INTERRUPTED after a simulated restart with no evidence stored; startup sweep removes labelled leftovers but never a concurrently active job's resources; retention cascade | `tests/test_job_service.py` (unit, fake runner), `test_resolution_live.py::test_cancel_cleans_up`, `::test_restart_sweep` | Unit + integration | Live: yes |
| 11 | Minimal UI integration | Compatibility panel states (checking, compatible, conflict, unsupported, failed, unavailable, unknown); cancel works; approval disabled with the reason; npm "not yet supported" message; no redesign | `frontend/src/components/*.test.tsx`; headless-Chrome journey (RESOLVER_UNAVAILABLE path locally; COMPATIBLE path requires Docker) | Component + end-to-end | E2E COMPATIBLE: yes |
| 12 | CI Docker integration | The CI Docker job runs `test_resolution_live.py` and `test_egress_live.py` with `DUA_REQUIRE_DOCKER=1` on the M12 commit; all jobs green | GitHub Actions run on the M12 commit | CI | Yes |
| 13 | Documentation and backward compatibility | Revision 4 requirements, architecture, threat model and ADR-009 to ADR-012 match the implementation; supported and unsupported cases documented; pre-M12 records intact and UNKNOWN; additive migration tested on an M11-era database | `tests/test_resolution_migration.py`; `validate_artifacts.py`; reviewer check | Unit + doc | No |
| 14 | Compatibility disclosure and resolved-set review (FR-32) | (a) Every COMPATIBLE view and the Markdown export carry the disclosure "resolves, not vetted or safe; DUA does not perform malware detection", and no UI or export text claims safety. (b) A job runs the original then the proposed resolution under identical settings, storing separate evidence and fingerprints; introduced packages are exactly proposed set minus original set; when the original is not COMPATIBLE the comparison shows `comparison unavailable` with that status and nothing is flagged; the comparison never changes approval. (c) Upload date is the selected file's time matched by filename and SHA-256 (PEP 700 `upload-time`, else JSON API `upload_time_iso_8601`); source and retrieval time recorded; an unmatched file or missing time shows `date unavailable`; a multi-file version uses the selected file's time. (d) The complete list (name, version, direct or transitive, introduced, date) is in the UI and the export. | `tests/test_resolution_comparison.py` (set difference, unavailable cases, approval unaffected); `tests/test_upload_dates.py` (multi-file selection, PEP 700 and JSON API sources, no match, missing field, no fallback to other files); `tests/test_export_resolution.py`; component tests; `test_resolution_live.py::test_original_and_proposed_runs` (e.g. scipy upgrade introducing a new transitive package) | Unit + API + component + integration | Live: yes |


### Construction sequence (phases inside M12)
M12 is a single milestone with one G4 review. The phases below order the work inside it. They are not separate milestones.

| Phase | Work | Main modules | Depends on |
|-------|------|--------------|------------|
| P1 | Data models, additive schema migration, resolver settings and ceilings | `src/resolution/models.py`, `src/services/run_store.py` (`dua_jobs`, `dua_resolutions`, `dua_approvals.resolution_fingerprint`), `src/api/config.py` | — |
| P2 | Input classification and per-role fingerprinting | `src/resolution/python_input.py`, `src/resolution/fingerprint.py` | P1 |
| P3 | Trusted supervisor: report verification, PyPI metadata, upload dates, comparison logic, pip exit and stderr mapping | `src/resolution/verifier.py`, `src/resolution/comparison.py`, `src/resolution/python_resolver.py` | P1, P2 |
| P4 | Egress proxy | `src/resolution/egress_proxy.py` | P1 |
| P5 | Docker resolver runtime: networks, proxy and resolver containers, bounded stdout, kill at deadline, labelled sweep | `src/resolution/resolver_runtime.py` | P2, P4 |
| P6 | SQLite job lifecycle: two sequential runs, deadlines, cancel, restart, evidence persistence, comparison wiring | `src/services/job_service.py` | P1, P3, P5 |
| P7 | Fail-closed enforcement at approve, validate, apply and export | `src/services/approval_service.py`, the affected routes | P1, P2 |
| P8 | Resolution API endpoints and Markdown export section | `src/api/routes/` (resolve, jobs, cancel, resolution), `src/services/exporter.py` | P6, P7 |
| P9 | Frontend compatibility panel (existing design tokens, no redesign) | `frontend/src/` | P8 |
| P10 | Live Docker test suites and CI wiring (`DUA_REQUIRE_DOCKER=1`) | `tests/test_resolution_live.py`, `tests/test_egress_live.py`, the CI workflow | P5, P6, P8 |
| P11 | Documentation, full regression, security scans, milestone report | docs, `docs/aidlc/reports/M12.md` | All |

**Changes from the suggested order, and why:**
- **The supervisor (P3) comes before the Docker runtime (P5).** The supervisor is the trust anchor, and the runtime's output means nothing without it. It can be fully tested without Docker on captured real pip reports.
- **The comparison's pure logic is built in P3,** next to the verifier it depends on. It is wired into jobs in P6.
- **The proxy (P4) comes before the runtime (P5),** because the runtime starts it as part of every job.
- **Enforcement (P7) lands before any new endpoint or UI (P8, P9),** so no resolution path is ever exposed without fail-closed checks.

**Test fixtures:** real pip reports and PyPI index and metadata responses are captured once on the development host. They come from `pip install --dry-run --ignore-installed --only-binary=:all: --report`, which runs no package code. They are stored under `tests/fixtures/resolution/` with the command and date that produced them.

### Acceptance criteria mapped to implementation and evidence
None of these criteria is implemented or verified yet. "Docker CI" means the test runs in the CI Docker job with `DUA_REQUIRE_DOCKER=1`. Docker is not installed on the development machine, so all live evidence comes from CI.

| # | Phase | Modules | Unit tests | Integration or API tests | Security tests | Docker CI tests | Completion evidence |
|---|-------|---------|------------|--------------------------|----------------|-----------------|---------------------|
| 1 | P3, P5, P6, P10 | `python_resolver`, `resolver_runtime`, `verifier` | Status mapping from captured compatible and conflict reports and stderr | — | — | `test_numpy_scipy_compatible`, `test_numpy_scipy_conflict` | Green CI Docker job on the M12 commit, with the conflict cause text asserted |
| 2 | P2, P3, P10 | `python_input`, `verifier` | `test_resolution_verifier.py` closure: transitive, markers, extras, constraints, `Requires-Python` | — | — | `test_transitive`, `test_markers`, `test_extras`, `test_constraints_file`, `test_requires_python_conflict`, `test_transitive_conflict` | Unit and CI Docker results |
| 3 | P2, P3, P10 | `python_input`, `verifier` | Prerelease and yanked handling on captured reports | — | — | `test_prerelease_policy`, `test_yanked_pin_warning` | Unit and CI Docker results |
| 4 | P2, P3, P5, P10 | `python_input`, `python_resolver`, `resolver_runtime` | `test_resolution_input.py`; `test_pip_args_binary_only` | — | No build activity; input-directory checksum unchanged; no writable mount (`docker inspect`) | `test_source_only_unsupported` (`pycrypto==2.6.1`) | CI Docker result plus security assertions |
| 5 | P3 | `verifier`, `fingerprint` | Forgery matrix (closure, hash, host, extra package, malformed, oversized); fingerprint tests | — | A fake resolver printing a fabricated all-COMPATIBLE report is rejected; only the supervisor inserts `dua_resolutions` | — | Unit results |
| 6 | P2, P7 | `fingerprint`, `approval_service` | One test per fingerprint input | `test_stale_evidence_blocks` | — | — | Unit and API results |
| 7 | P7 | `approval_service`, routes | — | 6 statuses × 4 actions matrix; pre-M12 plans UNKNOWN; npm UNSUPPORTED; M11 suites unchanged | Client-supplied status and fingerprint ignored | — | API results; full M11 regression green |
| 8 | P4, P5, P10 | `egress_proxy`, `resolver_runtime` | `test_egress_proxy.py`: rebinding, allow-list checked before DNS, ports, methods, denial log | — | Proxy unreachable from another network | `test_egress_live.py`: direct TCP, DNS, disallowed host, 169.254.169.254, gateway, host IP, RFC 1918, port 22 and plain HTTP all refused; `pypi.org:443` allowed | CI Docker results; denial log lines captured |
| 9 | P1, P5, P6, P10 | `config`, `resolver_runtime`, `job_service` | `test_resolver_runtime.py` (arguments), `test_resolution_config.py` (ceilings), `test_timeout_hierarchy` (fake clock) | — | No socket, credentials or writable mount in the arguments | `test_timeout_kills_containers` | Unit and CI Docker results |
| 10 | P6, P10 | `job_service`, `run_store`, `resolver_runtime` | `test_job_service.py`: transitions, compare-and-set, one job per plan, pool, cancel, INTERRUPTED, evidence persistence, sweep scope | Retention cascade | — | `test_cancel_cleans_up`, `test_restart_sweep` | Unit and CI Docker results |
| 11 | P8, P9 | Frontend, API | Component tests for every panel state, cancel, disabled approval, npm message | Browser journey (RESOLVER_UNAVAILABLE locally) | — | Browser journey to COMPATIBLE (Docker) | Component results; browser screenshots at 1440 px and 390 px |
| 12 | P10 | CI workflow | — | — | — | All live suites with `DUA_REQUIRE_DOCKER=1` | GitHub Actions run ID on the M12 commit, all jobs green |
| 13 | P1, P11 | `run_store`, docs | `test_resolution_migration.py` (M11-era database) | Pre-M12 plans UNKNOWN and intact | — | — | Unit results; `validate_artifacts.py`; documentation review |
| 14 | P3, P6, P8, P9, P10 | `comparison`, `verifier`, `job_service`, `exporter`, frontend | `test_resolution_comparison.py`, `test_upload_dates.py` | `test_export_resolution.py`; approval unaffected by the comparison | Copy test: no safety claim in the UI or export | `test_original_and_proposed_runs` | Unit, API, component and CI Docker results |

### Construction risks
- **Existing approval-flow tests (M10 and M11):** once P7 lands, approval requires COMPATIBLE evidence. Tests that approve, validate, apply or export will get their evidence from the test fake runner, a setup change only. No assertion is loosened, deleted or skipped, and each such change is listed in the M12 report.
- **No local Docker:** live criteria (1, 2, 3, 4, 8, 9, 10, 11 end-to-end, 12, 14) can be proven only in CI, which needs pushes of work-in-progress commits.
- **Live test data:** live tests depend on PyPI's current data. The pinned package sets were chosen for stable historical versions, and results are recorded with timestamps.

### Security test plan (T-31 and egress)
1. **No third-party code execution:**
   - pip arguments always include `--only-binary=:all:` (unit);
   - a live run with an sdist-only requirement produces no build activity, and the input directory checksum is unchanged afterwards;
   - the container has no writable host mount (`docker inspect` on the live container);
   - the report arrives only on stdout.
2. **Evidence cannot be forged from the resolver side:**
   - The supervisor rejects manipulated reports: closure violation, hash mismatch, foreign host, extra package, schema or size violation.
   - A test double for the resolver that prints a fabricated "all compatible" report is rejected unless it matches PyPI's metadata.
   - The resolver has no access to the SQLite file, the application or the Docker socket (live inspect of mounts and environment).
   - Only the supervisor inserts into `dua_resolutions` (grep test plus repository-layer test).
3. **No host-side state change:** after a live resolution, the database and workspace checksums change only through supervisor writes (an evidence row), and nothing in the repository workspace changes.
4. **Egress:** criterion 8 above, live and unit.

### Test strategy
- **Unit (no network):**
  - supervisor verification on captured real pip reports (recorded once from real pip runs, stored under `tests/fixtures/resolution/` with their source command);
  - conflict extraction from real stderr;
  - input classification;
  - fingerprint;
  - enforcement matrix;
  - job state machine;
  - proxy logic with an injected resolver function and local sockets;
  - Docker argument construction;
  - configuration ceilings;
  - migration.
- **API:** plan → resolve → approve with a faked runner and faked metadata source, for every status.
- **Live Docker (CI, required):** criteria 1–4 and 8–10.
- **Frontend:** component tests, plus a browser journey on 5173/5174.
- **Not mocked to get green:** compatibility claims in CI come only from live resolver runs plus supervisor verification against PyPI.

### Risks
- **Docker required:** no approvals without it (intended, fail closed).
- **npm blocked until M13.**
- **Source-only packages are UNSUPPORTED** (deliberate, ADR-009).
- **Registry drift:** evidence carries a timestamp and expires.
- **PyPI JSON metadata fallback:** for old wheels without PEP 658, verification uses release-level metadata, flagged with a warning.
- **Two resolutions per job:** doubles resolver time and PyPI traffic per plan; the job's wall-clock bound is twice the run timeout.
- **Registry drift between the two runs:** a release published between the original and proposed runs can change the comparison; both evidence records carry timestamps and the UI shows them.
- **COMPATIBLE is not a security verdict:** T-35 accepted by the owner at G2 (event 301): High inherent severity, Medium residual risk, with the FR-32 safeguards. OSV advisories are deferred to M13.
- **Docker Desktop:** `--internal` behaviour on macOS needs checking.
- **Metadata compatibility is not runtime compatibility:** for example, pandas 1.5.3 declares no upper bound on NumPy yet isn't binary-compatible with NumPy 2. Sandbox validation remains the runtime check.

### Migration
Additive: the `dua_jobs` and `dua_resolutions` tables (with a `role` column: `original` or `proposed`) and the `dua_approvals.resolution_fingerprint` column. No rewrites. Pre-M12 records are UNKNOWN.

### Traceability (requirements, architecture, threats, criteria)
| Requirement | Architecture or ADR | Threats | Criteria |
|-------------|---------------------|---------|----------|
| FR-22 | `python_resolver`, ADR-009 | T-31 | 1, 2, 4 |
| FR-23 | `python_input`, `verifier` | T-31 | 2, 4 |
| FR-24 | `resolver_runtime`, ADR-009 | T-30, T-31, T-37 | 4, 9 |
| FR-25, NFR-16 | `egress_proxy`, ADR-010 | T-27, T-28, T-29, T-36, T-37 | 8 |
| FR-26 | `python_input`, ADR-011 | — | 2, 3 |
| FR-27, NFR-14 | `verifier`, `fingerprint`, ADR-011 | T-31, T-32, T-33, T-38 | 5, 6 |
| FR-28, FR-31 | `approval_service` | T-32, T-33 | 7, 13 |
| FR-29, NFR-15 | `job_service`, ADR-012 | T-34 | 10 |
| FR-30 | Frontend panel | — | 11 |
| FR-32 | `comparison` (job runs original then proposed), `verifier` (upload dates), export, frontend panel; ADR-011, ADR-012 | T-35 | 14, 10, 5 |
| NFR-13 | `config`, `resolver_runtime` | T-30 | 9 |
| All | CI workflow | — | 12 |
