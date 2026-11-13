# M5 Sandboxed Validation Threat Model & Security Specification

## 1. Scope & Objective

This threat model analyzes the security boundaries, attack vectors, and defense-in-depth mitigations of the **Sandboxed Validation Subsystem** in Dependency Upgrade Assistant 2.0.

The primary objective is to execute untrusted third-party code (dependencies, upgrade candidates, setup scripts, and test suites) without endangering:
1. The host operating system, filesystem, and kernel integrity.
2. Sensitive host credentials (API keys, SSH keys, cloud tokens, environment variables).
3. Network infrastructure and local loopback services.
4. Host system availability (protection against resource exhaustion and denial of service).

---

## 2. Threat Actors & Asset Classification

### 2.1 Threat Actors
- **Malicious Dependency Author (Supply Chain Adversary):** Introduces Trojanized code into dependency packages (e.g. malicious `setup.py`, post-install hooks, or test harness payloads) attempting data exfiltration, cryptomining, or persistence.
- **Compromised Target Repository:** A repository with malicious test suites designed to exploit developer machines or CI/CD runners upon execution.
- **Malicious Internal / Multi-Tenant Actor:** An adversary attempting path traversal, symlink spoofing, or container breakout to access other sessions or host assets.

### 2.2 Critical Assets Under Protection
- **Host Credentials:** SSH keys (`~/.ssh`), cloud profiles (`~/.aws`, `~/.gcp`), git tokens (`GIT_ASKPASS`), environment secrets (`.env`).
- **Host Docker Daemon Socket:** `/var/run/docker.sock` (equivalent to root access on the host).
- **Host Filesystem:** Read-only protection for host files; immutable isolation of workspace source.
- **Host Availability:** CPU, memory, and process table availability against fork bombs and memory exhaustion.

---

## 3. Attack Vectors & Mitigations

### 3.1 Arbitrary Code Execution via Package Installation / Tests
- **Attack Vector:** An updated package or test harness attempts to execute shell commands, install rootkits, or spawn reverse shells.
- **Mitigation:**
  - Mandatory container execution as an unprivileged non-root user matching the host execution identity (`--user $(id -u):$(id -g)` on non-root POSIX systems, falling back to `1000:1000` when running as root). Root execution inside the container is strictly forbidden.
  - Total capability dropping (`--cap-drop=ALL`).
  - Strict prohibition of privilege escalation (`--security-opt=no-new-privileges:true`).
  - Root filesystem mounted read-only (`--read-only`), preventing writing to system binaries or libraries.

### 3.2 Network Exfiltration & Command-and-Control (C2)
- **Attack Vector:** Malicious code contacts an external C2 server, exfiltrates environment secrets, or attempts internal network scanning (e.g. probing AWS metadata service at `169.254.169.254` or local host ports). An attacker may also submit an API payload requesting `network_enabled=True`.
- **Mitigation:**
  - Container spawned strictly with `--network none` during test execution.
  - Server-enforced policy: `network_enabled` field is completely removed from the public API schema; Pydantic request validator rejects any attempt to supply `network_enabled` with HTTP 422.
  - Decoupled staging (Stage A): in M5, package staging operates strictly offline (`status: NOT_REQUIRED`), with dynamic network-based acquisition of 3rd-party wheels deferred to Phase 3. Registry validation logic (`is_safe_registry_url()`) enforces SSRF protection rejecting non-HTTPS URLs, loopback (`127.0.0.1`, `localhost`), RFC 1918 private IP subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), and link-local cloud metadata addresses (`169.254.169.254`).
  - Zero outbound or inbound socket connections permitted during test execution (Stage B).
  - Explicit prohibition of `--net=host` or `network_mode="host"`.

### 3.3 Docker Daemon Compromise (Docker-in-Docker / Socket Mount)
- **Attack Vector:** Code inside the container locates `/var/run/docker.sock` and issues API calls to spawn privileged root containers on the host.
- **Mitigation:**
  - Strict code-level assertion rejecting any mount containing `docker.sock`.
  - No socket pass-through or privileged volume mounts under any circumstances.
  - Container configuration generator strictly rejects privileged mode (`--privileged`).
  - Rejection of host namespaces (`--pid=host`, `--ipc=host`, `--uts=host`, `--userns=host`).

