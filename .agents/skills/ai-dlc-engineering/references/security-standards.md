# Security standards (apply in every milestone)

Aligned with common practice (OWASP ASVS and Top 10 themes). Treat as a floor, not a ceiling.

## Secrets
- Never in source, tests, fixtures, logs, screenshots, prompts or error messages
- Load from environment or a secret manager; commit `.env.example` with fake values only
- Add `.env*` (except the example) to `.gitignore` in milestone 1
- Run `scripts/scan_secrets.py` before each submit. If a real secret was ever committed, tell the user
  to rotate it. Deleting the commit is not enough

## Authentication and authorization
- Authenticate every non-public endpoint. Authorize **every object access on the server** (IDOR/BOLA):
  check that this user may touch this record, not just that they are logged in
- Deny by default. Centralize policy checks; do not scatter ad hoc conditionals
- Passwords: argon2id or bcrypt via a vetted library. Never invent crypto, never store plaintext, never use MD5/SHA1 for passwords
- Tokens: short-lived, scoped, revocable. Sessions: HttpOnly, Secure, SameSite cookies. CSRF protection for cookie auth
- Rate-limit login and expensive endpoints

## Input and output
- Validate at every trust boundary with a schema. Allowlists beat denylists. Bound sizes and counts
- Parameterized queries only. No string-built SQL, shell commands, or templates from user input
- Encode output for its context (HTML, URL, shell, SQL). Escape by default in templates
- File uploads: size and type limits, random server-side names, no execution, store outside web root
- SSRF: user-supplied URLs go through an allowlist and block internal and metadata IP ranges
- Avoid unsafe deserialization (pickle, unsafe YAML loaders) of untrusted data

## Data protection
- TLS everywhere. Encrypt sensitive data at rest where the platform allows
- Collect the minimum personal data. Define retention and deletion
- Do not log secrets, tokens, full card numbers or personal data. Mask identifiers when practical

## Errors and logging
- Generic messages to clients, details server-side with a request ID
- Log security events (auth failures, permission denials, admin actions) with enough context to investigate
- Fail closed: if an authorization check errors, deny

## Dependencies and supply chain
- Pin versions, commit lockfiles, use a registry you trust
- Audit at every milestone: `pip-audit`, `npm audit`, `cargo audit`, `govulncheck`, or the ecosystem equivalent
- Review new dependencies: need, maintenance, license, install scripts, transitive size
- Do not run `curl | sh` or untrusted install scripts outside a sandbox

## Containers, infrastructure and CI
- Minimal base images, non-root user, no secrets baked into layers, healthchecks
- Least-privilege IAM and database roles; separate credentials per environment
- CI secrets are not exposed to untrusted pull-request builds
- Infrastructure as code reviewed like application code

## Executing untrusted code
Follow the control list in `threat-modeling.md`. A sandbox that is only described in Markdown is not a
control: it must be enforced in code or infrastructure and proven by a test.

## Per-milestone security check (before `milestone submit`)
- [ ] New inputs validated; new endpoints authenticated and authorized
- [ ] No secrets added (scan run); `.gitignore` covers local env files
- [ ] New dependencies audited and justified
- [ ] Errors and logs reviewed for leaks
- [ ] Any new trust boundary added to the threat model
- [ ] Mitigations introduced in this milestone have tests
