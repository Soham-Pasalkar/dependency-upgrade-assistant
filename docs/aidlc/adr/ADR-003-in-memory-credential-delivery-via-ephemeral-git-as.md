# ADR-003: In-Memory Credential Delivery via Ephemeral Authenticated UNIX Domain Socket IPC and Git Hardening

- Status: proposed (submitted for Gate G2 review)
- Date: 2026-11-09

## Context
> **Baseline Prototype Note:** The existing brownfield prototype (`src/services/git_service.py`) dynamically creates a temporary Python script (`dua_askpass_*.py`) in `/tmp` containing `os.environ.get('DUA_GIT_TOKEN')` and marks it executable (`0o700`). This ADR specifies the target production architecture utilizing authenticated UNIX domain socket IPC, kernel peer credentials, and full process-tree ancestry verification, which will be constructed during Phase 2.

When cloning private repositories or authenticating with GitHub over HTTPS, the platform must supply a Personal Access Token (PAT). In the initial prototype, `src/services/git_service.py` dynamically generated a temporary Python script (`dua_askpass_*.py`) in `/tmp` containing `os.environ.get('DUA_GIT_TOKEN')` and marked it executable (`0o700`).

Detailed architectural evaluation identified critical security liabilities:
1. **Dynamic Executable Scripts on Disk:** Creating executable scripts in `/tmp` leaves file artifacts on disk subject to race conditions, unlinked file persistence, and audit warnings.
2. **Environment Inheritance Across Git Process Tree:** In standard POSIX operating systems, child processes inherit their parent's environment variables. Setting `close_fds=True` on `subprocess.Popen` controls file descriptor inheritance only; it does **not** prevent environment-variable inheritance. 
   When Git runs `git clone`, it spawns a tree of subprocesses:
   ```
   FastAPI Parent Process
     └─ git clone / git fetch (Child 1)
          ├─ git-remote-https (Child 2: libcurl HTTP transport)
          │    └─ $GIT_ASKPASS helper (Child 3: invoked by git-remote-https upon HTTP 401)
          ├─ git-credential-* (Global helpers from ~/.gitconfig, e.g. osxkeychain, manager)
          └─ git hooks (e.g., reference-transaction, post-checkout from core.hooksPath)
   ```
   If `DUA_GIT_TOKEN` is passed as an environment variable to `git`, every subprocess in this tree inherits `DUA_GIT_TOKEN` in its environment (`/proc/<pid>/environ`). Furthermore, third-party credential helpers could capture the token and persist it into the developer's OS keychain.
3. **Same-UID Socket Racing and Nonce Exposure Realities:**
   - A private directory with mode `0700` protects against other OS user accounts, but does not prevent other processes running under the same user UID from discovering the socket path.
   - If an authorization nonce `DUA_AUTH_NONCE` is passed to the helper via environment variables, any process running under the same UID on Linux that has access to `/proc/<child_pid>/environ` could inspect the process environment during the brief clone window, extract both the socket path and the nonce, and attempt to connect to the socket.
   - Therefore, the nonce alone is **not** sufficient protection against same-UID attackers on platforms with transparent environment inspection (like Linux `/proc`). Complete process-ancestry verification is required.

## Options considered
1. **Embed Token in Clone URL (`https://token@github.com/...`):**
   - *Drawback:* Tokens appear in `git remote -v`, process tables (`ps aux`), shell histories, and error logs. Strictly prohibited.
2. **Environment Variable Delivery to Git (`DUA_GIT_TOKEN` in Git env):**
   - *Drawback:* Inherited by all Git child processes, remote helpers, user credential helpers, and hooks.
