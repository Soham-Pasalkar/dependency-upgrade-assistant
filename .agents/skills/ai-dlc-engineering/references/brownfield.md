# Brownfield: working in an existing codebase

Use `init --mode brownfield`. The risk here is breaking what already works and misreading conventions.

## M0: Intake and safety net (always the first milestone)

Goal: understand the system and establish a baseline before changing anything.

1. **Map the repo.** Entry points, build and run commands, test setup, CI, deployment, dependencies,
   configuration, data stores, external integrations. Write `docs/aidlc/architecture.md` as *as-is*
   plus the delta you intend
2. **Learn conventions.** Style, naming, error handling, layering, test patterns, commit style. Match them
3. **Run the baseline.** Install, build, run the existing tests, linter and type checker. Record the
   exact results, including pre-existing failures. These become "known failures", not yours to fix
   silently and not yours to hide
4. **Characterization tests.** Where behaviour you will touch is untested, write tests that pin down
   what the code does today (even if odd) so regressions are visible
5. **Find seams.** Where can the change be introduced with the smallest blast radius: new module,
   adapter, feature flag, strangler wrapper?
6. **Threat-model the delta,** not the whole world: what new inputs, privileges or boundaries does the
   change add? Note existing risks you observe as findings for the user, without fixing them unasked
7. **Plan milestones** as small reversible steps. M0 is the baseline and tests; later milestones layer
   the change behind a seam

## Rules

- **Minimal diff.** Do not reformat, rename or "clean up" unrelated code. Offer cleanup as a separate
  milestone
- **Preserve public behaviour** (APIs, CLI flags, file formats, DB schema) unless a requirement says
  otherwise. Breaking changes need an ADR and user approval
- **Migrations:** additive first (add column, backfill, switch reads, then remove), reversible, tested
  on realistic data. Never run destructive migrations against real data without explicit approval
- **Feature flags or branches** for risky changes, with a documented rollback
- **Respect ownership.** Do not edit generated files, vendored code, lockfiles beyond need, or CI
  config without saying so
- **Do not trust docs over code,** and do not trust code comments over tests. When they disagree, tell
  the user
- Existing failing tests: report them, do not weaken or delete them

## Large legacy systems
Pick one vertical slice for the first real milestone and prove the workflow there. Avoid "rewrite"
milestones. Prefer incremental strangler patterns with parity tests against the old behaviour.
