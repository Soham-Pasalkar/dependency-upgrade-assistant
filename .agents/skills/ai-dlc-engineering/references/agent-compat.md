# Agent compatibility

The skill is plain Markdown plus Python 3.9+ scripts (stdlib only), in the open Agent Skills layout:
a folder with `SKILL.md` (YAML frontmatter with `name` and `description`) plus `references/`,
`templates/`, `scripts/`. Nothing in the instructions depends on one vendor's tools: they talk about a
"file-edit tool", a "shell" and "subagents", and every capability has a fallback (below).

Three layers, each independent:

1. **Content**: `SKILL.md` and the files it points to. Identical everywhere.
2. **Discovery**: how your agent finds the skill (a skills folder, or an instruction file that points at it).
3. **Enforcement**: what actually stops an agent from skipping a gate (hooks, git, CI). Optional, and the
   only layer that differs in strength between harnesses.

## 1. Install (discovery)

Fastest path, any agent: `python scripts/install.py --agent <name> [--scope user] [--with-agents-md]`
(`--agent all` for every layout; `--dest PATH` for anything else). Or copy the folder by hand to one of:

| Agent | Project skills folder | User skills folder | Instruction-file fallback |
|-------|-----------------------|--------------------|---------------------------|
| Claude Code | `.claude/skills/` | `~/.claude/skills/` | `CLAUDE.md` |
| Codex (CLI, IDE) | `.agents/skills/` (scanned from the working dir up to the repo root) | `~/.agents/skills/` (older builds: `~/.codex/skills/`) | `AGENTS.md` |
| Antigravity | `.agents/skills/` (older: `.agent/skills/`) | `~/.gemini/antigravity/skills/` (newer builds: `~/.gemini/config/skills/`) | `AGENTS.md`, `GEMINI.md`, workspace rules |
| Gemini CLI | `.gemini/skills/` (some versions also `.agents/skills/`) | `~/.gemini/skills/` | `GEMINI.md` |
| Cursor | `.cursor/skills/` (or `.agents/skills/`) | `~/.cursor/skills/` | `.cursor/rules/*.mdc`, `AGENTS.md` |
| GitHub Copilot | `.github/skills/` (or `.agents/skills/`) | `~/.copilot/skills/` | `.github/copilot-instructions.md`, `AGENTS.md` |
| Cline | `.cline/skills/` or `.agents/skills/` | `~/.cline/skills/` | `.clinerules/` |
| Windsurf | `.windsurf/skills/` | `~/.codeium/windsurf/skills/` | `.windsurf/rules/` |
| OpenCode | `.opencode/skills/` | see its docs | `AGENTS.md` |
| Others (Amp, Goose, Roo, Kiro, Junie, Qwen Code, ...) | see the agent's docs; many read `.agents/skills/` | | `AGENTS.md` |

**These paths drift.** The table reflects the agents' public docs and the open `skills` CLI at the time
of writing, and several vendors have already moved folders between versions. If the skill is not picked
up: restart the session, confirm the folder name equals the `name` in `SKILL.md`, run
`python scripts/lint_skill.py <skill-dir>`, then check the agent's current docs and use `--dest`.
`.agents/skills/` is the emerging shared location, so a good default is to install there once.

**Agents that do not load skills.** Append `adapters/AGENTS.md.snippet` to `AGENTS.md` (or put the same
text in `GEMINI.md`, `.clinerules/`, `.windsurf/rules/`, `.github/copilot-instructions.md`, or a Cursor
rule: `adapters/cursor-rule.mdc`). The snippet makes the agent read `SKILL.md` and follow it. The user can
also start a task with "Follow ai-dlc-engineering/SKILL.md for this".

**Chat UIs with a sandbox** (for example claude.ai with code execution): upload the packaged `.skill`
zip where skills are supported. The sandbox is per conversation, so download `.aidlc/` and `docs/aidlc/`
at the end and re-upload them to resume. `--strict` needs a real terminal and cannot work in a chat
sandbox. Without code execution use manual mode (`state-management.md`).

