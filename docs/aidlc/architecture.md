# Architecture: Dependency Upgrade Assistant 2.0

## Overview
Dependency Upgrade Assistant 2.0 is architected as a local-first, modular developer platform combining a high-performance Python FastAPI backend and a responsive React Single-Page Application (SPA). The platform orchestrates an 8-stage deterministic analysis and verification pipeline that ingests Python repositories, evaluates dependency manifests against PyPI metadata, detects code-level deprecations via static AST inspection, generates advisory upgrade guidance via local or explicitly authorized cloud LLMs, presents unified diffs for granular human review via Monaco Editor, executes test suites inside an isolated Docker sandbox, and exports cryptographically verified patch deliverables.

The platform strictly enforces a **Zero-Code-Exfiltration Local Privacy Policy**: local Ollama inference is the default, and zero repository source code or dependency data is transmitted to cloud LLM APIs without an explicit, repository-scoped opt-in consent toggle.

The initial release operates as a self-contained local desktop service on `127.0.0.1:8000` (single Uvicorn worker process), storing run history in SQLite, while maintaining strict Protocol-based interface seams for future hosted capabilities (pluggable OAuth, PostgreSQL, remote worker runners) without premature distributed infrastructure.

```
┌────────────────────────────────────────────────────────────────────────┐
│                      PRESENTATION TIER (Browser)                       │
│  React 18 · TypeScript · Vite · Tailwind CSS · shadcn/ui · Zustand     │
│  Monaco Editor (Side-by-Side Diff, Syntax Highlight, Hunk Accept/Reject)│
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTP REST / SSE Stream (127.0.0.1:8000)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        API & ORCHESTRATION TIER                        │
│  FastAPI (Single Uvicorn Worker) · Local Security Middleware           │
│  Pipeline Orchestrator & Event Bus (SSE Replay via Last-Event-ID)       │
└──────────────┬────────────────────┬────────────────────┬───────────────┘
               │                    │                    │
               ▼                    ▼                    ▼
┌────────────────────────┐ ┌──────────────────┐ ┌────────────────────────┐
│     ANALYSIS CORE      │ │  LLM ABSTRACTION │ │   CONTAINER SANDBOX    │
│ ManifestScanner (req/py)│ │ LLMProvider ABC │ │ DockerSandboxManager   │
│ DependencyAnalyzer     │ │ Ollama (Local)   │ │ SandboxedValidation    │
│ CodeInspector (AST)    │ │ Cloud (Opt-In)   │ │ (--cap-drop, --read-only│
│ PatchSynthesizer       │ │ Deterministic Fall│ │ --network none (lo only│
└──────────────┬─────────┘ └──────────────────┘ └────────────────────────┘
               │
               ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      PERSISTENCE & STORAGE TIER                        │
│  SQLite (WAL Mode, Partial Unique Index Concurrency, Isolated Workspaces)│
└────────────────────────────────────────────────────────────────────────┘
```

## Components
| Component | Responsibility | Talks to | Tech |
|-----------|----------------|----------|------|
| **Frontend SPA** | Developer UI (Overview, Repository Workspace, Dependency Explorer, Monaco Diff Reviewer, Validation Center, Artifact Manager). | FastAPI API via JSON REST & SSE streams. | React 18, TypeScript, Vite, Tailwind CSS, shadcn/ui, Monaco Editor, TanStack Query, Zustand. |
| **Local Security Middleware** | Validates `Host` header (DNS rebinding defense), enforces strict `Origin` whitelist (rejecting absent/null origins on mutating routes), requires `application/json` Content-Type, and validates anti-CSRF custom headers (`X-DUA-Client: web`). | Incoming HTTP requests before routing. | FastAPI / Starlette ASGI Middleware. |
| **API Router & Pipeline Controller** | Exposes REST endpoints for repository management, analysis triggers, approval submissions, validation, and exports; manages asynchronous background pipeline execution and startup state recovery. | Pipeline Orchestrator, Storage Service. | FastAPI, Pydantic v2. |
| **Pipeline Orchestrator & Event Bus** | Coordinates the 8-stage analysis, improvement, validation, and export workflow; persists progress events to SQLite; streams real-time stage progress over SSE with `Last-Event-ID` reconnection. | Analysis Core, LLM Provider, Sandbox Manager, Storage Service, Git Service. | Python 3.10–3.14 (`src/pipeline/orchestrator.py`), `asyncio`. |
| **Git Service & Workspace Manager** | Clones repositories into isolated read-only reference and mutable working copies; delivers PATs via authenticated single-use local UNIX socket IPC with kernel peer credentials and nonce verification, avoiding environment leaks or disk writes. | Git CLI, Workspace Filesystem. | Python `subprocess`, static `askpass_helper.py`, `socket`, `shutil`. |
| **Manifest Scanner & PyProject Parser** | Recursively scans for `requirements.txt` and PEP 621/735 / Poetry `pyproject.toml` files, parsing constraints while preserving comments and formatting. | Workspace Filesystem. | `packaging.requirements`, `packaging.specifiers`, `tomllib`/`tomli`. |
| **Dependency Analyzer** | Deterministic evaluation of package candidates against PyPI JSON API with in-memory caching and offline local catalog fallback (`package_catalog.json`). | PyPI JSON API, Local Catalog. | Python `requests`, `packaging.version`, `packaging.specifiers`. |
| **Code Inspector & AST Engine** | Static AST analysis of Python source files to detect usages of deprecated APIs, insecure constructs, and syntax incompatibilities without executing code. | Workspace Filesystem. | Python `ast`, `tokenize`. |
| **Patch Synthesizer & Validator** | Generates unified diffs; applies approved upgrade candidates and hunk selections atomically; verifies hunk independence; detects stale patches; runs post-synthesis AST syntax checks. | Working Workspace Filesystem. | Python `difflib`, `ast`. |
| **LLM Provider Abstraction** | Generates qualitative advisory explanations, breaking change summaries, and migration advice; enforces strict repository-scoped opt-in for cloud models under the Zero-Code-Exfiltration Local Privacy Policy. | Local Ollama Daemon, Cloud LLM APIs (OpenAI/Anthropic/Gemini). | Python Protocol (`LLMProvider`), `httpx`. |
| **Docker Sandbox Manager** | Executes test suites in ephemeral containers with `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only`, `--network none` (loopback only), `--tmpfs /tmp:rw,noexec,nosuid,nodev`, non-root user, and resource limits; fail-closed. | Local Docker Daemon CLI. | Docker CLI, `subprocess`. |
| **Storage Service** | Persists connected repositories, analysis runs, pipeline event logs (for SSE replay), detected dependencies, proposed changes, validation logs, and artifact hashes; enforces database-level partial unique index concurrency. | Local SQLite database. | Python `sqlite3` (WAL mode, foreign keys enabled, `BEGIN IMMEDIATE` coordination). |
| **Artifact Exporter** | Synthesizes validated `.patch` files, generates verified `.zip` archives of modified working copies, creates Markdown reports, and computes SHA-256 integrity checksums. | Working Workspace Filesystem, Export Storage. | Python `zipfile`, `hashlib`. |

## Data
The local-first platform uses an embedded SQLite database (`dua_local.db`) with Write-Ahead Logging (`PRAGMA journal_mode=WAL`) and foreign key enforcement (`PRAGMA foreign_keys=ON`).

