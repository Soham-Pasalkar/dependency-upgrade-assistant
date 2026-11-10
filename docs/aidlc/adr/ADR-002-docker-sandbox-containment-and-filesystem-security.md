# ADR-002: Docker Sandbox Containment, Network Isolation, and Filesystem Security

- Status: proposed (submitted for Gate G2 review)
- Date: 2026-11-09

## Context
> **Baseline Prototype Note:** The existing brownfield prototype (`src/validation/sandbox_manager.py`) implements a basic container runner using synchronous `docker run` subprocess commands and local workspace directories. It lacks persistent orphan recovery, POSIX advisory file locking, crash-consistent quarantine state transitions, and hardened network isolation. This ADR defines the target production architecture to be constructed in Construction Phase 2 while preserving existing test interfaces.

Dependency Upgrade Assistant validates proposed dependency and code changes by executing the target repository's test suite inside an ephemeral Docker container. Because target repositories may contain untrusted code, unvetted test fixtures, or malicious build hooks, the execution environment must enforce strict containment guarantees without breaking legitimate Python test workflows.

Rigorous evaluation of Docker containment flags and the Python testing runtime identified key technical constraints:
1. **Network Namespace Realities (`--network none`):** `--network none` does not mean zero network interfaces; it isolates the container into a private network namespace possessing strictly the loopback interface (`lo` / `127.0.0.1`), with no external network interfaces (`eth0`), default gateways, or DNS resolvers. Processes inside the container can bind to localhost (e.g. for mock HTTP servers within test suites), but cannot establish outbound connections outside the container or reach host network services.
2. **Temporary Files & `noexec` on `/tmp`:**
   - *Standard Python Testing Needs:* Pure Python unit tests executed via `pytest` read `.py` files from the working copy and interpret them in memory. Pytest cache files (`.pytest_cache`) are data files, not executables. Temporary files created by `tempfile.NamedTemporaryFile` or pytest's `tmp_path` fixture for JSON, text, or database fixtures are opened for read/write and do not invoke `execve` or `mmap(PROT_EXEC)`.
   - *Where Execution from `/tmp` Is Actually Required:*
     1. Dynamic C/Rust/Fortran compilation during tests (e.g., `cffi` out-of-line compile, `ctypes.CDLL` loading a compiled `.so` from `tmp_path`, or Cython test builds).
     2. Test suites that write temporary shell scripts to `tmp_path` and spawn them via `subprocess.run(["/tmp/.../script.sh"])`.
   - *The Security Risk of Executable `/tmp`:* Mounting `/tmp` without `noexec` allows untrusted code to drop an arbitrary native binary (e.g. an ELF executable) and execute it. While `--cap-drop=ALL` and `--security-opt=no-new-privileges` strip administrative capabilities, executable binaries broaden the attack surface against the shared host Linux kernel (e.g., triggering kernel exploit paths reachable via unprivileged syscalls).
3. **Container Lifecycle Across Application Crashes (Orphan Container Risk):** If the Python application process terminates abruptly (e.g., `SIGKILL`, unhandled crash, host restart) while a validation container is running, Docker daemon continues running the container independently. Simply deleting host workspace directories on restart while a container is still running or holding mounts creates race conditions, filesystem errors, and security hazards. Deterministic container labeling and a strict startup recovery protocol are required.

## Options considered
1. **Permissive Mounting without `noexec` on `/tmp`:**
   - Mount `/tmp` as `--tmpfs /tmp:rw,nosuid,size=128m`.
   - *Drawback:* Leaves `/tmp` executable, permitting execution of dropped native binaries.
2. **Strict Hardening with `noexec` by Default + Isolated Executable Scratch Workspace:**
   - Mount standard `/tmp` strictly with `--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m`.
   - Set `PYTHONDONTWRITEBYTECODE=1` (prevents `.pyc` creation in source trees).
   - Redirect pytest cache via `-o cache_dir=/tmp/.pytest_cache` (works under `noexec` because pytest reads/writes cache as structured JSON/text).
   - If a test suite explicitly requires native binary compilation or executable test scripts, provide a segregated, opt-in build mount: `--tmpfs /workspace/build_bin:rw,exec,nosuid,nodev,size=128m` enabled only via explicit configuration (`allow_dynamic_compilation: true`).
