# M5 Sandboxed Validation Architecture Specification

## 1. Executive Summary

Dependency Upgrade Assistant 2.0 validates dependency upgrades by executing syntax checks, installation routines, and test suites in containerized isolation. Because target repositories, manifests, and test suites may contain untrusted code, arbitrary build scripts, or potential supply-chain malware, the execution environment must enforce strict containment guarantees.

This document specifies the architecture of the **M5 Sandboxed Validation Subsystem**, designed to provide:
1. **Strict Defense-in-Depth Isolation:** Non-root execution, `--network none`, dropped capabilities, `no-new-privileges`, read-only root filesystems, and temporary memory-backed filesystems (`tmpfs`).
2. **Deterministic Workspace Snapshots:** Ephemeral test directories with automated secret exclusion, credential stripping, and symlink escape rejection.
3. **Provider-Independent Abstraction:** Clean separation between validation logic and container orchestration, supporting graceful degradation when container engines are unavailable.
4. **Truthful Environment Reporting:** Explicit distinction between `PASSED`, `FAILED`, `TIMED_OUT`, `CANCELLED`, `INFRASTRUCTURE_ERROR`, and `NOT_RUNNABLE`. Untrusted code is **never** executed directly on the host.
5. **Fail-Closed Lifecycle & Cleanup:** Guaranteed container termination, removal, and volume deallocation on success, failure, timeout, or server interruption.

---

## 2. Decoupled 2-Stage Execution Model

To reconcile the need for dependency acquisition with strict network isolation during test execution, the validation subsystem implements a decoupled 2-stage lifecycle:

```
┌────────────────────────────────────────────────────────────────────────┐
│               Stage A: Controlled Staging (Optional)                   │
│  - Environment: Strictly validated package registries (PyPI, npmjs)    │
│  - Validation: is_safe_registry_url() blocks RFC 1918, link-local, SSRF │
│  - Action: Fetch pre-built wheels / tarballs into isolated cache       │
│  - Restriction: Zero execution of repository setup.py, install hooks   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Cache Mount (ro)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│             Stage B: Sandboxed Test Execution (Strict Offline)         │
│  - Environment: Strict offline isolation (--network none)              │
│  - Immutability: Pinned SHA-256 image references (python/node)         │
│  - Action: Install from local cache and execute discovered test suite  │
│  - Restrictions:                                                       │
│      * --network none (Server-enforced; cannot be overridden by client)│
│      * --read-only root filesystem                                     │
│      * --tmpfs /tmp:rw,noexec,nosuid,size=64m                          │
│      * --tmpfs /workspace/scratch:rw,size=128m                         │
│      * Resource ceilings: RAM <= 1024m, CPU <= 2.0, PIDs <= 128        │
│      * --security-opt=no-new-privileges:true --cap-drop=ALL            │
│      * --user 1000:1000                                                │
│      * Bounded streaming stdout/stderr reader threads (512 KB budget)  │
│      * Explicit test discovery: zero tests yields NO_TESTS_FOUND       │
└────────────────────────────────────────────────────────────────────────┘
```

### Stage A: Dependency Acquisition (Staging Status & Scope)
- **Current Scope (M5):** Operates strictly in offline mode (`status: NOT_REQUIRED`); automated dynamic network downloading of third-party wheels from external package repositories is deferred to Phase 3.
- When dynamic staging is enabled in Phase 3, registry destinations will be strictly filtered via `DependencyStagingManager` and `is_safe_registry_url()`, rejecting non-HTTPS, localhost, private IP ranges (10.x, 172.16.x, 192.168.x), and cloud metadata IP (`169.254.169.254`).
- Arbitrary pre-install hooks (`setup.py` arbitrary code execution) are prevented by enforcing wheel binary requirements (`--only-binary=:all:`) where feasible.
- In M5, if a repository test requires unstaged dependencies, the execution truthfully fails or reports `NO_TESTS_FOUND`, and is never falsely marked as successful.

### Stage B: Sandboxed Test Execution
- Container is spawned with `--network none` (immutable server-side policy; client requests cannot enable networking).
- Repository snapshot is mounted read-only into an ephemeral directory with memory-backed `/workspace/scratch` and `/tmp` tmpfs mounts.
- Container image is resolved to its pinned immutable SHA-256 digest (`ImageCatalog.get_pinned_image_reference()`).
- Bounded streaming readers capture process output in real time without blocking pipes or exceeding memory budgets.
- Test discovery inspects repository fixtures; if no test files exist, status returns `NO_TESTS_FOUND`, preventing false `PASSED` results.

---

