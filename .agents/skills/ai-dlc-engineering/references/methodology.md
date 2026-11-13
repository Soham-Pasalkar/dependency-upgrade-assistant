# Methodology

## Origin and attribution

AI-DLC (AI-Driven Development Life Cycle) was introduced by AWS in 2025 (Raja SP and colleagues) as an
AI-centric alternative to sprint-based SDLC: AI drives execution across three phases (Inception,
Construction, Operations) while humans keep decision rights and validate outcomes. Its core loop is:
AI creates a plan, asks for clarification, humans approve, AI executes, humans verify.

This skill is an **independent implementation** for coding agents. It is not affiliated with or
endorsed by AWS. It borrows the phase structure and vocabulary and adds tooling (state, gates, audit
log, hooks, scanners) that the original describes only as practice.

| AWS AI-DLC term | In this skill |
|-----------------|---------------|
| Intent | The `Intent` section of `requirements.md` |
| Mob Elaboration | Inception: batched questions, requirements, design, plan, validated with the user at G1 to G3 |
| Unit of work | A milestone (or a group of milestones) |
| Bolt | One milestone cycle: hours to a day or two, not weeks |
| Mob Construction | The per-milestone loop with the user approving at G4 |
| Persistent context | `docs/aidlc/` (artifacts) and `.aidlc/` (state and audit log), committed to git |

## Why this exists: failure modes of unstructured agent coding

| Failure | Countermeasure here |
|---------|---------------------|
| Builds the wrong thing fast because requirements were guessed | Batched clarification, written assumptions, G1 |
| Giant diff nobody can review | One milestone at a time, reviewable size |
| "It works" claimed from reading code | Evidence rule: command plus real output |
| Tests rewritten to pass | Rule 4, QA lens, reviewer checks test diffs |
| Security bolted on at the end | Threat model at G2, security standards in every milestone |
| Architecture drift and surprise dependencies | ADRs, boring-by-default stack, G2 |
| Scope creep inside a milestone | Out-of-scope notes, `--replan` for plan changes |
| Context lost between sessions | `STATE.md`, `next` command, artifacts in repo |
| Agent marks its own homework | Approval gates, strict mode, hash-chained log |
| Thrashing on a failing test | Three-strikes rule |
| Overconfident final summary | Mandatory "Not verified" and "Residual risks" sections |

## Principles

1. **Humans decide, AI executes.** Direction, trade-offs, risk acceptance and release belong to the user.
2. **Plan before code, and verify before claiming.** Both are cheap compared with rework.
3. **Small verifiable steps.** If you cannot say how a milestone will be proven, it is too big or too vague.
4. **Evidence over assertion.** Output beats narrative.
5. **Simplest thing that meets the requirements.** Complexity needs a written justification.
6. **Security is a property of every change,** not a phase.
7. **Persist context in the repo.** Anything only in chat history is lost.
8. **Scale ceremony to risk.** A throwaway script does not need a threat model. A payments flow does.

## Global definition of done (any milestone)

- Acceptance criteria each verified with a recorded command and output
- New and existing tests pass, run in this session
- Linter and type checks clean (or pre-existing failures listed)
- No secrets, debug leftovers or commented-out code added
- Security standards applied; new inputs validated; new dependencies justified
- Docs and comments updated where behaviour changed
- Milestone report complete, including known limitations
- User has approved (G4)

## Anti-patterns

- Asking ten questions one at a time, or asking things you could decide and flag
- Presenting a gate and continuing to work "while waiting"
- Treating "looks good" about one artifact as approval of the next gate
- Padding artifacts with boilerplate to look thorough
- Using ceremony as a shield: the goal is a working, trustworthy system, not paperwork
