# Phase 2: Construction

Goal: deliver the plan one verified milestone at a time. Each milestone is a bolt: small, demonstrable,
and proven.

## Preconditions (the CLI enforces these)
G1, G2 and G3 are approved. No other milestone is open. Dependencies are done.

## The milestone loop

1. **Start.** `AIDLC milestone start M2`. This scaffolds `docs/aidlc/reports/M2.md` with the criteria
   pre-filled. Read the criteria and the relevant threat-model rows again.
2. **Plan the change** in a few lines: files to touch, data changes, tests to add, anything risky. If
   the plan contradicts the architecture, stop: that is a design change, not an implementation detail.
3. **Test first where it pays.** For new behaviour and bug fixes, write the failing test, watch it
   fail for the right reason, then implement.
4. **Implement the smallest change** that satisfies the criteria. No drive-by refactors, no features
   from later milestones. Note good ideas with `AIDLC note add`.
5. **Run everything:** the full test suite, linter, type checker, and `scan_secrets.py`. Fix failures
   at the root cause.
6. **Log evidence** for each run: `AIDLC milestone evidence M2 --cmd "pytest -q" --result "58 passed"`.
7. **Verify each criterion** with specific proof: `AIDLC milestone verify M2 --criterion 1 --proof "..."`.
   Prove behaviour, not existence: run the thing and observe it.
8. **Lens review** the diff: tech lead, QA, security, and any lens the change touches
   (`review-lenses.md`). Fix findings. Check the test diff for weakened assertions.
9. **Write the report** (`reports/M2.md`): summary, changes, tests run, criteria with evidence, lens
   review, known limitations, deviations. Be specific about what is not covered.
10. **Submit and stop.** `AIDLC milestone submit M2`, present the report with
    `templates/gate-presentation.md`, wait. Code is frozen until the user approves or rejects.
11. **On approval** (user's words in `--note`): `milestone approve M2`. Commit the milestone
    (see below), then `AIDLC next`. **On rejection:** `milestone reject M2 --note "..."`; criteria
    reset; fix and re-verify everything, not only the changed part.

## Code quality bar

- Readable names, small functions, clear module boundaries, no dead code or commented-out blocks
- Follow the existing conventions of the repo and the language. Match formatting tools in use
- Handle errors explicitly. No bare `except`, no swallowed errors, no `TODO` left behind
- Comments explain why, not what. Public interfaces have docstrings or equivalent
- Pin new dependencies and justify them in the report

## Commits

- One logical commit per milestone at minimum (more is fine). Message: `M2: create link endpoint`
  plus a body listing criteria met. Tag approved milestones if the user wants (`aidlc/M2`)
- Commit `.aidlc/` and `docs/aidlc/` with the milestone so history shows the audit trail
- Never commit secrets, build artifacts, or large binaries. Never rewrite published history

## When things go wrong

- **Test fails:** read the failure, form a hypothesis, change one thing, rerun. Three failed attempts at
  the same problem: stop, report diagnosis and options (rule 8)
- **Requirement turns out wrong or impossible:** stop, explain, propose options. If it changes scope
  or design: `milestone add --replan` or `gate reset G2`
- **Blocked** (missing access, ambiguous behaviour only the user can resolve): `milestone block
  --note`, tell the user precisely what you need
- More in `change-and-recovery.md`

## Do not

- Start the next milestone before G4 approval
- Mark criteria verified from memory or by reading code
- Report "tests pass" without having run them in this session
- Edit `.aidlc/` by hand
