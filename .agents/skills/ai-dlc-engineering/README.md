# AI-DLC Engineering

**A skill that makes your coding agent work like a senior engineering team.**
Clarify, design, threat-model, plan, build one verified milestone at a time, and stop at human approval
gates, with state and an audit trail that survive across sessions.

Agent-agnostic. Built on the open [Agent Skills](https://agentskills.io) layout (`SKILL.md` plus
references, templates and scripts), so it runs in Claude Code, Codex, Antigravity, Gemini CLI, Cursor,
GitHub Copilot, Cline, Windsurf, OpenCode and any other agent that loads skills. Agents that only read
instruction files (`AGENTS.md`, rules files) use ready-made adapters. Scripts need Python 3.9+ and nothing else.

> Independent implementation of the AI-DLC methodology introduced by AWS (2025). Not affiliated with
> or endorsed by AWS. See [Attribution](#attribution).

## Why

Unstructured "vibe coding" fails in predictable ways: it builds the wrong thing fast, produces diffs
nobody can review, claims "it works" without running anything, rewrites tests to go green, bolts on
security last, and forgets everything between sessions. This skill turns each failure into a rule, a
template, or a tool check. See the full table in [`references/methodology.md`](references/methodology.md).

## The workflow

```
 INCEPTION                         CONSTRUCTION (per milestone)                OPERATIONS
 clarify -> requirements --G1-->   start -> test-first -> implement ->        regression, security,
 architecture + threats --G2-->    run everything -> evidence -> lens           performance, docs,
 milestone plan --G3-->            review -> report --G4--> next milestone      release --G5--> released
        (human approves each gate; the agent stops and waits)
```

| You get | What it does |
|---------|--------------|
| **Tiering** | Throwaway scripts skip the ceremony; services and anything security-sensitive get the full workflow |
| **Batched clarification** | One message of questions, each with a recommended default |
| **Gated artifacts** | Requirements, architecture, threat model, milestone plan, milestone reports, completion report, ADRs |
| **Lens reviews** | The agent reviews its own work as product, architect, security, QA, tech lead, SRE and writer, and reports fixed and open findings |
| **Evidence rule** | A milestone is done only with real commands and output, and every criterion verified |
| **State and audit log** | `.aidlc/` holds machine state and a hash-chained event log; resume any time with `status` and `next` |
| **Enforcement tooling** | CLI rejects illegal transitions; git pre-commit hook and CI work with every agent; optional tool-call hook blocks edits outside an approved milestone where the harness supports hooks; strict mode makes approvals human-only |
| **Brownfield mode** | Baseline, characterization tests, seams, minimal diffs |
| **Security by default** | Per-milestone security checklist, threat modeling guide, untrusted-code sandbox controls, secrets scanner |

## Install

One command for any agent (run from your project root, from a clone of this repo or the unpacked skill):

```bash
python scripts/install.py --agent codex claude gemini     # project install for the agents you use
python scripts/install.py --agent all --with-agents-md    # every known layout plus an AGENTS.md pointer
python scripts/install.py --agent cursor --scope user     # personal install
python scripts/install.py --dest <any skills dir>         # custom location
python scripts/install.py --git-hook                      # also install the git pre-commit gate hook
```

By hand: copy the folder to your agent's skills directory, for example `.agents/skills/` (shared by
Codex, Antigravity and several others), `.claude/skills/`, `.gemini/skills/`, `.cursor/skills/` or
`.github/skills/`. Agents without skill loading: append `adapters/AGENTS.md.snippet` to `AGENTS.md`.
Claude.ai: upload the packaged `.skill` zip in skills settings with code execution enabled.

Skill folders move between agent versions, so the per-agent table and troubleshooting steps are in
[`references/agent-compat.md`](references/agent-compat.md).

## Use

Just ask for the work, or name it:

> Build me a secure URL shortener API in Python with Postgres. Use AI-DLC.
> Add JWT auth to this existing app, plan it properly first.
> Continue the project (resumes from `.aidlc/`).

The agent states a tier, asks its batched questions, drafts requirements, and presents **G1**. It then
stops until you reply "approve G1" (or tell it what to change). A worked example with real state and
audit log is in [`examples/url-shortener`](examples/url-shortener).

## What is actually enforced