3. **No `tmpfs` (Disk-Backed Host Volume):**
   - *Drawback:* Writes untrusted temp files to host storage; potential disk exhaustion.

## Decision
We choose **Option 2: Strict Hardening with `noexec` on `/tmp` by Default, Pytest Cache Redirection, Segregated Executable Workspace for Native Builds, and Deterministic Container Labeling with Safe Orphan Recovery**.
By default, the container enforces:
- `--network none` (loopback `lo` only; zero external routing or host access).
- `--cap-drop=ALL` (drops all 38+ Linux kernel capabilities).
- `--security-opt=no-new-privileges` (prevents privilege escalation via setuid/setgid).
- `--user $(id -u):$(id -g)` matching host owner (or unprivileged `1000:1000` with dedicated group/ACLs).
- `--read-only` container root filesystem.
- `--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m` (blocks execution of dropped scripts or binaries in temp storage).
- `-e PYTHONDONTWRITEBYTECODE=1` (pure in-memory bytecode compilation).
- Pre-baked testing toolchain (`pytest`, `ruff`, `py_compile`) inside image `dua-sandbox-python:3.11`.
- Clean pytest cache isolation via `-o cache_dir=/tmp/.pytest_cache`.
- Reference repository mounted strictly read-only (`:ro`); mutable working directory mounted `:rw`.
- **Strict Host Workspace Isolation & Permission Policy:**
  Workspace directories on the host are created with mode `0700` (`rwx------`) owned exclusively by the host user. World-writable permissions (`0777`) are strictly forbidden. In desktop CLI environments, running the container as `--user $(id -u):$(id -g)` grants the non-root container full access to `/workspace/working` without any expansion of permissions for other host users (`others: ---`). In multi-tenant environments with static UID 1000, access is granted via dedicated group ownership (`mode 0770`) or POSIX ACLs (`setfacl -m u:1000:rwx`), preserving total host user isolation.
- **Deterministic Container Ownership via Docker Labels:**
  All validation containers must be spawned with standard labels:
  - `--label dev.dua.managed=true`
  - `--label dev.dua.run_id=<run_id>`
  - `--label dev.dua.created_at=<iso_timestamp>`
