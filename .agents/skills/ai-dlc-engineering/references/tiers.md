# Tiers: scale the process to the risk

State the tier in one line at the start ("Tier 2: multi-file service with a database").

## Tier 0: no process
Explanations, snippets, one-line fixes, regexes, questions. Do not invoke this skill's workflow.

## Tier 1: lightweight (no state files, no gates)
Qualifies when all hold: about 3 files or fewer, one sitting, no new external services, no auth, PII,
payments or untrusted input, easy to revert.

Protocol:
1. Plan in 3 to 5 lines: what, where, how you will check it.
2. Ask at most one blocking question, with a default.
3. Implement the smallest change.
4. Run it or its tests. Show the real command and output.
5. Report: what changed, evidence, anything not verified.

## Tier 2: full workflow
Anything beyond tier 1: a project, a service, a multi-file feature, a schema or API change, work that
will span sessions, anything someone else will maintain.

## Tier 3: security-sensitive (tier 2 plus)
Any of: executing untrusted or third-party code, authentication or authorization, secrets handling,
payments, PII or regulated data, multi-tenant isolation, infrastructure or CI/CD changes, agents with
tool or network access, anything exposed to the internet.

Extra bars:
- Threat model needs 5 or more threats, each with a named enforcement point and a test or check
- Recommend `init --strict` so only the human can approve
- Every milestone report includes a security review line
- Dependency audit and secrets scan run at every milestone, not just at G5
- Sandbox, permission and egress controls are enforced technically (container flags, IAM policy,
  network rules), not merely described

## Escalation and de-escalation

- Escalate when scope grows mid-task: stop, say so, and switch tiers with the user's agreement. Do
  not retrofit gates silently. Run `init`, write the artifacts you skipped, and present them.
- De-escalate only on the user's explicit instruction, and record it as a decision.
- When unsure between two tiers, pick the higher one and offer to drop down.

## Examples

| Request | Tier |
|---------|------|
| "Rename these files to lowercase" | 1 |
| "Add a `--json` flag to my CLI" | 1 |
| "Build a REST API for a todo app" | 2 |
| "Add Stripe checkout to my shop" | 3 |
| "Build a tool that runs arbitrary repos' test suites to upgrade their dependencies" | 3 |
| "Explain how JWT works" | 0 |