### Entity Schema Summary
```sql
-- Connected Repositories
CREATE TABLE repositories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('github', 'local')),
    url_or_path TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    cloud_llm_consent INTEGER NOT NULL DEFAULT 0,  -- 0=denied, 1=granted
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Analysis Runs
CREATE TABLE analysis_runs (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('created', 'queued', 'running', 'awaiting_approval', 'completed', 'failed', 'cancelled')),
    current_stage TEXT NOT NULL,
    base_commit_hash TEXT,
    head_commit_sha TEXT,
    workspace_dir TEXT,
    proposal_file TEXT,
    proposal_sha256 TEXT,
    patch_file TEXT,
    patch_sha256 TEXT,
    notes TEXT,
    idempotency_key TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error_message TEXT
);
CREATE UNIQUE INDEX idx_analysis_runs_idempotency ON analysis_runs(idempotency_key) WHERE idempotency_key IS NOT NULL;

-- Database-Level Concurrency Safeguard: Exactly one active run per repository
CREATE UNIQUE INDEX idx_one_active_run_per_repo ON analysis_runs(repository_id) 
WHERE status IN ('queued', 'running', 'awaiting_approval');

-- Persistent Docker and Workspace Cleanup Queue (Durable Across Restarts & Run Pruning)
CREATE TABLE pending_cleanups (
    id TEXT PRIMARY KEY,
    originating_run_id TEXT NOT NULL,
    run_id TEXT REFERENCES analysis_runs(id) ON DELETE SET NULL,
    container_id TEXT,
    workspace_dir TEXT NOT NULL,
    quarantine_dir TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'in_progress', 'quarantine_intent', 'quarantined', 'completed', 'failed')),
    worker_id TEXT,
    lease_token TEXT,
    claimed_at TIMESTAMP,
    lease_expires_at TIMESTAMP,
    next_attempt_at TIMESTAMP,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_type TEXT CHECK(error_type IN ('TRANSIENT_DOCKER_UNAVAILABLE', 'PERMANENT_ERROR', NULL)),
    last_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_pending_cleanups_status ON pending_cleanups(status);
CREATE INDEX idx_pending_cleanups_lease ON pending_cleanups(status, lease_expires_at);
CREATE INDEX idx_pending_cleanups_schedule ON pending_cleanups(status, next_attempt_at);

-- Monotonic Event Log for SSE Reconnection
CREATE TABLE pipeline_events (
    seq_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    event_type TEXT NOT NULL,  -- 'stage_start', 'stage_progress', 'log', 'stage_complete', 'error'
    payload_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_pipeline_events_run_seq ON pipeline_events(run_id, seq_id);

-- Discovered Dependencies & Candidates
CREATE TABLE analyzed_dependencies (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    manifest_path TEXT NOT NULL,
    package_name TEXT NOT NULL,
    current_specifier TEXT NOT NULL,
    current_version TEXT,
    candidate_version TEXT,
    has_upgrade INTEGER NOT NULL DEFAULT 0,
    upgrade_status TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance IN ('PyPI JSON API', 'Local Catalog')),
    ai_risk_level TEXT CHECK(ai_risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'UNKNOWN')),
    ai_summary TEXT
);

-- Proposed Code & Manifest Modifications (Hunks)
CREATE TABLE proposed_modifications (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    hunk_index INTEGER NOT NULL,
    before_content TEXT NOT NULL,
    after_content TEXT NOT NULL,
    diff_text TEXT NOT NULL,
    ai_rationale TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
    applied_at TIMESTAMP
);

-- Sandbox Validation Results
CREATE TABLE validation_results (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    command_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PASSED', 'FAILED', 'NOT_RUNNABLE', 'ERROR', 'SKIPPED')),
    exit_code INTEGER NOT NULL,
    stdout TEXT NOT NULL,
    stderr TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    error_message TEXT
);

-- Exported Deliverables
CREATE TABLE exported_artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL CHECK(artifact_type IN ('patch', 'zip', 'markdown')),
    file_path TEXT NOT NULL,
    sha256_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Data Retention & Concurrency Policy
- **Concurrency & WAL Mode:** SQLite is configured with `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`. WAL mode permits multiple concurrent reader connections while a background worker executes write transactions. State updates and event insertions use explicit `BEGIN IMMEDIATE` transactions to prevent database deadlocks.
- **Database Partial Unique Index Safeguard:** To prevent race conditions across processes, threads, or accidental duplicate launches, `CREATE UNIQUE INDEX idx_one_active_run_per_repo ON analysis_runs(repository_id) WHERE status IN ('queued', 'running', 'awaiting_approval')` guarantees that the database engine itself rejects concurrent active runs for the same repository.
- **Workspaces:** Isolated reference and mutable working directories reside in `~/.dua/workspaces/<run_id>/`. Ephemeral workspace directories are cleaned up upon successful run completion, failure, or explicit user deletion.

## Interfaces & API Contracts

### REST Endpoints
All endpoints are prefixed with `/api/v1` and strictly enforce local security middleware.

| Method | Endpoint | Description | Request Payload | Response Shape |
|--------|----------|-------------|-----------------|----------------|
| `GET` | `/repositories` | List connected repositories. | None | `{"items": [RepositorySummary]}` |
| `POST` | `/repositories` | Connect a GitHub or local repository. | `{"name": str, "source_type": "github"|"local", "url_or_path": str, "cloud_llm_consent": bool, "pat": Optional[str]}` | `{"id": str, "name": str, ...}` |
| `POST` | `/repositories/{id}/runs` | Trigger an analysis run (supports superseding prior pending approval). | `{"skip_ai": bool, "force_supersede": bool}` (optional header: `Idempotency-Key`) | `{"run_id": str, "status": "queued"}` |
| `GET` | `/runs/{id}` | Get run details and status. | None | `{"id": str, "status": str, "stage": str, ...}` |
| `GET` | `/runs/{id}/events` | SSE event stream supporting reconnection via `Last-Event-ID`. | None (standard SSE request) | `id: <seq_id>\ndata: {"stage": str, "event_type": str, "payload": {...}}\n\n` |
| `POST` | `/runs/{id}/cancel` | Cancel an active pipeline run (releases repository lock). | None | `{"run_id": str, "status": "cancelled"}` |
| `GET` | `/runs/{id}/dependencies` | Fetch analyzed dependencies and upgrade paths. | None | `{"items": [AnalyzedDependency]}` |
| `GET` | `/runs/{id}/diffs` | Fetch proposed file modifications and hunks. | None | `{"files": [{"file_path": str, "hunks": [DiffHunk]}]}` |
| `POST` | `/runs/{id}/approvals` | Submit human approval/rejection of hunks. | `{"decisions": [{"modification_id": str, "action": "approve"|"reject"}]}` | `{"applied_count": int, "rejected_count": int, "stale": bool}` |
| `POST` | `/runs/{id}/validate` | Trigger isolated Docker validation. | `{"timeout_seconds": int}` | `{"status": "running"}` |
| `GET` | `/runs/{id}/validation-results` | Fetch validation test output and logs. | None | `{"results": [ValidationResultSummary]}` |
| `POST` | `/runs/{id}/export` | Generate verified deliverables. | None | `{"artifacts": [ArtifactMetadata]}` |
| `GET` | `/runs/{id}/export/{artifact_type}/download` | Download `.patch`, `.zip`, or report. | None | File binary stream with `Content-Disposition`. |

### Standard Error Response Shape
```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "Selected hunks conflict with unselected modifications in requirements.txt",
    "details": {
      "file_path": "requirements.txt",
      "conflicting_hunk_indices": [1, 3]
    },
    "timestamp": "2026-11-09T18:45:00Z"
  }
}
```

## Deep-Dive Architecture & Security Specifications

### 1. Git Credential Isolation: Process Tree, Same-UID Race Defense & Scope Realities
Standard POSIX process inheritance causes child processes to inherit their parent's environment variables. Simply passing a token in an environment dictionary to `git clone` exposes the token to the entire Git process tree:
```
FastAPI Parent Process (PID P1)
  └─ git clone / git fetch (Child 1: PID P2, spawned by Python via subprocess.Popen)
       ├─ git-remote-https (Child 2: PID P3, spawned by git clone for HTTPS transport)
       │    └─ $GIT_ASKPASS helper (Child 3: PID P4, spawned by git-remote-https upon HTTP 401)
       ├─ git-credential-* (Global helpers from ~/.gitconfig, e.g. osxkeychain, manager)
       └─ git hooks (e.g. reference-transaction, post-checkout from core.hooksPath)
