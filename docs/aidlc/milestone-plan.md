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
