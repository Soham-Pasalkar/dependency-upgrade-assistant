# Release checklist

- [ ] Full test suite green on a clean checkout
- [ ] Linter and type checks clean
- [ ] Dependency audit run; no unaddressed high/critical findings
- [ ] Secrets scan clean (working tree and history)
- [ ] Threat-model mitigations each verified by a test or a documented manual check
- [ ] NFRs measured against stated thresholds
- [ ] Migrations tested forward and backward on realistic data
- [ ] Config and environment variables documented; no defaults that are unsafe in production
- [ ] Logging, metrics and health checks verified
- [ ] Backup, rollback and runbook written and rehearsed
- [ ] README: install, run, test, deploy
- [ ] Git history coherent, no committed secrets or large binaries
- [ ] Completion report written; "Not verified" is honest
