# Security policy

## Reporting a vulnerability
Please report suspected vulnerabilities privately through the repository's security advisory
feature (GitHub: Security > Report a vulnerability) rather than a public issue. Include steps to
reproduce and the affected version. Expect an initial response within a week.

## Scope
The scripts in `scripts/` run locally with your user's permissions. In scope: path handling that could
write outside the project, the hook failing open in a way that defeats its documented guarantees, and
integrity-check bypasses in `aidlc.py verify`.

## Known and documented limits (not vulnerabilities)
- The audit log detects casual tampering; it is not proof against someone who rewrites the whole log
- Strict approvals stop an agent without a TTY from self-approving; they do not stop an attacker with
  shell access
- The hook's Bash check is a heuristic
- `scan_secrets.py` is a safety net, not a substitute for dedicated scanners
