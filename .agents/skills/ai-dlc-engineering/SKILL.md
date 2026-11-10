---
name: ai-dlc-engineering
description: >-
  Structured, milestone-driven software engineering workflow (AI-DLC: Inception, Construction,
  Operations) that makes any coding agent work like a senior engineering team. The agent clarifies requirements,
  designs the architecture, threat-models, plans verifiable milestones, builds one milestone at a
  time with tests and real evidence, and stops at human approval gates. Use whenever the user asks
  to build, scaffold, extend, refactor, migrate, or productionize a project, service, API, app,
  pipeline, or agent system, or says things like "build me", "plan and build", "make this
  production-ready", "vibe code this properly", "act like a senior dev team", or "use AI-DLC", or
  when the repo has a .aidlc folder. Also use for brownfield changes that need a safe plan. Skip
  for one-off snippets, tiny fixes, explanations, and questions.
license: MIT
compatibility: Agent-agnostic (Agent Skills format; also usable via AGENTS.md or rules files). Bundled scripts need Python 3.9+ (stdlib only). Hard gate enforcement is optional and harness-specific (tool hooks, git pre-commit, CI); without it the workflow runs on guidance plus the CLI or manual mode.
metadata:
  version: "1.1.0"
  methodology: "AI-DLC (introduced by AWS, 2025). Independent implementation, not affiliated."
---

# AI-DLC Engineering

Turns "generate 30 files and declare victory" into an engineering process: AI leads the execution,
the human holds the decisions. This skill is written for any coding agent and harness (Claude Code,
Codex, Antigravity, Gemini CLI, Cursor, Copilot and others); where a step names a capability your
harness lacks, use the fallback in `references/agent-compat.md`. Every cycle is:
**plan, clarify, human approves, execute, verify with evidence, human approves.** Details and
rationale: `references/methodology.md`.

`AIDLC` below means `python <skill-dir>/scripts/aidlc.py` (use `python3` if that is what your shell
has), where `<skill-dir>` is the folder holding this SKILL.md. If Python cannot run, use manual mode
(`references/state-management.md`).

**Capabilities this skill assumes**, with fallbacks: a shell (else manual mode), a way to stop and wait
for the user's next message (every agent has this: end your turn), and file read/write. Nothing else is
required. No harness-specific tool names appear in these instructions.

## Step 0: Pick the tier and say it in one line

| Tier | When | What you do |
|------|------|-------------|
| 0 | Snippet, explanation, one-line fix, question | Do not use this skill. Just answer. |
| 1 | Small and self-contained: about 3 files or fewer, no auth/PII/payments/untrusted input, one sitting | 3 to 5 line plan, implement, run it, report real output. No state files, no gates. |
| 2 | Everything else: a project, service, multi-file feature, schema change, anything spanning sessions | Full workflow below. |
| 3 | Tier 2 plus any of: untrusted code execution, auth or secrets handling, payments, PII, multi-tenant data, infra changes, agents with tool access | Full workflow, stricter bars (threat model of 5+ threats), recommend `--strict`. |

Never silently downgrade security-relevant work to tier 1. The user can override the tier; record it.
Escalation, de-escalation and examples: `references/tiers.md`.

## Step 1: Bootstrap or resume

- `.aidlc/state.json` exists: run `AIDLC status`, then follow its `next:` line. Never redo
  inception on a project that already has state.
- Otherwise, tier 2 or 3: `AIDLC init --project NAME [--tier 3] [--mode brownfield] [--strict] --scaffold`.
  Existing codebase? Read `references/brownfield.md` first and use `--mode brownfield`.
- Tell the user what you created and that `.aidlc/` and `docs/aidlc/` belong in git.

## Step 2: Run the phases (read only the reference for the current phase)

| Phase | Read | Output | Gates |
|-------|------|--------|-------|
| 1 Inception | `references/inception.md`, `requirements-elicitation.md`, `architecture-standards.md`, `threat-modeling.md` | requirements, architecture, threat model, milestone plan | G1, G2, G3 |
| 2 Construction | `references/construction.md`, `testing-standards.md`, `security-standards.md` | one verified milestone at a time | G4 per milestone |
| 3 Operations | `references/operations.md` | regression, security, performance, docs, release | G5 |

Templates live in `templates/`. `AIDLC next` always tells you the single next action.

## How you behave

**Clarify once, in a batch.** Ask only questions that change architecture, scope or risk. One message,
numbered, each with a recommended default and why it matters (`templates/clarifying-questions.md`).
"defaults" from the user accepts all. Non-blocking unknowns become written assumptions in the
requirements, not questions.

**Challenge, then respect.** If a requirement is vague, a choice is risky, or scope is bloated, say so
plainly with a better alternative and the trade-off. One round of pushback. If the user still chooses
their way, record an ADR or `note add --kind decision` and proceed. Do not flatter and do not nag.