```
**Process Hierarchy & Askpass Execution Path:**
1. Python executes `git clone <repo_url>` as child process $P_2$.
2. When accessing an HTTPS remote, `git clone` delegates the HTTP transport to `git-remote-https` ($P_3$).
3. `git-remote-https` uses `libcurl` to execute the TLS handshake and HTTP request.
4. When GitHub responds with an HTTP 401 Unauthorized status requiring authentication, `git-remote-https` invokes Git's credential subsystem, which directly executes `$GIT_ASKPASS <prompt>` as child process $P_4$.
5. The askpass helper ($P_4$) queries the credential, prints it to `stdout`, and exits. `git-remote-https` consumes the token from the pipe and issues the authenticated request with `Authorization: Basic ...`.

If `DUA_GIT_TOKEN` is present in Git's environment, `git-remote-https`, global credential helpers, and hooks inherit the secret. A malicious hook or third-party credential manager could capture the token or write it into the user's OS keychain.

Furthermore, passing an authorization nonce (`DUA_AUTH_NONCE`) via environment variables does not protect against same-UID inspection on Linux, where `/proc/<pid>/environ` is readable by other processes of the same user. Therefore, kernel peer credential verification and complete process-ancestry validation are mandatory.

> [!NOTE]
> **Brownfield Baseline vs. Target Architecture Note:**
> The existing codebase contains a functional prototype in `src/services/git_service.py` (which writes temporary askpass scripts). The hardened UNIX domain socket IPC architecture with peer credentials, PID ancestry walks, and nonce validation specified in [ADR-003](file:///Users/soham/Github/Dependency Upgrade Assistant/docs/aidlc/adr/ADR-003-in-memory-credential-delivery-via-ephemeral-git-as.md) is the **Target Architecture Specification** for Dependency Upgrade Assistant 2.0 to be constructed in Phase 2 Construction. Architectural contracts are verified via `tests/test_git_peer_auth.py`.

**Hardened Ephemeral Socket Delivery with Peer and Nonce Verification ([ADR-003](file:///Users/soham/Github/Dependency Upgrade Assistant/docs/aidlc/adr/ADR-003-in-memory-credential-delivery-via-ephemeral-git-as.md)):**
To eliminate environment-variable credential exposure and defend against same-UID socket race conditions:
1. **No Token in Process Environment:** The token string is **never** placed into the environment dictionary of `git` or any child process.
2. **Ephemeral UNIX Domain Socket:** `GitService` binds an ephemeral UNIX domain socket in a private directory with mode `0700` (`~/.dua/ipc/ask_<uuid>.sock`).
3. **Cryptographic Nonce Handshake:** `GitService` generates a 256-bit single-use random nonce (`secrets.token_hex(32)`). When the helper connects to the socket, it must send this nonce before any data is exchanged; connections with invalid nonces are terminated immediately.
4. **Kernel Peer Credential & Complete Process-Ancestry Verification:**
   - **Linux Peer Verification & `/proc` Ancestry Walk:**
     * The listener calls `getsockopt(SOL_SOCKET, SO_PEERCRED, ...)` to obtain kernel-verified `struct ucred` (`pid`, `uid`, `gid`).
     * The listener verifies `ucred.uid == os.getuid()`.
     * To defend against same-UID attackers who discovered the socket and nonce via `/proc/<pid>/environ`, the listener performs complete process-ancestry validation by walking `/proc/<pid>/stat`:
       - Starting PID: `curr_pid = ucred.pid` (the askpass helper $P_4$).
       - For up to 5 parent levels:
         1. Read `/proc/<curr_pid>/stat`.
         2. Parse `comm` robustly by finding the last closing parenthesis `)` to handle spaces or parentheses in executable names, then extract subsequent tokens.
         3. Extract `ppid` (field 4) and `starttime` (field 22).
         4. Guard against PID reuse: verify that each process was created after its parent and after the Python clone invocation began by checking `starttime` monotonic ordering.
         5. If `curr_pid == git_proc.pid` ($P_2$): verify that its `ppid` is `os.getpid()` ($P_1$). Verification succeeds; break loop.
         6. Advance: `curr_pid = ppid`.
       - If traversal terminates without verifying ancestry through `git_proc.pid`, fail closed immediately.
     * **Robust Failure Handling:** If any `/proc` entry is missing, process terminates during walk, `/proc` is inaccessible, `/proc/<pid>/stat` is malformed, or an unexpected ancestry is encountered: the connection is aborted immediately and fails closed.
   - **macOS Peer Verification & `libproc` Ancestry Walk:**
     * `libc.getpeereid(fd, &euid, &egid)` is called to verify kernel-enforced `euid == os.getuid()`. Note that `getpeereid()` verifies UID/GID, not PID.
     * To verify the peer PID on macOS, the listener calls `getsockopt(0, 0x002, 4)` (`SOL_LOCAL=0`, `LOCAL_PEERPID=2`, returning a 4-byte signed integer `pid_t`). Empirically verified in Python runtime via `conn.getsockopt(0, 2, 4)` and `struct.unpack('i', raw_pid)[0]`.
     * Because `/proc` does not exist on macOS, process ancestry is verified using `libproc` (`/usr/lib/libproc.dylib`) via `ctypes`: `libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buf, 1024)`. Struct `proc_bsdinfo` provides `pbi_ppid` at byte offset 16. The listener walks parent PIDs up the tree to assert that the connecting process is an ancestor of `git_proc.pid`.
     * If `LOCAL_PEERPID` or `libproc` is unavailable or returns an error, the system fails closed rather than silently downgrading to UID-only authentication.
     * On macOS, Mach task security restricts same-UID environment inspection (`task_for_pid` requires elevated entitlements), providing additional defense against same-UID environment snooping.
5. **Static Helper Invocation & Hardened Git Flags:**
   Git is spawned with `GIT_ASKPASS=/path/to/static_askpass_helper.py`, `DUA_AUTH_SOCK=/path/to/ask_<uuid>.sock`, and `DUA_AUTH_NONCE=<nonce>`:
   ```bash
   git -c credential.helper= -c core.hooksPath=/dev/null clone <url>
   ```
   - `-c credential.helper=`: Explicitly overrides and disables all workstation-configured credential helpers (preventing tokens from being stored in macOS Keychain or Windows Credential Manager).
   - `-c core.hooksPath=/dev/null`: Suppresses all global and template hooks during acquisition.
   - Diagnostic variables (`GIT_TRACE`, `GIT_TRANSFER_TRACE`, `GIT_CURL_VERBOSE`) are stripped from the child environment.
6. **Single-Use Socket Streaming & Teardown:** The listener accepts strictly ONE connection, enforces a 5.0-second connection timeout and a 2.0-second handshake timeout, serves the token for milliseconds, immediately unlinks the socket file, removes the directory, and clears all in-memory references (`del pat`).
7. **Accurate Exposure Scope:** The `askpass_helper` writes the token to `stdout`, which Git consumes via its internal pipe to construct the HTTP `Authorization: Basic ...` header. Therefore, Git's HTTP transport (`git-remote-https` via `libcurl`) **necessarily receives the credential** to authenticate to GitHub over TLS. The architecture guarantees zero token persistence to disk, zero presence in process command-line arguments or URLs, zero presence in process environments of `git` or third-party helpers/hooks, and zero logging.

### 2. Docker Sandbox Accuracy, Network Namespace & Filesystem Hardening
The container sandbox provides isolated execution for repository verification commands ([ADR-002](file:///Users/soham/Github/Dependency Upgrade Assistant/docs/aidlc/adr/ADR-002-docker-sandbox-containment-and-filesystem-security.md)):

```
Docker Command Invocation:
docker run --rm \
  --label dev.dua.managed=true \
  --label dev.dua.run_id=run_123 \
  --label dev.dua.created_at=2026-11-09T12:00:00Z \
  --user $(id -u):$(id -g) \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --network=none \
  --read-only \
  --memory=512m --cpus=1.0 --pids-limit=64 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v /home/user/.dua/workspaces/run_123/reference:/workspace/reference:ro \
  -v /home/user/.dua/workspaces/run_123/working:/workspace/working:rw \
  -w /workspace/working \
  dua-sandbox-python:3.11 \
  pytest -v -o cache_dir=/tmp/.pytest_cache
