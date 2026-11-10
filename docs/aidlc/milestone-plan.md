# Milestone plan: Dependency Upgrade Assistant 2.0

<!-- Gate G3. One milestone = one bolt: independently verifiable, ideally hours to a day or two.
     Order by risk. Every milestone must also be registered with `aidlc.py milestone add`. -->

## Scope and Boundary

### Must-Have Functionality (Prototype Delivery)
- Multi-ecosystem manifest upload and parsing supporting `requirements.txt` and `package.json`.
- Deterministic dependency scanning querying PyPI JSON API and npm registry API to identify outdated packages.
- Provider-independent upgrade recommendation architecture supporting local Ollama models with automatic model discovery.
- Deterministic heuristic fallback recommendations activating automatically when Ollama is offline or unconfigured.
- Ground-truth validation guaranteeing that AI recommendations never suggest unverified or hallucinated package versions.
- Surgical manifest modification and standard unified diff (`.diff` / `.patch`) generation.
- Explicit human review and approval gate required prior to workspace modification.
- Sandboxed Docker test validation enforcing security limits (`--network none`, resource caps, read-only mounts) with truthful `PASS`, `FAIL`, and `NOT_RUNNABLE` status reporting.
- Sanitized artifact export (`.patch` file, execution logs, SHA-256 checksums) with automated secret scanning.
- Responsive, high-density React + TypeScript frontend providing the complete end-to-end workflow: Upload -> Scan -> Recommend -> Diff -> Review -> Test -> Export.

### Important but Deferrable Functionality
- Complex lockfile graph resolution (e.g. `poetry.lock`, `pnpm-lock.yaml`, `package-lock.json`).
- Multi-container distributed testing suites (e.g. Docker Compose orchestrations).
- Cloud AI provider integrations (OpenAI, Anthropic, Gemini BYOK).
- In-browser free-form line-level patch editing.

### Explicitly Out-of-Scope Functionality
- Automated or unattended Git branching, committing, or remote GitHub Pull Request opening without human export.
- Auto-merging of dependencies without human review and confirmation.
- Multi-tenant cloud hosting, distributed message brokers (Celery, RabbitMQ, Redis), or Kubernetes deployment.
- Paid commercial APIs or subscription dependencies.
- Host-level execution of untrusted repository code or build scripts.

## Milestones
| ID | Title | Depends on | Acceptance criteria | Test approach | Risk |
|----|-------|------------|---------------------|---------------|------|
| M1 | Application foundation | none | FastAPI backend initialized with configuration, CORS, and health endpoint; React frontend scaffolded with Vite; SQLite persistence foundation initialized; backend test suite passes | Unit and integration tests for FastAPI startup, health checks, config loading, and SQLite connection | Full-stack setup overhead; mitigated by lightweight Vite scaffold and modular FastAPI routers |
| M2 | Manifest ingestion and dependency analysis | M1 | Multipart and raw manifest upload endpoints accept `requirements.txt` and `package.json`; dependencies safely parsed without code execution; PyPI and npm registry clients query available versions with caching; outdated packages classified; malformed manifests fail gracefully | Unit tests on manifest fixtures, mock PyPI/npm API responses, invalid syntax error handling, and registry timeout simulation | Registry rate limits or network latency; mitigated by in-memory caching and mock fallbacks |
| M3 | Upgrade recommendations and AI provider | M2 | Provider-independent AI interface defined; Ollama service implemented with dynamic model discovery; structured recommendation output generated; AI versions validated against registry ground truth; deterministic fallback operates when Ollama is offline | Unit tests for Ollama client mocking, model discovery, prompt construction, structured parsing, registry verification, and fallback activation | Ollama daemon offline; mitigated by deterministic semantic-versioning fallback ensuring continuous operation |
| M4 | Upgrade planning and patch generation | M3 | Upgrade plan endpoint accepts selected candidate packages; manifest modifier applies changes preserving formatting and comments; unified diff generator produces standard diffs; human approval gate enforced prior to workspace mutation | Unit tests for manifest modification accuracy, formatting preservation, unified diff syntax validation, and approval gate enforcement | Formatting corruption; mitigated by AST/regex surgical line replacements preserving whitespace and comments |
| M5 | Sandboxed validation | M4 | Docker runner executes test commands inside container with `--network none` and resource caps; execution logs and exit codes captured; truthful reporting of `PASS`, `FAIL`, and `NOT_RUNNABLE` when Docker is unavailable; fail-closed container cleanup | Integration tests mocking Docker CLI/socket, container execution timeout simulation, exit code parsing, and verification of truthful `NOT_RUNNABLE` reporting | Docker daemon not installed or running; mitigated by environment probe returning diagnostic `NOT_RUNNABLE` status |
| M6 | Artifact export | M5 | Export service bundles changes into downloadable `.patch` files and JSON execution summaries; complete execution logs and SHA-256 checksums generated; secret sanitization scanner verifies zero credential leakage; download endpoints functional | Unit tests for export file integrity, checksum calculation, secret redaction filter verification, and export endpoint testing | Accidental secret leakage in logs; mitigated by pre-export secret scanner and header/token redaction filters |
| M7 | Frontend integration and end-to-end demonstration | M6 | React UI connects to FastAPI across complete workflow (Upload -> Scan -> Recommend -> Diff -> Review -> Test -> Export); displays dependency badges, metadata, and diffs; displays test logs and truthful status badges; implements responsive loading, empty, and error states | Component tests, mock API integration tests, responsive state checks, and accessibility contrast verification | Integration mismatches or CORS errors; mitigated by shared TypeScript interfaces and unified Vite proxy |
| M8 | Final verification | M7 | Full test suite passes 100% green with zero regressions across pre-existing and new tests; end-to-end workflow verified against Python and Node.js fixtures; secret scanning and artifact validation pass with 0 errors; setup and demo guide documented | Full pytest test suite execution, secret scanner audit, end-to-end verification script, and artifact verification | Time pressure before end-of-day deadline; mitigated by tightly scoped vertical slices and continuous verification |

