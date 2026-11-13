# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-11-09

### Changed
- Skill instructions and metadata are now agent-agnostic: no vendor-specific tool names, "the agent"
  instead of a named model, capability fallbacks for shell, subagents, hooks and persistence
- `gate_hook.py` is harness-agnostic: tolerant tool-call payload parsing (edit, patch and shell tools,
  several key spellings), plus `--check-path` and `--git-pre-commit` modes
- `references/agent-compat.md` rewritten: per-agent install locations, fallbacks, enforcement ladder

### Added
- `scripts/install.py`: installs the skill for Codex, Claude Code, Antigravity, Gemini CLI, Cursor,
  Copilot, Cline, Windsurf, OpenCode or a custom path; optional AGENTS.md, Cursor rule and git hook
- `adapters/` for agents that read instruction files instead of loading skills
- `tests/test_gate_hook_agents.py`

## [1.0.0] - 2026-11-01

### Added
- `SKILL.md` orchestrator with tiering, hard rules, interaction protocol and CLI reference
- Sixteen reference guides: methodology, tiers, inception, requirements elicitation, architecture,
  threat modeling, security, testing, construction, operations, approval gates, review lenses,
  state management, brownfield, change and recovery, agent compatibility
- Eleven templates for gate artifacts, reports, ADRs and manual-mode state
- `aidlc.py`: state machine for gates and milestones with a hash-chained audit log, strict
  human-only approvals, and `verify` cross-checking state against the log
- `gate_hook.py`: tool-call hook enforcing gates technically (Claude Code payload)
- `scan_secrets.py`, `validate_artifacts.py`, `lint_skill.py`
- Unit tests, eval cases, trigger-query set, worked example, CI workflow