```

- **Workspace Directory Permissions & Non-Root Execution:**
  - **No World-Writable Permissions:** World-writable mode (`0777`) is strictly forbidden. Any write or read permission for `others` (`mode & 0o007 != 0`) violates host isolation and is rejected.
  - **Host UID/GID Passthrough (Default):** In desktop/CLI developer environments, the container executes with `--user $(id -u):$(id -g)` matching the host user's non-root UID/GID (e.g. `501:20` on macOS or `1000:1000` on Linux). Host workspace directories are created with mode `0700` (`rwx------`), owned exclusively by the host developer. The unprivileged container process has direct read/write access to `/workspace/working` without expanding host filesystem permissions.
  - **Dedicated Group / POSIX ACLs (Fixed UID Environments):** In server or multi-tenant deployments requiring a fixed container UID (e.g. `appuser:1000:1000`), permissions are narrowly scoped using POSIX ACLs (`setfacl -R -m u:1000:rwx <workspace>`) or dedicated group ownership (`chgrp 1000 <workspace>` with mode `0770`), ensuring zero access for other unprivileged host accounts (`others: ---`).

- **Network Namespace Reality (`--network none`):**
  `--network none` assigns the container a private network namespace possessing **strictly the loopback interface (`lo` / `127.0.0.1`)** with no external network interfaces (`eth0`), default gateways, or DNS resolvers. Test suites can bind local mock servers to `127.0.0.1`, but cannot route traffic outside the container or reach host network services.
- **The `noexec` on `/tmp` Architecture:**
  - Standard pure-Python unit tests (`pytest`, `unittest`) execute by reading `.py` files and interpreting them in userland memory. Pytest cache files (`cache_dir=/tmp/.pytest_cache`) are read and written as structured data (JSON/text), which functions completely under `noexec`.
  - Mounting `/tmp` with `noexec,nosuid,nodev` blocks untrusted code from dropping and executing arbitrary ELF binaries or compiled scripts in temp storage.
  - *Dynamic Compilation Compatibility:* If a test suite requires compiling and executing dynamic C/Rust extensions on the fly (e.g. `cffi`, `ctypes.CDLL`), the user must enable an explicit configuration flag (`allow_dynamic_compilation: true`), which provisions a dedicated, segregated build mount: `--tmpfs /workspace/build_bin:rw,exec,nosuid,nodev,size=128m`.
- **Pre-Baked Toolchain & Fail-Closed Guard:** The container image (`dua-sandbox-python:3.11`) comes pre-installed with `pytest`, `ruff`, and `py_compile`. If a tool is missing, execution terminates with status `NOT_RUNNABLE` (Reason: `TOOL_MISSING`), **never falling back to host execution**.
- **Deterministic Container Labeling & Orphan Recovery:** All validation containers are spawned with labels `--label dev.dua.managed=true`, `--label dev.dua.run_id=<run_id>`, and `--label dev.dua.created_at=<timestamp>`. On application startup, the recovery engine inspects Docker containers by label, verifies ownership, and stops/removes orphaned containers before deleting workspace directories on the host. If Docker daemon is unavailable, workspace deletion is deferred (fails closed).
- **Residual Risk:** Docker isolates userland processes but shares the host operating system kernel. Unpatched zero-day vulnerabilities in the host Linux kernel reachable via permitted syscalls represent a shared-kernel residual risk.

> [!NOTE]
> **Brownfield Baseline vs. Target Architecture Note:**
> The existing codebase contains a prototype runner in `src/validation/sandbox_manager.py`. The complete Docker sandbox containment, tmpfs `noexec` isolation, OS-level filesystem locks (`WorkspaceFileLock`), and durable cleanup recovery engine specified in [ADR-002](file:///Users/soham/Github/Dependency Upgrade Assistant/docs/aidlc/adr/ADR-002-docker-sandbox-containment-and-filesystem-security.md) constitute the **Target Architecture Specification** for Dependency Upgrade Assistant 2.0 to be constructed in Phase 2 Construction. Architectural contracts are verified via `tests/test_docker_orphan_recovery.py`.

### 3. Background Pipeline Lifecycle, Concurrency & Startup Recovery
Long-running analysis and validation tasks must execute reliably regardless of frontend connection status:
1. **Single-Worker Local Deployment Baseline:** The local backend is deployed as a single Uvicorn process (`workers=1`).
2. **Database-Level Partial Unique Index Concurrency:**
   - Enforced by `CREATE UNIQUE INDEX idx_one_active_run_per_repo ON analysis_runs(repository_id) WHERE status IN ('queued', 'running', 'awaiting_approval');`.
   - Run acquisition executes an atomic SQLite transaction (`BEGIN IMMEDIATE`). If an active run exists, the transaction fails with `409 Conflict`. Multi-process race conditions are mechanically impossible without Redis.
3. **Persistent Docker Cleanup Recovery Engine (`pending_cleanups`):**
   To resolve the risk that interrupted runs transitioned to `failed` are lost to subsequent active-run recovery sweeps, or that run deletion during retention maintenance drops uncompleted cleanup obligations:
   - **Independent Cleanup State & Non-Cascading Provenance:**
     Cleanup obligations are decoupled from `analysis_runs` and persisted in `pending_cleanups`:
     ```sql
     CREATE TABLE pending_cleanups (
         id TEXT PRIMARY KEY,
         originating_run_id TEXT NOT NULL,
         run_id TEXT REFERENCES analysis_runs(id) ON DELETE SET NULL,
         container_id TEXT,
         workspace_dir TEXT NOT NULL,
         quarantine_dir TEXT,
         status TEXT NOT NULL CHECK(status IN ('pending', 'in_progress', 'quarantine_intent', 'quarantined', 'completed', 'failed')),
         worker_id TEXT,
         lease_token TEXT,
         claimed_at TIMESTAMP,
         lease_expires_at TIMESTAMP,
         next_attempt_at TIMESTAMP,
         retry_count INTEGER NOT NULL DEFAULT 0,
         error_type TEXT CHECK(error_type IN ('TRANSIENT_DOCKER_UNAVAILABLE', 'PERMANENT_ERROR', NULL)),
         last_error TEXT,
         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
     );
     ```
     * **Foreign Key & Retention Strategy:** `originating_run_id TEXT NOT NULL` permanently captures the run identifier for container label matching (`dev.dua.run_id=<originating_run_id>`) even if the parent `analysis_runs` record is deleted or archived. `run_id` uses `ON DELETE SET NULL` (never `CASCADE`), guaranteeing that cleanup obligations survive run pruning. Completed cleanups (`status = 'completed'`) may be safely archived after 30 days; pending or failed obligations are never purged automatically.
   - **Cleanup Lifecycle & Lease-Fenced State Transitions:**
     ```
     [Interrupted Run / Crash] 
            │
            ▼
     [pending] ──(Atomic Claim with Lease Token)──> [in_progress] (lease: 120s)
                                                        │
                         ┌──────────────────────────────┼──────────────────────────────┐
                         ▼                              ▼                              ▼
                 (Confirmed Stop/RM)          (Transient Docker Down)          (Permanent Error)
                         │                              │                              │
                         ▼                              ▼                              ▼
               [quarantine_intent]            [pending] (backoff)            [pending] (retry++)
             (Intent Persisted to DB)          (next_attempt_at)              (next_attempt_at)
                         │                                                             │
                         ▼                                                 (Exceeded 5 Retries)
               (os.replace Rename)                                                     │
                         │                                                             ▼
                         ▼                                                          [failed]
                   [quarantined]                                                (Terminal State)
             (Rename Confirmed in DB)                                                  │
                         │                                            (Operator Explicit Retry)
                         ▼                                                             │
                  (shutil.rmtree)                                                      ▼
                         │                                                         [pending]
                         ▼
                    [completed]
     ```
   - **Atomic Worker Claims, Bounded Timeouts & Lease Renewal:**
     * **Bounded Docker Timeouts:** Docker commands are strictly bounded: `docker stop -t 5` enforces a 5-second graceful SIGTERM before SIGKILL; `docker rm -f` enforces a 10-second timeout. Total Docker interactions are bounded to $\le 30$ seconds, well within the 120-second lease window (leaving a 90-second safety margin).
     * **Atomic Claim Acquisition with Fencing Token:** Workers query records where `status = 'pending'` AND `(next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP)`. The claim generates a unique cryptographic fencing token with 256 bits of entropy (`lease_token = secrets.token_hex(32)`, represented as 64 hexadecimal characters) and executes an atomic conditional update:
       ```sql
       UPDATE pending_cleanups
       SET status = 'in_progress',
           worker_id = :worker_id,
           lease_token = :lease_token,
           claimed_at = CURRENT_TIMESTAMP,
           lease_expires_at = datetime('now', '+120 seconds'),
           updated_at = CURRENT_TIMESTAMP
       WHERE id = :id AND (
           status = 'pending'
           OR (status = 'in_progress' AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP))
       );
       ```
       If `rowcount == 1`, the worker acquired the lease and token. If `rowcount == 0`, another worker claimed it.
     * **Heartbeat Lease Renewal with Fencing & Expiration Verification:** For multi-step cleanups, the worker issues an atomic heartbeat renewal:
       ```sql
       UPDATE pending_cleanups
       SET lease_expires_at = datetime('now', '+120 seconds'),
           updated_at = CURRENT_TIMESTAMP
       WHERE id = :id 
         AND worker_id = :worker_id 
         AND lease_token = :lease_token 
         AND status = 'in_progress'
         AND lease_expires_at >= CURRENT_TIMESTAMP;
       ```
       If `rowcount == 0`, renewal failed (the lease expired or another worker reclaimed the record). The worker detects this immediately, aborts, and executes no further destructive operations.
     * **Worker Crash Handling During Renewal:** If a worker crashes or encounters a database error during lease renewal, the lease expires naturally after 120 seconds, allowing safe reclamation by another worker without deadlocking.
   - **Durable Quarantine-Intent Protocol (SQLite/Filesystem Crash Consistency):**
     Performing filesystem renames inside an uncommitted database transaction risks leaving orphaned directories on disk if the process crashes before `COMMIT`. Furthermore, database transactions alone cannot revoke in-flight filesystem operations of a stalled process. The architecture combines database intent logging, POSIX kernel file locking, canonical path containment validation, and directory metadata fsync into a durable 5-step quarantine protocol:
     1. **Step 1 (Path Validation, POSIX Kernel Lock & Persist Intent to SQLite):**
        * **Canonical Path Containment:** Before touching the filesystem, the worker resolves `realpath` on both the workspace directory and base directory, asserting `os.path.commonpath([real_root, real_path]) == real_root` and `real_path != real_root`. It verifies `os.path.islink(path)` is false to prevent directory symlink escapes or sibling directory prefix attacks (e.g., `/tmp/ws_evil` matching prefix `/tmp/ws`).
        * **OS Advisory Kernel File Lock (`WorkspaceFileLock`) & Persistent Lock-File Lifecycle:** The worker acquires a non-blocking POSIX advisory kernel lock `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` on `.cleanup_<id>.lock` in the parent directory.
          - *Advisory Scope:* `flock` is advisory and protects cooperating processes that adhere to the locking protocol; it does not protect against external processes that bypass the lock or across network mounts lacking POSIX lock daemons.
          - *Persistent Lifecycle (No Unlinking):* Lock files are persistent (opened with mode `0o600` and `O_CLOEXEC`) and **must not be unlinked** upon normal lock release. Unlinking a lock file introduces an inode-splitting race where a waiting or acquiring process holds an unlinked inode while a newly arriving worker creates and locks a different inode at the same path, allowing concurrent lock acquisition on the same logical cleanup ID.
          - *Descriptor Lifetime:* The open file descriptor is held continuously throughout the entire protected filesystem critical section and closed only after all filesystem steps finish. If a worker terminates, crashes, or receives `SIGKILL`, the kernel synchronously closes file descriptors and releases the lock automatically.
        * **Persist Intent to SQLite:** The worker persists quarantine intent and the unique target path (`quarantine_dir = f"{workspace_dir}.quarantine_{lease_token}"`):
        ```sql
        UPDATE pending_cleanups
        SET status = 'quarantine_intent',
            quarantine_dir = :quarantine_path,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :id
          AND worker_id = :worker_id
          AND lease_token = :lease_token
          AND status = 'in_progress'
          AND lease_expires_at >= CURRENT_TIMESTAMP;
        ```
        The transaction is immediately committed. If `rowcount == 0`, lease ownership was lost; abort immediately.
     2. **Step 2 (Pre-Rename Fencing Check, Atomic Rename & Parent Directory Fsync):**
        * **Pre-Rename Fencing Check:** Immediately before touching the filesystem, the worker queries SQLite asserting that it still holds active lease ownership with status `quarantine_intent`. If the lease expired or was superseded while waiting, the rename is aborted.
        * **Atomic Rename:** The worker performs `os.replace(workspace_dir, quarantine_path)`. In POSIX kernels, `rename(2)` atomically unlinks the source path and links the destination in a single operation.
        * **Parent Directory Fsync & Durability Failures:** While a standard process crash (`SIGKILL`) preserves directory entries in the kernel's virtual filesystem (VFS) page cache, sudden hardware power loss can revert unflushed directory metadata blocks. To achieve true crash-consistency across power loss, the worker executes `os.fsync(dir_fd)` on an `os.O_RDONLY` descriptor to the parent directory. If fsync fails (`FsyncException`), the failure is observable: the worker aborts without confirming quarantine in SQLite, leaving the status as `quarantine_intent` so recovery can safely reconcile actual on-disk reality.
     3. **Step 3 (Confirm Quarantine in SQLite):**
        Once the rename and directory fsync succeed, the worker confirms the quarantine state in the database:
        ```sql
        UPDATE pending_cleanups
        SET status = 'quarantined',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :id
          AND worker_id = :worker_id
          AND lease_token = :lease_token
          AND status = 'quarantine_intent'
          AND lease_expires_at >= CURRENT_TIMESTAMP;
        ```
        The transaction is immediately committed. If `rowcount == 0` (e.g. lease expired during rename), the worker aborts deletion.
     4. **Step 4 (Pre-Deletion Fencing Check & Fail-Closed Recursive Deletion):**
        * **Pre-Deletion Fencing Check:** Immediately before executing recursive directory deletion, the worker verifies that its lease remains valid and unexpired with status `quarantined`.
        * **Fail-Closed Recursive Deletion:** The worker executes `secure_rmtree(quarantine_path)`.
          - *Symlink Traversal Prevention:* `secure_rmtree` traverses with `followlinks=False` to unlink symlinks directly rather than descending into external targets. (Note: `followlinks=False` prevents traversal into symlinks, but does not provide atomic descriptor-relative `openat(..., O_NOFOLLOW)` protection against concurrent TOCTOU symlink replacement races during directory traversal).
          - *Fail Closed:* All unlink and rmdir operations are checked. If any file cannot be unlinked or any directory cannot be removed, `secure_rmtree` raises `DeletionException`. The cleanup worker does not report completion, leaving the directory for retry or recovery.
     5. **Step 5 (Commit Completion):**
        ```sql
        UPDATE pending_cleanups
        SET status = 'completed',
            quarantine_dir = NULL,
            worker_id = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :id
          AND worker_id = :worker_id
          AND lease_token = :lease_token
          AND status = 'quarantined'
          AND lease_expires_at >= CURRENT_TIMESTAMP;
        ```
   - **Crash Consistency Matrix & Recovery Invariants:**
     When startup recovery or periodic sweeps inspect records with expired leases or interrupted transitions:
     * **`quarantine_intent` Recovery:**
       - *Case 1 (Original exists, quarantine absent):* The process crashed before `os.replace`. The rename never occurred. Recovery resets `status = 'pending'`, clears `quarantine_dir = NULL`, preserving the workspace for clean retry.
       - *Case 2 (Original absent, quarantine exists):* The process crashed after `os.replace`. The rename succeeded. Recovery verifies pre-deletion ownership, safely deletes `quarantine_dir` via `secure_rmtree`, and marks `status = 'completed'`.
       - *Case 3 (Both exist):* Incomplete quarantine anomaly. Recovery verifies ownership, removes the orphaned quarantine artifact `quarantine_dir`, preserves the intact `workspace_dir`, and resets `status = 'pending'` with `quarantine_dir = NULL`.
       - *Case 4 (Neither exists):* Workspace already gone. Recovery marks `status = 'completed'`.
     * **`quarantined` Recovery:**
       - If `quarantine_dir` exists, verify ownership, delete via `secure_rmtree`, and mark `status = 'completed'`.
       - If `quarantine_dir` is already absent, mark `status = 'completed'`.
     * **Canonical Path Containment Validation:** Before any recovery deletion, recovery validates that `quarantine_dir` is non-empty, matches `{ws_basename}.quarantine_{lease_token}`, and is canonically contained within the authorized workspace base directory (`os.path.commonpath`), strictly rejecting path-prefix sibling attacks and symlink escapes.
   - **Deletion-Phase Ownership, Short Transactions & Stale-Worker Fencing:**
     * **Short-Transaction Recovery Claim:** To prevent SQLite write lock starvation and concurrency bottlenecks, recovery workers claim stale records using short atomic `BEGIN IMMEDIATE` transactions via `claim_stale_cleanup_for_recovery`:
       ```sql
       UPDATE pending_cleanups
       SET worker_id = :new_worker_id,
           lease_token = :new_lease_token,
           lease_expires_at = datetime('now', '+120 seconds'),
           updated_at = CURRENT_TIMESTAMP
       WHERE id = :id
         AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP);
       ```
       The database write lock is immediately released once the claim commits; slow recursive filesystem deletions are never performed while holding a database write transaction.
     * **Dual-Layer Fencing & Lock Yield Coherence:**
       - If a recovery worker claims a stale record in SQLite but fails to acquire `WorkspaceFileLock` (because an active process is still executing filesystem operations), recovery **immediately releases its DB claim** (`lease_expires_at = datetime('now', '-1 second')`) and yields, ensuring the record does not remain locked in the database for 120 seconds.
       - If a worker's lease expires during recursive deletion, the worker's Step 5 completion update matches 0 rows and is rejected. The recovery worker subsequently acquires the lock, verifies the directory is deleted (or finishes deletion), and commits completion.
     * Stale workers cannot overwrite newer cleanup state or interfere with recovery operations.
   - **Crash Recovery & Startup Reclamation of Stale Claims:**
     On application startup (or during periodic sweeps), the lifespan handler reclaims any stale `in_progress` records left behind by a crash or SIGKILL:
     ```sql
     UPDATE pending_cleanups
     SET status = 'pending',
         worker_id = NULL,
         lease_token = NULL,
         lease_expires_at = NULL,
         last_error = 'Reclaimed after application restart (lease expired or worker terminated)',
         updated_at = CURRENT_TIMESTAMP
     WHERE status = 'in_progress' AND (lease_expires_at IS NULL OR lease_expires_at < CURRENT_TIMESTAMP);
     ```
   - **Persistent Retry Scheduling (`next_attempt_at`) & Error Classification:**
     * **Retry Deadline Calculation:** Retries use exponential backoff:
       $$\text{delay} = \min(300, 10 \times 2^{\text{retry\_count}})\text{ seconds}$$
       `next_attempt_at = datetime('now', '+' || delay || ' seconds')`.
     * **Transient Docker Unavailability (`TRANSIENT_DOCKER_UNAVAILABLE`):** When the Docker daemon is unreachable or the socket connection is refused, the record returns to `status = 'pending'`, sets `error_type = 'TRANSIENT_DOCKER_UNAVAILABLE'`, and schedules `next_attempt_at = datetime('now', '+30 seconds')`. **It does NOT consume the permanent retry limit**, because host files cannot safely be unlinked without confirming container status.
     * **Permanent Cleanup Errors (`PERMANENT_ERROR`):** Non-connectivity errors (e.g. filesystem permission denial, unremovable busy devices) increment `retry_count`. If `retry_count < 5`, `next_attempt_at` is set with exponential backoff and status remains `pending`. Once `retry_count >= 5` on permanent failure, the record transitions to terminal `status = 'failed'`, `next_attempt_at = NULL`, and `error_type = 'PERMANENT_ERROR'`.
     * **Startup Respect for Deadlines:** Startup recovery queries strictly records where `next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP`. Records in backoff survive restarts and are not retried prematurely.
     * **Reactive Docker Health Recovery Trigger:** When a Docker health check passes or a Docker API call succeeds, the application reactively accelerates transient retries:
       ```sql
       UPDATE pending_cleanups
       SET next_attempt_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
       WHERE status = 'pending' AND error_type = 'TRANSIENT_DOCKER_UNAVAILABLE';
       ```
       This immediately wakes up eligible transient records without modifying `retry_count`, without bypassing lease ownership checks, and without touching permanent `failed` records.
     * **Terminal `failed` State & Operator Recovery:** `status = 'failed'` is **terminal**. The automatic background worker and standard startup sweeps **DO NOT** pick up `failed` records, strictly preventing infinite retry loops. An operator or administrator can explicitly reset a failed cleanup record via `POST /admin/cleanups/{id}/retry` or CLI:
       ```sql
       UPDATE pending_cleanups
       SET status = 'pending',
           retry_count = 0,
           worker_id = NULL,
           lease_expires_at = NULL,
           next_attempt_at = CURRENT_TIMESTAMP,
           error_type = NULL,
           last_error = NULL,
           updated_at = CURRENT_TIMESTAMP
       WHERE id = :id;
       ```
   - **Ownership Verification via Labels:** Before terminating any container, the cleanup engine verifies container labels: `--filter "label=dev.dua.managed=true"`. It asserts that `dev.dua.run_id` matches the `originating_run_id`. Unrelated containers (lacking `dev.dua.managed=true` or belonging to other projects) are strictly ignored.
   - **Strict Order of Operations & Idempotency:**
     1. Query Docker for container by ID or label `dev.dua.run_id=<originating_run_id>`.
     2. If container exists and is running: execute `docker stop -t 5 <container_id>`.
     3. Remove container: execute `docker rm -f <container_id>`.
     4. If container is already exited, proceed directly to `rm -f`.
     5. If container does not exist (e.g. already removed, pruned, or 404), proceed directly to workspace deletion.
     6. **Workspace Deletion Invariant:** Host workspace directories are deleted (`shutil.rmtree`) **strictly after** confirmed container termination and removal AND pre-deletion lease ownership verification. Under no circumstances are host directories unlinked while a container may still be mounting them.
     7. If the workspace directory is already gone, mark cleanup `completed` harmlessly (idempotent).
     8. If Docker daemon is unavailable, the record returns to `status = 'pending'`, and the host workspace remains untouched (fails closed).
4. **Startup Recovery Across All Active States:**
   When the FastAPI application boots up in its lifespan handler, it sweeps active runs:
   - **`queued` Runs:** Marked `failed` (`error_message = 'Interrupted: server restarted before execution began'`, `completed_at = CURRENT_TIMESTAMP`).
   - **`running` Runs:** Marked `failed` (`error_message = 'Interrupted: server restarted during pipeline execution'`, `completed_at = CURRENT_TIMESTAMP`). An entry is created in `pending_cleanups` with `status = 'pending'` to guarantee container and workspace cleanup even across repeated restart cycles or Docker outages.
   - **`awaiting_approval` Runs (Strong Integrity Verification):**
     Before resuming an `awaiting_approval` run, the engine executes six mandatory cryptographic and structural integrity checks:
     1. **Workspace Structure Verification:** Asserts that `workspace_dir`, `reference/`, and `working/` subdirectories, `proposal.json`, and `patch.diff` all exist on disk.
     2. **Cryptographic Artifact Match:** Computes SHA-256 of `proposal.json` and `patch.diff` on disk; asserts exact match against `proposal_sha256` and `patch_sha256` stored in the database record.
     3. **Repository Identity Match:** Asserts that the repository ID recorded in the workspace metadata (`.dua_repo_id` or Git remote origin) exactly matches `analysis_runs.repository_id`.
     4. **Baseline Git Commit Invariant:** Executes `git rev-parse HEAD` in the workspace; asserts exact match against `analysis_runs.base_commit_hash` recorded at run creation.
     5. **Proposal & Patch Mutual Consistency:** Asserts that all files targeted in `patch.diff` match the upgraded dependencies and modified files declared in `proposal.json`.
     6. **Working Copy Patch Validity:** Executes `git apply --check patch.diff` in `working/` to verify that the working tree remains in a clean, compatible state and the patch can be applied cleanly without conflict.
     * **Integrity Failure Policy:** If **any** of the six checks fail, the run is **never resumed**. It transitions immediately to `failed` (`error_message = 'INTEGRITY_CHECK_FAILED: <specific_reason>'`), and its workspace is enqueued in `pending_cleanups`. Only runs passing all six checks remain in `awaiting_approval` with status note `'Resumed after restart'`.
5. **Deadlock / Permanent Blocking Defense & Atomic Supersede:**
   To ensure an abandoned `awaiting_approval` run never blocks a repository permanently:
   1. The user can cancel it at any time via `POST /runs/{id}/cancel` (or UI "Discard Run"), which transitions the run to `cancelled` and immediately frees the partial unique index.
   2. A new run can be triggered with `POST /repositories/{id}/runs` passing `force_supersede: true`. This executes an atomic transaction (`BEGIN IMMEDIATE`):
      - Verifies that previous active run is in `awaiting_approval`.
      - Transitions previous run to `cancelled` (`cancellation_reason = 'SUPERSEDED'`).
      - Inserts the new run with `status = 'queued'`.
      - Commits transaction. If insertion fails, transaction rolls back automatically and the previous run remains in `awaiting_approval`.
6. **Idempotency & Duplicate Request Handling:** Clients can pass an optional `Idempotency-Key` header on `POST /repositories/{id}/runs`. The unique index on `idempotency_key` ensures that retransmitted requests return the existing run record with status `200 OK` rather than initiating a duplicate run.
7. **Event Persistence & SSE Reconnection (`Last-Event-ID`):**
   - Every stage transition, log line, and progress update is written to the `pipeline_events` SQLite table with an auto-incrementing `seq_id`.
   - The frontend connects to `GET /runs/{id}/events`.
   - If the connection drops, the frontend reconnects with `Last-Event-ID: <seq_id>`. The backend replays missed events before streaming live events.
8. **Client Disconnection Resilience & Clean Lock Release:** The background pipeline runs as an independent `asyncio` task. Closing the browser tab does not terminate the run. On normal completion, failure, or cancellation, the run status updates to a terminal state, automatically freeing the repository lock in the partial unique index.

### 4. Local API Security & CSRF Protections
- **Host Validation:** Starlette middleware inspects the `Host` header. Requests with hosts other than `localhost:[port]` or `127.0.0.1:[port]` are rejected with `400 Bad Request` to neutralize DNS-rebinding attacks.
- **Strict Origin Whitelisting:** On mutating endpoints (`POST`, `PUT`, `DELETE`), the `Origin` header must match `http://localhost:[frontend_port]`. Missing `Origin` headers (on browser requests), `Origin: null` (from sandboxed iframes or `file://`), or foreign origins are rejected with `403 Forbidden`.
- **Content-Type Whitelist:** Mutating routes reject `application/x-www-form-urlencoded`, `multipart/form-data`, and `text/plain`, strictly requiring `application/json`.
- **Custom Anti-CSRF Header:** Mutating routes require `X-DUA-Client: web`.