## 3. Subsystem Architecture & Component Decomposition

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           FastAPI Layer                                 │
│  - POST /api/validation/execute                                         │
│  - GET  /api/validation/{execution_id}                                  │
│  - GET  /api/validation/{execution_id}/logs                             │
│  - POST /api/validation/{execution_id}/cancel                           │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      Validation Service Layer                           │
│  (src/services/sandbox_service.py)                                      │
│  - Injects ProposalRegistry to verify proposal authenticity             │
│  - Resolves target ecosystem and select allowlisted container image    │
│  - Tracks active executions, status, durations, and bounded logs        │
└──────────────┬───────────────────────────────────────────┬──────────────┘
               │                                           │
               ▼                                           ▼
┌───────────────────────────────┐         ┌───────────────────────────────┐
│       Snapshot Manager        │         │      Image Catalog            │
│ (src/sandbox/snapshot_mgr.py) │         │ (src/sandbox/image_catalog.py)│
│ - Ephemeral temporary dir     │         │ - Pinned SHA-256 digests:     │
│ - Secret & token scrubbing    │         │   * python:3.11-slim@sha256.. │
│ - Symlink escape rejection    │         │   * python:3.12-slim@sha256.. │
│ - Special file (FIFO) error   │         │   * node:20-slim@sha256..     │
│ - SnapshotManifest auditing   │         │   * node:22-slim@sha256..     │
│ - Proposal mutation overlay   │         │ - Arbitrary images rejected   │
└──────────────┬────────────────┘         └───────────────┬───────────────┘
               │                                          │
               ▼                                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     Workspace Boundary Validation                       │
│  (src/sandbox/workspace_boundary.py)                                    │
│  - Validates relative paths against trusted root (settings.workspace)   │
│  - Strictly blocks client-specified host roots, /, /etc, Windows paths  │
│  - Resolves symlinks and detects sibling prefix confusion (/root_evil)  │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       Container Configuration                           │
│  (src/sandbox/container_config.py)                                      │
│  - Enforces mandatory security flags:                                   │
│      --rm, --user, --network=none, --memory, --cpus, --pids-limit,      │
│      --security-opt=no-new-privileges, --cap-drop=ALL, --read-only      │
│  - Enforces server-side resource ceilings (RAM: 1024m, CPUs: 2.0)      │
│  - Prohibits docker.sock, --privileged, host namespaces                 │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Sandbox Runner Interface & Engine                    │
│  (src/sandbox/runner.py & src/sandbox/streaming_capture.py)             │
│  - Probes Docker engine availability (docker info)                      │
│  - Returns truthful NOT_RUNNABLE when Docker is unavailable             │
│  - Spawns subprocess.Popen with real-time process handle registration   │
│  - Streaming background threads enforce 512KB quota and strip ANSI      │
│  - Real-time atomic cancellation: docker rm -f + process kill           │
│  - Enforces hard timeouts and fail-closed cleanup (SIGTERM -> SIGKILL)  │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Security Containment Specifications

### 4.1 Mandatory Docker Flags
Every container invocation generated by `ContainerConfigBuilder` must enforce:

| Flag | Parameter / Value | Purpose |
|:---|:---|:---|
| `--rm` | (flag present) | Ensures automatic container cleanup upon exit |
| `--user` | `$(id -u):$(id -g)` / non-root | Executes as non-root user matching host identity (fallback `1000:1000`); prevents root privilege abuse |
| `--network` | `none` (default) | Complete network isolation; prevents exfiltration & C2 |
| `--memory` | `512m` (default) | Hard memory ceiling; prevents host OOM exhaustion |
| `--cpus` | `1.0` (default) | CPU core quota; prevents CPU starvation |
| `--pids-limit`| `64` (default) | Process count cap; prevents fork-bomb attacks |
| `--cap-drop` | `ALL` | Drops all Linux capabilities (CAP_SYS_ADMIN, etc.) |
| `--security-opt`| `no-new-privileges:true`| Disallows setuid / privilege escalation |
| `--read-only` | (flag present) | Mounts root container filesystem read-only |
| `--tmpfs` | `/tmp:rw,noexec,nosuid,size=64m` | Controlled in-memory temporary scratch space |

### 4.2 Forbidden Configurations
The container configuration builder strictly forbids and throws `ValueError` upon:
1. **Docker Socket Mounts:** Mounting `/var/run/docker.sock` or any socket file.
2. **Privileged Mode:** Using `--privileged` under any circumstance.
3. **Host Networking:** Using `--network=host` or `--net=host`.
4. **Host Root Mounting:** Mounting the host filesystem root (`/`) or host system directories (`/etc`, `/usr`, `/bin`, `/var`).
5. **Arbitrary Container Images:** Images outside the allowlisted catalog are strictly rejected.

---

## 5. Workspace Snapshot Isolation Protocol