Honest table. Guidance alone is not a control.

| Rule | Instructions | CLI | Tool-call hook (where supported) | `--strict` |
|------|:-----------:|:---:|:------------------:|:----------:|
| Stop at gates, no self-approval | yes | records approvals, orders gates | | human must type approval |
| No milestone start before G1 to G3 approved | yes | **blocked** | source edits blocked | |
| One milestone open at a time | yes | **blocked** | | |
| No submit without evidence, verified criteria, valid report | yes | **blocked** | | |
| Code frozen while awaiting approval | yes | | **blocked** | |
| State cannot be hand-edited unnoticed | yes | `verify` detects | edits to `.aidlc/` blocked | |
| No source commits before G1 to G3 | yes | | pre-commit hook (any agent) | |
| No secrets committed | yes | | | `scan_secrets.py` / CI |
| Tests not weakened | yes (rule 4, QA lens) | | | review and CI only |

Enforcement levels, strongest first: CI plus protected branches (every agent), git pre-commit hook
(every agent that commits through git), tool-call hook (harnesses with hooks; only the Claude Code
payload is covered by this repo's tests), guidance only. See `references/agent-compat.md`.

Limits: the audit log detects casual tampering, not a full rewrite; the hook's shell check is a
heuristic; an agent with unrestricted shell access can bypass any in-process check. For real assurance
add `--strict`, CI running `aidlc.py verify` and your tests, and protected branches with human review.

## Repository layout

```
SKILL.md                orchestrator (kept under 500 lines)
references/             16 guides, loaded on demand
templates/              artifact and report templates
scripts/                aidlc.py  gate_hook.py  install.py  scan_secrets.py  validate_artifacts.py  lint_skill.py
adapters/               AGENTS.md, Cursor, Copilot and git pre-commit files for agents without skill loading
examples/url-shortener  worked example with real .aidlc state
evals/                  behavioural eval cases and trigger-query set
tests/                  unit tests (stdlib unittest)
```

## CLI cheat sheet

```bash
aidlc.py init --project NAME [--tier 3] [--mode brownfield] [--strict] [--scaffold]
aidlc.py status | next | verify | log | render
aidlc.py gate present|approve|reject|reset G1|G2|G3|G5 [--note "user said ..."]
aidlc.py milestone add|start|evidence|verify|submit|approve|reject|block|unblock|reopen M1 ...
aidlc.py note add --kind decision|risk|deviation|blocker|waiver --text "..."
aidlc.py adr "Decision title"
```

## Design decisions

- **Progressive disclosure.** `SKILL.md` is the router and the rules; depth lives in references the
  agent reads per phase, keeping context small
- **Tiering over uniform ceremony.** A methodology that burdens a ten-line script gets abandoned
- **Tools where rules must hold.** Everything an agent could talk itself out of is backed by a
  deterministic check where practical
- **Stdlib only.** No install step, runs anywhere Python does
- **Honesty requirements.** "Not verified" and "Residual risks" are mandatory sections, validated by script

Non-goals: replacing human code review, CI, or dedicated security scanners; being a project-management
tool; supporting parallel multi-agent writes to the same state.

## Testing

```bash
python3 -m unittest discover -s tests          # unit tests for CLI, hook, scanner, validator
python3 scripts/lint_skill.py .                # package lint
```

Behavioural evals live in `evals/` (one folder per case: prompt, expected outcome, graders), plus
`evals/trigger-queries.json` for tuning the skill description. Unit tests cover the tools; the evals
check agent behaviour and need a model to run. Contributions of transcripts and new cases are the most
valuable kind.

## Roadmap

- Stack-specific appendices (Python, TypeScript, Go) for test and audit commands
- Optional GitHub Action wrapping `verify`, `validate_artifacts` and `scan_secrets`
- Parallel-milestone support with explicit independence checks
- More worked examples (brownfield, untrusted-code sandbox)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## Attribution

The AI-DLC term, phases (Inception, Construction, Operations), and concepts such as Intent, Units of
work, Bolts and Mob Elaboration come from AWS's AI-Driven Development Life Cycle, introduced in 2025.
This repository is an independent, community implementation for coding agents and adds its own state,
gate, audit and enforcement tooling. All trademarks belong to their owners.

## License

MIT, see [LICENSE](LICENSE).
