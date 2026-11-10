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
  - Mandatory container execution as an unprivileged user (`--user 1000:1000`).
  - Total capability dropping (`--cap-drop=ALL`).
  - Strict prohibition of privilege escalation (`--security-opt=no-new-privileges:true`).
  - Root filesystem mounted read-only (`--read-only`), preventing writing to system binaries or libraries.

### 3.2 Network Exfiltration & Command-and-Control (C2)
- **Attack Vector:** Malicious code contacts an external C2 server, exfiltrates environment secrets, or attempts internal network scanning (e.g. probing AWS metadata service at `169.254.169.254` or local host ports).
- **Mitigation:**
  - Container spawned with `--network none` by default during test execution.
  - Zero outbound or inbound socket connections permitted.
  - No access to local host loopback interfaces (`127.0.0.1` on host).
  - Explicit prohibition of `--net=host` or `network_mode="host"`.

### 3.3 Docker Daemon Compromise (Docker-in-Docker / Socket Mount)
- **Attack Vector:** Code inside the container locates `/var/run/docker.sock` and issues API calls to spawn privileged root containers on the host.
- **Mitigation:**
  - Strict code-level assertion rejecting any mount containing `docker.sock`.
  - No socket pass-through or privileged volume mounts under any circumstances.
  - Container configuration generator strictly rejects privileged mode (`--privileged`).

### 3.4 Filesystem Traversal & Symlink Escape
- **Attack Vector:** Untrusted repositories create symlinks pointing to sensitive host paths (e.g. `../../../../etc/passwd` or `~/.ssh/id_rsa`) to exfiltrate or overwrite host files.
- **Mitigation:**
  - Workspace snapshot isolation: files are copied into an ephemeral directory under `tempfile.TemporaryDirectory()`.
  - Symlinks in the source workspace are evaluated via `os.path.realpath`. Any symlink pointing outside the workspace root is rejected or excluded.
  - Sensitive files (`.env`, `.git`, `.pem`, `.key`, `id_rsa`, `id_ed25519`) are excluded during snapshot creation.
  - Host files are never mounted into containers read-write; only isolated temporary directories or memory tmpfs mounts are writable.

### 3.5 Resource Exhaustion & Denial of Service (DoS)
- **Attack Vector:** An untrusted test script executes an infinite loop (`while True:`), allocates gigabytes of RAM, or triggers a fork bomb (`:(){ :|:& };:`).
- **Mitigation:**
  - Memory ceiling: `--memory=512m` (configurable with hard ceiling).
  - CPU quota: `--cpus=1.0`.
  - Process table cap: `--pids-limit=64` (neutralizes fork bombs).
  - Hard execution timeout: 60-second subprocess timeout. Upon expiration, container receives `SIGTERM`, followed by `SIGKILL` and `docker rm -f`.
  - Bounded output streaming: `stdout` and `stderr` buffers capped at 512 KB to prevent backend process memory exhaustion.

### 3.6 Docker Unavailability & Host Fallback Attack
- **Attack Vector:** If the Docker daemon is offline or fails, an insecure application might fall back to executing test commands on the host machine using `subprocess.run()`.
- **Mitigation:**
  - **Fail-Closed Security Policy:** If the Docker CLI is missing or the daemon is unreachable, the runner immediately returns `NOT_RUNNABLE`.
  - Untrusted repository code or test commands are **strictly never executed on the host system**.

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
| Privilege Escalation | `--user 1000:1000`, `--cap-drop=ALL`, `no-new-privileges` | `test_sandbox_security_flags` |
| Network Exfiltration | `--network none` enforced by default | `test_network_disabled_by_default` |
| Host Root Compromise | Root filesystem mounted `--read-only` | `test_readonly_root_fs` |
| Daemon Hijack | Strict assertion rejecting `docker.sock` | `test_reject_docker_socket_mount` |
| Malicious Base Images | Allowlisted image catalog only | `test_reject_arbitrary_images` |
| Secret Leaks | Exclusion of `.env`, `.pem`, `.git`, `.key` in snapshots | `test_snapshot_excludes_secrets` |
| Fork Bomb / DoS | `--pids-limit=64`, `--memory=512m`, `--cpus=1.0` | `test_resource_limits_enforced` |
| Infinite Loop Hang | Subprocess timeout with `docker rm -f` cleanup | `test_execution_timeout_kills_container` |
| Host Execution Fallback | Fail-closed `NOT_RUNNABLE` when Docker offline | `test_fails_closed_when_docker_offline` |
| Memory Exhaustion via Logs | Bounded buffer (512 KB cap) | `test_bounded_log_capture` |
