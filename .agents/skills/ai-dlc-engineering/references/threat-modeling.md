# Threat modeling

Light, repeatable, and tied to controls you can test. Output: `docs/aidlc/threat-model.md`.

## Process

1. **List assets**: what an attacker wants or what hurts if lost (user data, credentials, money,
   compute, reputation, model weights, integrity of builds).
2. **Draw trust boundaries**: where data or control crosses between parties with different privileges
   (browser to API, API to database, service to third party, host to sandbox, agent to tool, CI to prod).
3. **For each boundary, enumerate threats** with STRIDE, plus the AI and supply-chain list below.
4. **Rate** each: likelihood (low, med, high) times impact (low, med, high). Focus on the top ones.
5. **Write the mitigation and where it is enforced** (code path, config, infrastructure), and how it
   is tested. "We will be careful" is not a mitigation.
6. **State residual risk** honestly. Accepting risk is the user's decision at G2.

## STRIDE prompts

| Letter | Threat | Questions |
|--------|--------|-----------|
| S | Spoofing | Can someone pretend to be a user, service or the update server? |
| T | Tampering | Can data, config, code or dependencies be modified in transit or at rest? |
| R | Repudiation | Could a party deny an action? Do audit logs exist and resist tampering? |
| I | Information disclosure | Can data leak via errors, logs, side channels, IDOR, caches? |
| D | Denial of service | Unbounded input, expensive queries, resource exhaustion, retry storms? |
| E | Elevation of privilege | Missing authorization checks, confused deputy, mass assignment, sandbox escape? |

## Additional checklists

**Web and API:** IDOR/BOLA (object-level authorization on every access), broken function-level
authorization, mass assignment, SSRF, open redirects, CSRF, injection (SQL, command, template), unsafe
deserialization, file upload abuse, weak session handling, missing rate limits, verbose errors.

**LLM and agent systems:** prompt injection (direct and via retrieved content), insecure output
handling, excessive agency (tools with too much power), data exfiltration through tool calls or
rendered links, secrets in prompts or logs, cost abuse, poisoned retrieval data, unreviewed
model-written code run with real privileges.

**Supply chain and CI:** typosquatted or hijacked packages, malicious install scripts, unpinned
dependencies, compromised build steps, secrets exposed to untrusted PR builds, unsigned artifacts.

## Executing untrusted code: required controls

If the system runs code from outside the trust boundary (third-party repos, user scripts, package
install scripts), the sandbox is the product's most important control. Enforce technically:

- Fresh, ephemeral container or microVM per job; destroyed after
- Non-root user, all capabilities dropped, `no-new-privileges`, default seccomp profile or stricter
- Read-only root filesystem, a small writable tmpfs or scratch volume only
- No host mounts, no Docker socket, no cloud metadata endpoint access
- Network: none by default, or an explicit egress allowlist through a proxy (package registry only)
- CPU, memory, PID, disk and wall-clock limits; output size caps
- No secrets, tokens or ambient credentials inside; credentials, if any, are short-lived and scoped
- Results returned as data via a narrow channel, then validated before use
- A test that proves each control (for example, the sandbox cannot reach the network or read the host)

## Writing threats

Format a row: `T-1 | boundary | what can go wrong | STRIDE letter | mitigation and where enforced | residual`.
Good: `Attacker-supplied repo install script reads host env | host/sandbox | E, I | container has no
env passthrough, non-root, no mounts; enforced in sandbox/runner.py, tested by test_sandbox_isolation |
low`.
Bad: `Security issues | app | - | Be careful | -`.

## Keep it alive

Revisit the model when a milestone adds a new boundary, a new data type, a new integration or a new
privilege. Update the file and note the change in the milestone report. Structural changes reopen G2
(`gate reset G2`).