### 3.4 Filesystem Traversal, Arbitrary Host Paths & Symlink Escape
- **Attack Vector:** An adversary supplies arbitrary host paths (e.g. `/`, `/etc`, `C:\Windows`) in validation requests, or uses path traversals (`../../`), sibling prefix confusion (`/root_evil` vs `/root`), or symlinks to access or overwrite host files.
- **Mitigation:**
  - Strict host boundary validation (`src/sandbox/workspace_boundary.py`):
    - `workspace_path` parameter removed from public `SandboxExecutionRequest`; only relative subpaths validated against `settings.resolved_workspace_root` are permitted.
    - Path traversal sequences (`../`, `..\`), root paths (`/`), and Windows drive letters (`C:`) are rejected.
    - Sibling prefix confusion is eliminated by enforcing directory containment with explicit trailing separator checks (`os.path.commonpath`).
  - Workspace snapshot isolation: files are replicated into an ephemeral directory under `tempfile.TemporaryDirectory()`. Snapshot root and subdirectories are strictly configured with mode `0700` (`rwx------`), forbidding world-writable (`0777`) permissions (`mode & 0o002 == 0`) and denying access to all other host users (`mode & 0o007 == 0`). Copied files strip group and world-write bits.
  - Symlinks in the source workspace are evaluated via `os.path.realpath`. Any symlink whose target escapes the workspace boundary is rejected with `SecurityException`.
  - Special files (FIFOs, named pipes, Unix domain sockets) are rejected via `stat.S_ISFIFO` and `stat.S_ISSOCK`, failing closed with `SnapshotCreationError` to prevent hanging copy routines.
  - Sensitive files (`.env`, `.env.*`, `.git`, `.pem`, `.key`, `id_rsa`, `.aws`, `.gcp`, `.azure`, `.kube`, `.docker`, `.npmrc`, `.pypirc`) are scrubbed during snapshot creation.
  - Safe configuration dotfiles (`.pytest.ini`, `.ruff.toml`) are explicitly preserved to maintain valid test runner discovery.
  - Snapshot manifest (`SnapshotManifest`) tracks copied files and excluded entries.

### 3.5 Resource Exhaustion & Denial of Service (DoS)
- **Attack Vector:** An untrusted test script executes an infinite loop (`while True:`), allocates gigabytes of RAM, spawns thousands of threads/processes, or generates infinite stdout output to exhaust host memory.
- **Mitigation:**
  - Server-side sanitized resource ceilings (`sanitize_resource_limits`):
    - Hard maximum RAM ceiling: 1024 MB.
    - Hard maximum CPU quota: 2.0 cores.
    - Hard process table cap: 128 PIDs (neutralizes fork bombs).
    - Hard execution timeout: 120 seconds.
  - Process-level streaming buffers (`BoundedStreamCapture`):
    - Non-blocking background threads stream stdout and stderr separately.
    - Enforces a 512 KB memory limit; once reached, logs are truncated with an informative notice while the stream continues to be drained from the OS pipe, preventing pipe deadlocks.
    - Strips ANSI escape sequences to prevent log injection in web clients.

### 3.6 Docker Unavailability & Host Fallback Attack
- **Attack Vector:** If the Docker daemon is offline or fails, an insecure application might fall back to executing test commands on the host machine using `subprocess.run()`.
- **Mitigation:**
  - **Fail-Closed Security Policy:** If the Docker CLI is missing or the daemon is unreachable, the runner immediately returns `NOT_RUNNABLE`.
  - Untrusted repository code or test commands are **strictly never executed on the host system**.

### 3.7 False-Positive Test Reporting (Zero Tests Found)
- **Attack Vector:** A malicious or empty package contains no tests, or an npm package has a default placeholder test script (`"echo \"Error: no test specified\" && exit 1"`). Without explicit discovery, this could either report false `PASSED` or confuse users.
- **Mitigation:**
  - Explicit test discovery subsystem (`TestDiscoveryManager`):
    - Python: discovers `test_*.py`, `*_test.py`, `tests/` directories, and `pytest.ini`.
    - Node.js: inspects `package.json` for test scripts; detects default npm placeholders.
    - If zero tests are present, returns `TestDiscoveryStatus.NO_TESTS_FOUND` and reports validation status `NO_TESTS_FOUND` rather than false `PASSED`.

### 3.8 Base Image Supply-Chain Poisoning & Pinning
- **Attack Vector:** Referencing mutable Docker image tags (e.g. `python:3.11-slim`) allows malicious or compromised upstream registries to inject backdoor layers.
- **Mitigation:**
  - All allowlisted images are pinned to immutable multi-arch SHA-256 digests in `src/sandbox/image_catalog.py`:
    - `python:3.11-slim` -> `sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535`
    - `python:3.12-slim` -> `sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`
    - `node:20-slim` -> `sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0`
    - `node:22-slim` -> `sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d`
  - Invocations use `image@sha256:...`.
  - The resolved image digest is recorded in `ValidationReport.image_digest`.

### 3.9 Lifecycle Race Conditions & Zombie Containers
- **Attack Vector:** Concurrent validation requests or asynchronous cancellations leave abandoned containers running indefinitely or allow late completion callbacks to overwrite a `CANCELLED` status.
- **Mitigation:**
  - Atomic tracking of container names and process handles upon startup (`on_process_started` callback).
  - Cancellation (`cancel_execution()`) immediately issues `docker rm -f <container_name>` and terminates the host subprocess.
  - State machine transitions prevent terminal `CANCELLED` status from being overwritten by post-execution callbacks.

---

## 4. Trust Boundary & TOCTOU Considerations

### 4.1 Documented Trust Boundary
```
[ Host User / Antigravity Backend (Trust Level: High) ]
                         │
      ───────────────────┴─────────────────── Workspace Boundary
                         │
[ Ephemeral Workspace Snapshot (Trust Level: Medium - Sanitized) ]
                         │
      ───────────────────┴─────────────────── Container Boundary
                         │
[ Isolated Container: --network none, non-root, cap-drop (Trust Level: Zero) ]
```

### 4.2 Time-Of-Check to Time-Of-Use (TOCTOU) Disclosure
The current workspace mutation and snapshot isolation security model assumes a single-tenant or container-isolated execution environment where concurrent local filesystem actors do not have concurrent write access to the workspace directory structure.

Mitigations against sub-millisecond local symlink-swap race conditions (TOCTOU) between resolution and file copying rely on workspace boundary containment, parent directory verification, and filesystem permissions rather than kernel-level `openat(O_NOFOLLOW)` descriptor trees. In production multi-tenant environments, workspaces must run in dedicated ephemeral virtual machines or separate namespaces.

---

## 5. Security Checklist & Verification Matrix

| Vulnerability Category | Mitigation Mechanism | Verification Test |
|:---|:---|:---|
| Privilege Escalation | Non-root `--user`, `--cap-drop=ALL`, `no-new-privileges` | `test_container_config_enforces_all_mandatory_security_flags` |
| Root User Prohibited | Explicit rejection of UID 0 / root container execution | `test_container_config_rejects_root_user_execution` |
| World-Writable Permissions | `0700` snapshot permissions, forbidding world-writable `0777` | `test_snapshot_directories_forbid_world_writable_and_other_permissions` |
| Network Override Denial | `--network none` enforced, `network_enabled` rejected in request | `test_sandbox_execution_request_rejects_network_enabled` |
| SSRF in Staging | Registry URLs restricted to public HTTPS endpoints | `test_staging_manager_safe_registry_urls` |
| Arbitrary Host Path / Root | Rejection of `/`, `/etc`, Windows `C:`, traversals | `test_workspace_boundary_rejects_root_and_etc_traversal` |
| Sibling Directory Confusion | Commonpath prefix checking with directory boundary | `test_workspace_boundary_rejects_sibling_prefix_directory` |
| External Symlink Escape | Rejection / exclusion of escaping symlinks | `test_workspace_boundary_rejects_symlink_escape` |
| Special File / FIFO Hangs | Reject FIFOs and unix sockets with SnapshotCreationError | `test_snapshot_manager_rejects_fifo_special_files` |
| Host Root Compromise | Root filesystem mounted `--read-only` | `test_container_config_enforces_read_only_root_fs` |
| Daemon Hijack | Strict assertion rejecting `docker.sock` | `test_container_config_rejects_docker_socket_mount` |
| Image Tampering / Substitution | Pinned immutable SHA-256 digests for all approved images | `test_image_catalog_pinned_digests` |
| Secret Leaks | Exclusion of `.env*`, `.pem`, `.git`, `.key`, cloud configs | `test_snapshot_excludes_sensitive_files_and_preserves_safe_dotfiles` |
| Safe Dotfile Retention | Retain `.pytest.ini`, `.ruff.toml` for test discovery | `test_snapshot_excludes_sensitive_files_and_preserves_safe_dotfiles` |
| Fork Bomb / DoS | Hard ceilings: `--pids-limit=128`, `--memory=1024m`, `--cpus=2.0` | `test_container_config_sanitizes_resource_limits` |
| Infinite Loop Hang | Subprocess timeout with `docker rm -f` cleanup | `test_runner_timeout_terminates_process_and_container` |
| Host Execution Fallback | Fail-closed `NOT_RUNNABLE` when Docker offline | `test_runner_fails_closed_when_docker_binary_missing` |
| Memory Exhaustion via Logs | Bounded buffer (512 KB cap) with streaming reader & drain | `test_runner_streaming_log_capture_bounds_output` |
| ANSI Log Injection | Stripping ANSI terminal control codes from UI output | `test_runner_strips_ansi_escape_sequences` |
| False Positive Test Reports | Explicit test discovery; reports `NO_TESTS_FOUND` | `test_discovery_python_and_node_edge_cases` |
| Zombie Container / Race | Real-time atomic cancellation (`docker rm -f` + proc kill) | `test_validation_real_time_cancellation_and_repeated_cancel` |
