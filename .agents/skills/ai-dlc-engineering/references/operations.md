# Phase 3: Operations

Goal: prove the whole system works and is safe to hand over or ship. Entered when every milestone is
done. Gate: G5.

## Checklist (`templates/release-checklist.md` is the copy-paste form)

1. **Full regression** on a clean checkout: install, build, run the entire test suite. Record command
   and result.
2. **Security validation:**
   - `scan_secrets.py . --history` plus a dedicated scanner (gitleaks, trufflehog) if available
   - Dependency audit for the ecosystem; triage every high or critical finding
   - Walk the threat model: each mitigation maps to a passing test or a documented manual check
   - Review configuration for unsafe defaults (debug mode, permissive CORS, default credentials)
3. **Performance and capacity:** measure against each NFR with a repeatable script. State the
   environment. Report misses plainly
4. **Reliability:** failure-mode checks (dependency down, timeouts, bad input), health check behaviour,
   graceful shutdown, migration forward and backward on realistic data
5. **Observability:** logs are structured and free of secrets, key metrics exist, alerts or at least
   alert recommendations are written
6. **Documentation:** README (install, configure, run, test, deploy), configuration reference,
   architecture summary, runbook (start, stop, rollback, common failures), API docs
7. **Release mechanics:** version, changelog, build artifact reproducible, rollback plan rehearsed,
   deployment steps written. Do not deploy on your own unless the user asked you to
8. **Git review:** history coherent, milestone-aligned, no secrets or large binaries
9. **Completion report** (`completion-report.md`)

## Completion report honesty rules

- **Not verified** is mandatory and specific: what you did not test, could not test, or assumed
- **Residual risks** each have an owner suggestion and a follow-up
- Separate "verified by running X" from "believed from reading the code"
- Recommendation is one of: ship, ship with conditions (list them), do not ship (say why)

## Gate G5

`AIDLC gate present G5` requires all milestones done and a valid completion report. Present it, wait
for the user's approval. After G5 the project is `released`. Further work starts a new intent:
`milestone add --replan`, which reopens G3 and G5.

## Post-release
Suggest what to watch in the first days: error rate, latency, resource use, cost, security logs. Offer a
follow-up milestone for monitoring gaps you found.