Target repositories and proposals must never be executed directly within their host workspace:
1. **Workspace Boundary Validation:**
   - Any workspace path is verified using `validate_trusted_workspace_path(path, settings.resolved_workspace_root)`.
   - Client requests are prohibited from supplying arbitrary host paths (such as `/`, `/etc`, Windows drive letters `C:`, or path traversals).
   - Sibling prefix confusion (`/workspace_evil` vs `/workspace`) is prevented by enforcing path containment with trailing separator checks.
2. **Ephemeral Directory & Filesystem Permissions:**
   - Ephemeral snapshot directories are created with strict mode `0700` (`rwx------`).
   - World-writable permissions (`0777`) are strictly forbidden (`mode & 0o002 == 0`).
   - Other users on the host are denied all access (`mode & 0o007 == 0`).
   - Copied files strip group and other write permissions (`mode & ~(stat.S_IWGRP | stat.S_IWOTH)`).
   - Container execution matches the host execution identity (`--user $(id -u):$(id -g)`), enabling the container process full owner permissions (`rwx`) on `/workspace/working` without expanding host-level permissions.
3. **Secret & Sensitive File Scrubbing:** During snapshot replication, sensitive paths are excluded:
   - Git metadata: `.git/`
   - Secrets & Environment files: `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`
   - Cloud credentials: `.aws/`, `.gcp/`, `.azure/`, `.npmrc`, `.pypirc`, `.docker/`, `.kube/`
   - Temporary and virtual environments: `.venv/`, `venv/`, `node_modules/`, `__pycache__/`
   - Safe dotfiles (`.pytest.ini`, `.ruff.toml`) are retained to preserve test discovery configuration.
4. **Symlink and Special File Validation:**
   - Symlinks in the source workspace are evaluated via `os.path.realpath`. Any symlink whose resolved target escapes the workspace boundary is skipped or rejected with `SecurityException`.
   - Special files (FIFOs, named pipes, Unix domain sockets) detected via `stat.S_ISFIFO` or `stat.S_ISSOCK` cause immediate failure (`SnapshotCreationError`), protecting against hanging copy operations.
5. **SnapshotManifest Auditing:** The snapshot creation returns a cryptographic `SnapshotManifest` recording the snapshot path, file count, and list of excluded sensitive entries.
6. **Proposal Mutation Overlay:** If an approved `proposal_id` is specified, the server retrieves the proposal from `ProposalRegistry` and writes the proposed manifest content into the snapshot.

---

## 6. Execution Lifecycle, Cancellation & Status Model

```
       ┌───────────┐
       │  QUEUED   │
       └─────┬─────┘
             │
             ▼
       ┌───────────┐      Docker Unavailable
       │  PROBING  ├──────────────────────────┐
       └─────┬─────┘                          │
             │ Docker Available               │
             ▼                                ▼
       ┌───────────┐                   ┌──────────────┐
       │ EXECUTING │                   │ NOT_RUNNABLE │
       └─────┬─────┘                   └──────────────┘
             │
   ┌─────────┼──────────┬───────────┐
   │         │          │           │
   ▼         ▼          ▼           ▼
┌────────┐┌────────┐┌─────────┐┌───────────┐
│ PASSED ││ FAILED ││TIMED_OUT││ CANCELLED │
└────────┘└────────┘└─────────┘└───────────┘
```

### Status Definitions:
- `PASSED`: All validation commands completed with exit code 0 and actual tests were executed.
- `FAILED`: One or more validation commands returned a non-zero exit code.
- `TIMED_OUT`: Container execution exceeded the configured timeout (e.g. 60s) and was killed.
- `CANCELLED`: Execution was explicitly cancelled by the user via `/api/validation/{id}/cancel`. Container is immediately removed (`docker rm -f`) and subprocess terminated. Subsequent completion cannot overwrite cancelled state.
- `NO_TESTS_FOUND`: Test discovery found zero test files or only npm default placeholders (`echo ... && exit 1`). Truthfully reports absence of tests, strictly avoiding false-positive `PASSED` results.
- `INFRASTRUCTURE_ERROR`: A container engine error occurred (e.g. Docker daemon crashed or out of memory).
- `NOT_RUNNABLE`: Docker engine is not installed or unreachable. Untrusted code is **not** run on host.

---

## 7. Bounded Streaming Log Capture & Thread Safety

To protect backend server memory against high-output loops (e.g. infinite print statements):
- Container `stdout` and `stderr` streams are captured through non-blocking streaming background reader threads (`BoundedStreamCapture`).
- Enforces a strict memory budget (default: 512 KB).
- When the log size limit is reached, subsequent output continues to be drained from the OS pipe to prevent pipe deadlock, while the buffer records an informative truncation notice: `"... [Output truncated after reaching 524,288 bytes limit] ..."`.
- ANSI terminal escape sequences (colors, cursor movements) are stripped to prevent log injection in the web UI.