- **Durable Orphan Recovery & Persistent Cleanup Queue (`pending_cleanups`):**
  To ensure interrupted runs do not evade cleanup after transitioning to `failed` during Docker outages, and to prevent cleanup records from disappearing upon run deletion:
  1. Cleanup obligations are stored in an independent `pending_cleanups` table (`originating_run_id`, `run_id REFERENCES analysis_runs(id) ON DELETE SET NULL`, `container_id`, `workspace_dir`, `quarantine_dir`, `status`, `worker_id`, `lease_token`, `claimed_at`, `lease_expires_at`, `next_attempt_at`, `retry_count`, `error_type`, `last_error`). The non-cascading FK ensures cleanup obligations survive parent run deletion or archival.
  2. Workers acquire claims via atomic lease with unique fencing tokens (`lease_token = secrets.token_hex(16)`, `lease_expires_at = now + 120s`). Long-running operations renew leases via atomic heartbeat verifying `lease_token`, `worker_id`, and `lease_expires_at >= now`. Stale `in_progress` records left by process crashes or SIGKILL are reclaimed to `pending` during startup and periodic sweeps.
  3. **Durable Quarantine-Intent Protocol (SQLite/Filesystem Crash Consistency):** To eliminate the crash-window between database transactions and filesystem operations:
     - **Path Containment & POSIX Advisory Locking:** Worker validates canonical path containment (`os.path.commonpath([real_root, real_path]) == real_root` and `real_path != real_root`) rejecting symlinks (`os.path.islink`) and sibling prefix attacks (`/tmp/ws_evil` matching prefix `/tmp/ws`). It acquires a non-blocking POSIX kernel file lock (`fcntl.flock(LOCK_EX | LOCK_NB)` on `.cleanup_<id>.lock`). The kernel manages lock lifetime, auto-releasing on process termination or `SIGKILL`.
     - **Persist Intent:** Worker persists quarantine intent and unique destination path (`quarantine_dir = f"{workspace_dir}.quarantine_{lease_token}"`) to SQLite (`status = 'quarantine_intent'`) *before* touching the filesystem.
     - **Atomic Rename & Parent Directory Fsync:** Worker atomically renames the directory on disk (`os.replace`). To guarantee true power-loss crash consistency (preventing loss of dirty directory metadata blocks from kernel page cache), it issues `os.fsync(dir_fd)` on the parent directory descriptor before proceeding.
     - **Confirm Quarantine:** Worker confirms quarantine in SQLite (`status = 'quarantined'`).
     - **Secure Recursive Deletion:** Quarantined directory is recursively deleted via `secure_rmtree` using `followlinks=False` to unlink symlinks directly without following them (`O_NOFOLLOW` semantics), preventing symlink traversal attacks.
     - **Commit Completion:** Worker marks completion in SQLite (`status = 'completed'`).
     - **Crash Recovery & Short-Transaction Claiming:** Crash recovery safely handles all crash points: crash before rename resets to `pending` (preserving workspace); crash after rename deletes `quarantine_dir` and completes; anomalous dual existence purges the artifact and resets for retry; absence marks completed. Recovery workers claim stale records via short `BEGIN IMMEDIATE` transactions without holding database locks during slow recursive deletion, and verify mutual exclusion via the OS kernel file lock.
  4. The cleanup state transitions: `pending` -> `in_progress` -> `quarantine_intent` -> `quarantined` -> `completed` (or back to `pending` on transient failure).
  5. Persistent retry scheduling via `next_attempt_at`: retry bounds enforce 5 retries after initial attempt (6 total attempts). Transient Docker outages (`TRANSIENT_DOCKER_UNAVAILABLE`) schedule `next_attempt_at` with exponential backoff without exhausting the retry limit; permanent failures (`PERMANENT_ERROR`) exhaust into terminal `status = 'failed'`, which is not automatically retried and requires explicit operator recovery (`POST /admin/cleanups/{id}/retry`).
  6. Startup recovery and an in-process `asyncio` background task (without Redis/Celery) poll `pending_cleanups` respecting `next_attempt_at`. Docker health recovery reactively resets `next_attempt_at = now` for transient records without bypassing safety checks.
  7. Container ownership is verified via label `dev.dua.managed=true` and matching `dev.dua.run_id=<originating_run_id>`. Docker operations are strictly bounded ($\le 30$s).
  8. The operation is strictly ordered and idempotent: stops and removes the container before deleting the host workspace; safely handles already-stopped or already-removed containers; never deletes a workspace while a container may still be active.
  9. Unrelated containers (lacking `dev.dua.managed=true`) and completed runs are strictly untouched.

## Consequences
- **Positive:** Blocks execution of arbitrary dropped binaries in `/tmp`; standard Python testing works cleanly without bytecode disk pollution or cache failures; zero external network egress prevents data exfiltration; orphaned containers across application restarts or transient Docker outages are durably tracked, retried, and removed before filesystem cleanup.
- **Trade-offs:** Test suites that compile dynamic C-extensions on the fly or spawn shell scripts from `/tmp` will fail under the default `noexec` policy unless executed with the segregated build workspace configuration.
- **Residual Risk:** Docker isolates userland processes but shares the host operating system kernel. While capabilities are dropped and root is read-only, any unpatched zero-day vulnerability in the host Linux kernel reachable via unprivileged system calls represents an unavoidable shared-kernel residual risk.