3. **Authenticated Ephemeral UNIX Domain Socket IPC with Kernel Peer Credentials + Git Hardening:**
   - A static, immutable helper script (`src/services/askpass_helper.py`) is bundled in the source tree (zero dynamically generated files).
   - Python `GitService` creates an ephemeral local UNIX domain socket in a private directory with mode `0700` (`~/.dua/ipc/ask_<uuid>.sock`).
   - The token string itself is **never** passed in Git's environment.
   - Git is spawned with `GIT_ASKPASS=/path/to/static_askpass_helper.py`, `DUA_AUTH_SOCK=/path/to/ask_<uuid>.sock`, and a 256-bit random nonce `DUA_AUTH_NONCE`.
   - **Kernel Peer Credential Verification & Process-Tree Ancestry:**
     * *Linux:* The listener calls `getsockopt(SOL_SOCKET, SO_PEERCRED, ...)` to obtain kernel-verified `struct ucred` (`pid`, `uid`, `gid`). It asserts `ucred.uid == os.getuid()`. It then verifies full process-tree ancestry: walks `/proc/<ucred.pid>/stat` up through parent PIDs (`ppid`, field 4) to assert that the connecting process is a descendant of the expected `git` process spawned by Python. Handles PID reuse (verifies `starttime`, field 22), malformed `/proc` content, and process termination (fails closed immediately on any missing entry or unexpected ancestry).
     * *macOS / BSD:* The listener calls `libc.getpeereid(fd, &euid, &egid)` to verify kernel-enforced `euid == os.getuid()`. On macOS, `/proc` does not exist, and Mach task security restricts same-UID environment inspection. To verify PID on macOS, the listener calls `getsockopt(0, 0x002, 4)` (`LOCAL_PEERPID` under `SOL_LOCAL=0`), unmasks the peer PID, and walks parent PIDs via `libproc` (`proc_pidinfo` with `PROC_PIDTBSDINFO`). If `LOCAL_PEERPID` fails or is unsupported on a legacy environment, the system fails closed rather than silently downgrading.
   - **Single-Use Semantics & Race Closure:** The listener sets `backlog=1`, accepts strictly ONE connection, immediately unlinks the socket file, and times out after 5.0 seconds. A strict `try...finally` block guarantees cleanup under all outcomes.
   - **Hardened Git Flags:**
     `git -c credential.helper= -c core.hooksPath=/dev/null clone ...`
     disables all workstation credential helpers and global hooks.
   - Diagnostic variables (`GIT_TRACE`, `GIT_TRANSFER_TRACE`, `GIT_CURL_VERBOSE`) are explicitly stripped.

## Decision
We choose **Option 3: Authenticated Ephemeral UNIX Domain Socket IPC with Kernel Peer Credentials + Git Hardening**.
The token is never placed in the environment of Git or its subprocesses. The token string exists only in Python parent memory, is queried across an authenticated local UNIX socket with kernel peer verification, ancestry walking, and single-use accept-and-unlink semantics, and is delivered strictly to the `askpass_helper` process.

## Consequences & Security Scope
- **Accurate Exposure Scope:** The `askpass_helper` writes the token to `stdout`, which Git consumes via its internal pipe to construct the HTTP `Authorization: Basic ...` header. Therefore, Git's HTTP transport (`git-remote-https` via `libcurl`) **necessarily receives the credential** to authenticate to GitHub over TLS.
- **Actual Security Guarantees:**
  1. Zero token persistence to disk.
  2. Zero token presence in process command-line arguments or remote URLs.
  3. Zero token presence in the process environment of `git`, `git-remote-https`, credential helpers, or hooks.
  4. Complete suppression of workstation credential stores (`-c credential.helper=`) and global hooks (`-c core.hooksPath=/dev/null`).
  5. Same-user process race attacks on the socket are blocked by kernel peer credential verification (`SO_PEERCRED` on Linux, `LOCAL_PEERPID` + `libproc` on macOS) combined with full process ancestry validation, single-use unlinking, and nonce validation.
  6. Zero logging of credentials.
- **Trade-offs:** Requires a lightweight local UNIX socket server thread in `GitService` with a 5-second timeout during clone. On platforms without UNIX domain sockets (native Windows), falls back to a loopback TCP socket bound to `127.0.0.1` with the same single-use random authorization nonce.