### 5. Patch Synthesis & Hunk Integrity
- **Hunk Collision Detection:** Proposed modifications are stored as discrete hunks. If a user approves Hunk 2 but rejects Hunk 1 within the same file, `PatchSynthesizer` verifies that line offsets remain valid. If hunks overlap or collide, synthesis aborts with `PATCH_CONFLICT`, prompting the user for whole-file review or diff re-evaluation.
- **Stale Patch Guard:** Before applying patches, target files on disk are verified against their SHA-256 hashes recorded at analysis time. If modified externally, the patch is rejected as stale.
- **Post-Synthesis Syntax Check:** Immediately following file modification, `ast.parse()` is executed on all modified `.py` files. If any syntax error occurs, the working copy is rolled back to pristine reference state.

## Failure Modes & Resilience
| Dependency or Step | Failure Mode | Behavior |
|--------------------|--------------|----------|
| **Git Clone** | Authentication failure, 404, or network timeout. | Fail fast, record error message, clear credentials from memory, abort run without creating corrupt workspace. |
| **PyPI JSON API** | Network timeout, 5xx server error, or rate limiting. | Fall back to local cached catalog (`src/data/package_catalog.json`); mark dependency provenance as `Local Catalog`; continue analysis without blocking. |
| **Local Ollama Daemon** | Daemon not running or model not found. | Fail open to deterministic-only analysis mode; generate upgrade candidates and AST findings without AI risk summaries; mark advisory status as `UNAVAILABLE`. |
| **Cloud LLM API** | Invalid API key, rate limit, or user denies consent. | If consent denied: block call at boundary. If API error: fall back to deterministic mode; log diagnostic error without exposing user source code. |
| **Docker Daemon** | Docker CLI missing or daemon unreachable. | Fail closed: classify validation outcome as `NOT_RUNNABLE`; display actionable setup guide; **never execute tests on host**. |
| **Patch Application** | Stale working directory or overlapping hunk collision. | Abort patch synthesis; roll back working copy; mark run as `STALE_PATCH_CONFLICT`; prompt user to re-generate diffs. |
| **Validation Test Runner** | Test execution times out (exceeds 60s) or test fails. | Kill container process; capture stdout/stderr; mark validation status as `FAILED` (or `TIMED_OUT`); retain logs for inspection. |

