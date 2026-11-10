# Architecture standards

## Defaults

1. **Boring by default.** Use mainstream, well-documented tech the team can operate. A new technology
   needs an ADR justifying it against the boring option.
2. **Simplest thing that satisfies the requirements.** Start as a modular monolith. Add queues, caches,
   services or clusters only when a measured requirement demands it.
3. **Data model first.** Decide entities, keys, constraints and ownership before endpoints.
   Put invariants in the schema (NOT NULL, UNIQUE, foreign keys, CHECK), not only in application code.
4. **Clear boundaries.** Each component has one responsibility and a narrow interface. Dependencies
   point inward: domain logic does not import frameworks or I/O.
5. **Design for failure.** For every external call: timeout, retry policy with backoff and jitter only
   for idempotent operations, circuit-breaking where it matters, and a defined degraded behaviour.
6. **Idempotence for side effects.** Anything that can be retried (webhooks, jobs, payments) needs an
   idempotency key or natural uniqueness.
7. **Configuration in the environment,** secrets from a secret store, no environment-specific code.
8. **Observable from day one.** Structured logs with request IDs, basic RED metrics (rate, errors,
   duration), a real health check (checks dependencies, not just "process is up").
9. **Testable.** Pure core, thin I/O shell, injectable clocks and randomness.
10. **Evolvable.** Version external APIs. Make migrations additive first, destructive later.

## Interfaces

- Define request and response shapes and the error model (status codes, stable machine-readable error
  codes) in `architecture.md`.
- Validate at the boundary and reject unknown input. Paginate every list. Bound every payload.
- For CLIs: exit codes, `--help`, machine-readable output option.

## AI and LLM components

- Treat model output as untrusted input: validate against a schema before use.
- Set timeouts, retries, token and cost caps. Log prompts and outputs only if privacy allows.
- Assume prompt injection from any retrieved or user-supplied content. Never let model output choose
  tools, file paths, URLs or commands without an allowlist.
- Keep prompts in versioned files, with evals for behaviour you depend on.
- Give agents least privilege: scoped tools, no ambient credentials, human confirmation for
  destructive or irreversible actions.

## Dependencies

- Prefer the standard library and a few well-maintained packages. Check license, maintenance activity,
  and transitive footprint before adding one. Pin versions and commit the lockfile.
- Each new dependency in a milestone is named in the milestone report with the reason.

## Decision records (ADRs)

Create with `AIDLC adr "Title"`. Write an ADR when: choosing a datastore, framework, auth approach,
sync vs async design, deploying to a new platform, or deviating from these standards. Keep it short:
context, options, decision, consequences. Never rewrite history; supersede an ADR with a new one.

## Project layout

Follow the ecosystem's conventions. Keep tests next to or mirroring source, `docs/` for docs,
`docs/aidlc/` for AI-DLC artifacts. Include a README with install, run, test and deploy commands from
milestone 1.