**Run the lens review before every gate.** Review your own artifact as the people a senior team would
bring in: product, architect, security, QA, tech lead, SRE, writer. Fix what you find, and report both
what you fixed and what remains open. If your harness supports subagents or a separate session, run the review there with only the
artifact and the lens checklist; otherwise do it as a distinct, explicit pass.
Checklists: `references/review-lenses.md`.

**Present gates compactly** using `templates/gate-presentation.md`: summary, key decisions and
trade-offs, lens findings, risks, artifact paths, and the exact words to approve. Then
`AIDLC gate present GX` and **stop**. Approval protocol: `references/approval-gates.md`.

**Keep updates short.** State what changed, what was verified, what is next. No celebration, no
restating the plan.

## Hard rules

1. **No gate is crossed without explicit approval.** Approval is the user's own message saying so
   ("approved", "go ahead", "approve G2"). Silence, a question, a partial answer, or enthusiasm is not
   approval. Quote their words in `--note`. Never approve on the user's behalf.
2. **No "done" without evidence.** Evidence is a command you actually ran and its real output. Reading
   the code is not verification. If you did not run it, say "not run".
3. **Never fabricate or soften results.** Report failures as failures, with output.
4. **Never weaken tests to pass.** Do not delete, skip, loosen or rewrite a test to turn it green. If
   a test is wrong, say why and get approval first.
5. **One milestone in progress at a time.** No work from later milestones. Out-of-scope ideas go to
   `note add`, not into the diff.
6. **Security from milestone 1.** Follow `references/security-standards.md` in every milestone. Code
   from unknown sources runs only in an isolated sandbox, after the user confirms.
7. **State changes only through the CLI** (or manual mode). Never hand-edit `.aidlc/`.
8. **Three strikes.** After three failed attempts at the same problem, stop, report your diagnosis and
   options, and ask. Do not thrash.
9. **Plan changes are gated.** Scope or design changes after G3 mean `milestone add --replan`, which
   resets G3 and needs fresh approval (`references/change-and-recovery.md`).
10. **Be honest about uncertainty.** Say what you assumed, what you could not verify, and what could
    be wrong, in the milestone and completion reports.

## User authority

The user owns decisions. If they explicitly waive a gate or step ("skip the threat model, I know the
risks"), state the specific risk once in a sentence, record it (`AIDLC note add --kind waiver --text "..."`),
and proceed on their terms. A waiver of ceremony is fine; a request to misreport results is not.

## Quick CLI reference

```
AIDLC status | next | verify | log | render
AIDLC gate present|approve|reject|reset G1|G2|G3|G5 [--note "user said: ..."]
AIDLC milestone add M1 --title T --accept "criterion" [--depends M0] [--replan]
AIDLC milestone start|submit|approve|reject|block|unblock|reopen M1 [--note ...]
AIDLC milestone evidence M1 --cmd "pytest -q" --result "42 passed" [--file logs/m1.txt]
AIDLC milestone verify M1 --criterion 1 --proof "test_x passes, see log"
AIDLC note add --kind decision|risk|deviation|blocker|waiver --text "..."   |   adr "Title"
python <skill-dir>/scripts/scan_secrets.py .          # secrets scan
python <skill-dir>/scripts/validate_artifacts.py      # artifact structure check
```

## What is and is not enforced

This skill is mostly guidance, backed by tools. The CLI rejects illegal transitions and records every
approval in a hash-chained log that `verify` cross-checks. With `--strict`, approvals must be typed by a
human in a real terminal. Beyond that, enforcement depends on the harness, from strongest to weakest:

1. **CI and protected branches** (works with every agent): run `verify`, `validate_artifacts.py` and
   `scan_secrets.py`, plus the tests, and require human review.
2. **Git pre-commit hook** (every agent that commits through git): `scripts/gate_hook.py --git-pre-commit`.
3. **Tool-call hooks** (only harnesses that support them): `scripts/gate_hook.py` blocks source edits
   outside an in-progress milestone.
4. **Guidance only** (everything else): the user reading `STATE.md` and the evidence is the enforcement.

If asked, say exactly this and name which level is active in this project. Setup per harness and the
honest limits: `references/agent-compat.md`.

## File map

- `references/`: methodology, tiers, inception, requirements-elicitation, architecture-standards,
  threat-modeling, security-standards, testing-standards, construction, operations, approval-gates,
  review-lenses, state-management, brownfield, change-and-recovery, agent-compat
- `templates/`: requirements, architecture, threat-model, milestone-plan, milestone-report,
  completion-report, adr, clarifying-questions, gate-presentation, release-checklist, state-manual
- `scripts/`: `aidlc.py`, `gate_hook.py`, `install.py`, `scan_secrets.py`, `validate_artifacts.py`, `lint_skill.py`
- `adapters/`: ready-to-copy instruction files for agents that do not load skills (AGENTS.md, Cursor, Copilot, git hook)
- `examples/`: a complete worked project with real state and audit log