## Quality Attributes & Operational Rigor
- **Performance:** In-memory caching for PyPI metadata responses; asynchronous background task processing prevents UI blocking; P95 query latency under 200ms.
- **Maintainability & Brownfield Preservation:** Core domain logic under `src/` is preserved intact behind interface seams; all 146 existing unit/integration tests remain active in CI and pass without modification.
- **Observability:** Structured logging with unique `run_id` correlation; real-time event streaming via SSE with `Last-Event-ID` replay; SHA-256 audit log verified by `aidlc.py verify`.

## Decision Records
- [ADR-001: Decoupled Architecture with FastAPI and React](file:///Users/soham/Github/Dependency Upgrade Assistant/docs/aidlc/adr/ADR-001-decoupled-architecture-with-fastapi-and-react.md): Migrating from monolithic Streamlit to FastAPI REST/SSE backend and React SPA.
- [ADR-002: Docker Sandbox Containment, Network Isolation, and Filesystem Security](file:///Users/soham/Github/Dependency Upgrade Assistant/docs/aidlc/adr/ADR-002-docker-sandbox-containment-and-filesystem-security.md): Network namespace loopback reality, balanced tmpfs sizing, noexec by default, and bytecode isolation.
- [ADR-003: In-Memory Credential Delivery via Ephemeral Authenticated UNIX Domain Socket IPC and Git Hardening](file:///Users/soham/Github/Dependency Upgrade Assistant/docs/aidlc/adr/ADR-003-in-memory-credential-delivery-via-ephemeral-git-as.md): Authenticated ephemeral UNIX domain socket IPC avoiding process environment variable leaks, defending against same-UID socket races, and suppressing external helpers/hooks.
- [ADR-004: Provider-Independent AI Abstraction and Privacy Opt-In](file:///Users/soham/Github/Dependency Upgrade Assistant/docs/aidlc/adr/ADR-004-provider-independent-ai-abstraction-and-privacy-op.md): Zero-Code-Exfiltration Local Privacy Policy with explicit per-repository opt-in consent for hosted LLM providers.

## Requirements Traceability Matrix
| Requirement ID | Architectural Component | Enforcing Module | Verification Approach |
|---|---|---|---|
| **FR-1** | Git Service & Workspace Manager | `src/services/git_service.py`, `workspace_manager.py` | Unit tests with mock Git CLI; audit assertion of zero disk tokens. |
| **FR-2** | Manifest Scanner & PyProject Parser | `src/analysis/manifest_scanner.py`, `pyproject_parser.py` | Unit tests across multi-manifest fixtures (`tests/test_manifest_scanner.py`). |
| **FR-3** | Dependency Analyzer & PyPI Client | `src/analysis/dependency_analyzer.py` | Integration tests with cached/offline PyPI fixtures (`tests/test_dependency_analyzer.py`). |
| **FR-4** | LLM Provider Abstraction | `src/services/llm/` (`LLMProvider` Protocol) | Unit tests verifying zero external requests when consent is false. |
| **FR-5** | Code Inspector & AST Engine | `src/analysis/code_inspector.py` | AST parsing tests verifying detection of deprecated APIs (`tests/test_code_inspector.py`). |
| **FR-6** | Pipeline Orchestrator & Approval Gate | `src/pipeline/orchestrator.py`, `api/routes/approvals.py` | Integration tests asserting zero disk mutations without explicit approval. |
| **FR-7** | Monaco Diff Reviewer & Frontend | `frontend/src/components/diff/MonacoDiffViewer.tsx` | Component tests verifying side-by-side, inline, and hunk toggle interactions. |
| **FR-8** | Patch Synthesizer & AST Check | `src/core/diff_generator.py`, `src/services/patch_synthesizer.py` | Unit tests verifying collision detection, stale patch rejection, and syntax checks. |
| **FR-9** | Docker Sandbox Manager | `src/validation/sandbox_manager.py`, `test_runner.py` | Security flag inspection tests, timeout tests, fail-closed assertion tests. |
| **FR-10** | Artifact Exporter | `src/services/exporter.py` | Cryptographic SHA-256 verification tests (`tests/test_exporter.py`). |
| **FR-11** | Decoupled Modern Frontend | `frontend/src/` (React + Tailwind + shadcn/ui) | End-to-end user workflow verification and accessibility audits. |
| **FR-12** | Event Bus & SSE Controller | `api/routes/events.py`, `src/pipeline/orchestrator.py` | Streaming endpoint unit tests verifying stage transition events and `Last-Event-ID` replay. |
| **FR-13** | SQLite Storage Service | `src/services/storage/sqlite_storage.py` | Database persistence, partial unique index concurrency, and round-trip query tests. |
| **FR-14** | Clean Interface Seams | `src/core/interfaces/` | Interface compliance tests asserting zero remote dependencies in local mode. |
| **NFR-1** | Local Security Middleware | `api/middleware/security.py`, `git_service.py` | DNS-rebinding, Origin rejection, CSRF header, and credential lifecycle tests. |
| **NFR-2** | Privacy Controller | `src/services/llm/privacy_guard.py` | Network isolation tests asserting zero outbound telemetry without consent. |
| **NFR-3** | Deterministic Engine | `src/analysis/dependency_analyzer.py` | Asserting deterministic package resolution without LLM dependencies. |
| **NFR-4** | Sandbox Containment | `src/validation/sandbox_manager.py` | Container flag validation asserting `--network none`, `--cap-drop=ALL`, `--read-only`, `--tmpfs /tmp:rw,noexec,nosuid,nodev`. |
| **NFR-5** | Performance & Cache | `src/analysis/dependency_analyzer.py` | Benchmark tests verifying P95 latency < 200ms on cached queries. |
| **NFR-6** | Accessible UI | `frontend/src/` | Automated a11y testing verifying WCAG AA compliance. |
| **NFR-7** | Brownfield Test Preservation | All existing test suites | Full execution of all 146 existing tests via pytest. |
| **NFR-8** | Audit Trail Verification | `.aidlc/` and `scripts/aidlc.py` | Automated execution of `aidlc.py verify` in CI. |