## 2. Capability fallbacks

| The workflow wants | If your harness lacks it |
|--------------------|--------------------------|
| A shell to run `aidlc.py` and the tests | Manual mode (`state-management.md`); say which checks could not be run. "Not run" is a valid, honest result |
| Stopping and waiting for the user | Always available: end your turn after presenting a gate. Never continue past it |
| Subagents or a second session for lens reviews | Do the review as a separate, explicit pass over the artifact with the lens checklist, and say it was a self-review |
| Persistent project files | Keep `.aidlc/` and `docs/aidlc/` in the user's repo; in sandboxes, have the user download and re-upload them |
| Tool-call hooks | Use the git pre-commit hook and CI (next section) |
| A real terminal for `--strict` | Use default mode; the user's own "approve GX" message is the approval |

## 3. Enforcement ladder

Strongest first. State to the user which level is active in their project.

1. **CI and protected branches** (every agent). Run these in the pipeline and require human review:
   ```bash
   python <skill-dir>/scripts/aidlc.py verify          # state and audit-log integrity
   python <skill-dir>/scripts/validate_artifacts.py     # artifact structure
   python <skill-dir>/scripts/scan_secrets.py .         # secrets
   ```
   plus the project's own test suite.
2. **Git pre-commit hook** (every agent that commits through git). `python scripts/install.py --git-hook`,
   or copy `adapters/git-pre-commit` to `.git/hooks/pre-commit`. It runs `aidlc.py verify` and rejects
   commits that stage source files before G1 to G3 are approved. Agents can bypass hooks with
   `--no-verify`; treat that as a policy violation and keep CI as the backstop.
3. **Tool-call hooks** (harnesses that can run a command before an edit or shell tool). Configure the
   harness to run `python <skill-dir>/scripts/gate_hook.py` on file-edit, patch and shell tool calls. The
   script reads the harness's JSON payload on stdin and exits 2 with a reason on stderr to block. It
   blocks source edits until G1 to G3 are approved and outside an in-progress milestone, protects
   `.aidlc/`, and always allows docs. The payload parser tolerates common spellings of tool-name and
   path keys and recognises edit, patch and shell tools, but **only the Claude Code payload shape is
   covered by this project's tests**. For any other harness:
   - read its hook docs for the payload shape and the "block" convention (exit code 2 is common, but not universal)
   - check the decision logic without the harness: `python scripts/gate_hook.py --check-path src/app.py`
   - then try a real edit before approval and confirm it is blocked. If it is not, treat the hook as not active
4. **Guidance only**. The user reading `STATE.md`, the evidence and the diffs is the enforcement.

### Claude Code hook (verified payload)

`.claude/settings.json` (see `examples/claude-code/settings.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/skills/ai-dlc-engineering/scripts/gate_hook.py\""
          }
        ]
      }
    ]
  }
}
```

Hook settings format follows Claude Code's hooks documentation at the time of writing; check the
current docs if behaviour differs. For Claude Code projects that always use the workflow, add to
`CLAUDE.md`: "This project uses the ai-dlc-engineering skill. Follow it for any non-trivial change: run
`aidlc.py status` and `aidlc.py next` first, and never cross an approval gate without my explicit approval."

### Hook behaviour and limits

- Does nothing in projects without `.aidlc/`. Humans can disable it for a session with `export AIDLC_HOOK=off`.
- Fails open on unreadable input (it must not brick your editor), so a broken hook configuration looks
  like "no enforcement". Step 3's test edit catches this.
- The shell-command check is a heuristic. An agent with arbitrary shell access can route around any
  in-process hook. Pair hooks with `init --strict`, CI and a protected main branch for real assurance.

## Quick instruction for the agent

If you are the agent reading this: detect your harness from your own tool names and environment, follow
the fallbacks above for anything you lack, never claim enforcement that is not installed, and when in
doubt describe the active level to the user in one sentence.
