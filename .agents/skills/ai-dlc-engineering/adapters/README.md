# Adapters

Use these when your agent does not load Agent Skills folders, or to add a safety net on top of one
that does. `scripts/install.py` can place them for you; they are also fine to copy by hand.

| File | Goes to | Used by |
|------|---------|---------|
| `AGENTS.md.snippet` | append to `AGENTS.md` at the repo root | Codex, Cursor, Copilot, Gemini CLI (when configured to read it), Antigravity, OpenCode, Amp and other agents that read AGENTS.md |
| `cursor-rule.mdc` | `.cursor/rules/ai-dlc.mdc` | Cursor |
| `copilot-instructions.md.snippet` | append to `.github/copilot-instructions.md` | GitHub Copilot |
| `git-pre-commit` | `.git/hooks/pre-commit` | any agent that commits through git |
| `../examples/claude-code/settings.json` | `.claude/settings.json` | Claude Code tool-call hook |

Agents that use other instruction files (for example `GEMINI.md`, `.windsurf/rules/`, `.clinerules/`)
can use the text of `AGENTS.md.snippet` verbatim; only the file location differs.