## Test strategy
- Unit tests: Fast, isolated unit tests covering manifest parsing (`requirements.txt`, `package.json`), version comparison logic, registry response caching, prompt formatting, diff synthesis, and checksum calculation.
- Integration tests: Endpoint-level tests for FastAPI routes (upload, analyze, recommend, plan, validate, export) using `httpx.AsyncClient` or Starlette `TestClient` with mocked external services (PyPI, npm, Ollama, Docker).
- Sandboxed execution validation: Dedicated isolation tests verifying Docker execution commands, timeout handling, exit code interpretation, and environmental probing that returns `NOT_RUNNABLE` when Docker is unavailable.
- Characterization and regression safety: Preservation of all 246 existing tests in `tests/`, ensuring zero regression in existing catalog loader, AST parser, diff generator, and recovery reference models.
- Security and NFR verification: Automated execution of `scan_secrets.py` on all exports and workspace files; validation of fail-closed file locks; and verification of `NOT_RUNNABLE` truthful reporting.

## Risks
- Docker daemon unavailable or unprivileged: Target environments (such as developer workstations without Docker or restricted CI environments) cannot run Docker containers. Mitigation: Environmental detection probe checks Docker daemon connectivity and returns an honest, diagnostic `NOT_RUNNABLE` status with actionable explanations, never falsely claiming test success.
- Package registry downtime or rate limiting: External PyPI or npm APIs may be down, throttled, or unreachable. Mitigation: In-memory TTL caching prevents duplicate queries, and resilient timeout handling provides cached or diagnostic error responses.
- Local Ollama instance offline or model missing: Developers may not have Ollama running or the requested model pulled. Mitigation: The AI provider abstraction automatically falls back to deterministic heuristic version recommendations based on semantic versioning rules, ensuring the entire workflow functions without blocking.
- LLM version hallucination: AI models may recommend nonexistent, vulnerable, or malicious package versions. Mitigation: Strict ground-truth validation checks all AI recommendations against verified package versions retrieved from PyPI/npm registries, discarding any unverified version before presenting it to the user.
- Secret or credential leakage: Uploaded manifests, environment variables, or test outputs might contain sensitive tokens or keys. Mitigation: In-memory-only token handling (`GIT_ASKPASS`), automated regex-based secret scanning on all exported files, and strict `.gitignore` enforcement.
- Manifest formatting clobbering: Parsing and serializing manifests might inadvertently strip comments, whitespace, or environment markers. Mitigation: Surgical line-replacement modifiers preserve surrounding lines, indentation, and inline comments in both `requirements.txt` and `package.json`.
