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
│                        Stage 1: Staging (Controlled)                   │
│  - Environment: Controlled network access (PyPI / npm registries only) │
│  - Action: Fetch pre-built wheels / tarballs into cache directory      │
│  - Restriction: No execution of target repository setup.py or tests   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Cache Mount (ro)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   Stage 2: Sandboxed Execution (Isolated)              │
│  - Environment: Strict offline isolation (--network none)              │
│  - Action: Install from local cache and execute test suite             │
│  - Restrictions:                                                       │
│      * --network none                                                  │
│      * --read-only root filesystem                                     │
│      * --tmpfs /tmp:rw,noexec,nosuid,size=64m                          │
│      * --tmpfs /workspace/scratch:rw,size=128m                         │
│      * --memory=512m --cpus=1.0 --pids-limit=64                       │
│      * --security-opt=no-new-privileges --cap-drop=ALL                 │
│      * --user 1000:1000                                                │
└────────────────────────────────────────────────────────────────────────┘
```

### Stage 1: Dependency Acquisition (Staging)
- Only wheels/tarballs matching the target manifest are acquired.
- In offline or pre-baked scenarios, standard base environments provide common testing harnesses (`pytest`, `unittest`, `ruff` for Python; `node:test`, `vitest`, `jest` for Node.js).
- Arbitrary pre-install hooks (`setup.py` arbitrary code execution) are prevented by enforcing wheel binary requirements (`--only-binary=:all:`) where feasible.

### Stage 2: Sandboxed Test Execution
- Container is spawned with `--network none`.
- Repository snapshot is mounted read-only or into an ephemeral scratch space.
- The host filesystem is never mounted read-write.
- The container executes strictly under an unprivileged user (`1000:1000`).

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
│ - Ephemeral temporary dir     │         │ - Allowlisted images only:    │
│ - Secret & token scrubbing    │         │   * python:3.11-slim          │
│ - Symlink escape rejection    │         │   * python:3.12-slim          │
│ - Apply proposal mutations    │         │   * node:20-slim              │
└──────────────┬────────────────┘         │   * node:22-slim              │
               │                          │ - Arbitrary images rejected   │
               │                          └───────────────┬───────────────┘
               ▼                                          │
┌─────────────────────────────────────────────────────────▼───────────────┐
│                       Container Configuration                           │
│  (src/sandbox/container_config.py)                                      │
│  - Enforces mandatory security flags:                                   │
│      --rm, --user, --network=none, --memory, --cpus, --pids-limit,      │
│      --security-opt=no-new-privileges, --cap-drop=ALL, --read-only      │
│  - Prohibits docker.sock, --privileged, host networking                 │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Sandbox Runner Interface & Engine                    │
│  (src/sandbox/runner.py & src/validation/sandbox_manager.py)            │
│  - Probes Docker engine availability (docker info)                      │
│  - Returns truthful NOT_RUNNABLE when Docker is unavailable             │
│  - Spawns and supervises container execution with bounded log capture   │
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
| `--user` | `1000:1000` | Executes as non-root user; prevents root privilege abuse |
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
1. **Ephemeral Directory:** An ephemeral working directory is created under `tempfile.TemporaryDirectory()`.
2. **Secret & Sensitive File Scrubbing:** During snapshot replication, sensitive paths are excluded:
   - Git metadata: `.git/`
   - Secrets & Environment files: `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`
   - Cloud credentials: `.aws/`, `.gcp/`, `.azure/`, `.npmrc`, `.pypirc`
   - Temporary and virtual environments: `.venv/`, `venv/`, `node_modules/`, `__pycache__/`
3. **Symlink Validation:** Symlinks in the source workspace are evaluated via `os.path.realpath`. Any symlink whose resolved target escapes the workspace boundary is skipped or rejected with `SecurityException`.
4. **Proposal Mutation Overlay:** If an approved `proposal_id` is specified, the server retrieves the proposal from `ProposalRegistry` and writes the proposed manifest content into the snapshot.

---

## 6. Execution Lifecycle & Status Model

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
- `PASSED`: All validation commands completed with exit code 0.
- `FAILED`: One or more validation commands returned a non-zero exit code.
- `TIMED_OUT`: Container execution exceeded the configured timeout (e.g. 60s) and was killed.
- `CANCELLED`: Execution was explicitly cancelled by the user via `/api/validation/{id}/cancel`.
- `INFRASTRUCTURE_ERROR`: A container engine error occurred (e.g. Docker daemon crashed or out of memory).
- `NOT_RUNNABLE`: Docker engine is not installed or unreachable. Untrusted code is **not** run on host.

---

## 7. Bounded Log Streaming & Memory Safety

To protect backend server memory against high-output loops (e.g. infinite print statements):
- Container `stdout` and `stderr` streams are captured through bounded buffers with a maximum threshold (default: 512 KB).
- When the log size limit is reached, subsequent output is truncated with an informative notice: `"... [Output truncated after reaching 524,288 bytes limit] ..."`.
- Logs are sanitized to prevent ANSI terminal escapes from injecting control characters into the web UI.
