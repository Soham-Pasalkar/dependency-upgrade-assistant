#!/usr/bin/env python3
"""Install the ai-dlc-engineering skill for one or more coding agents. Stdlib only.

Examples:
  python scripts/install.py --agent codex claude                 # into the current project
  python scripts/install.py --agent all --with-agents-md         # every known layout + AGENTS.md pointer
  python scripts/install.py --agent gemini --scope user          # personal install
  python scripts/install.py --agent generic --git-hook           # .agents/skills + git pre-commit hook
  python scripts/install.py --dest ~/somewhere/skills            # any custom skills directory
  python scripts/install.py --agent cursor --dry-run

Skill locations change between agent versions. The table below was checked against the agents' public
docs and the open `skills` CLI at the time of writing; if your agent does not pick the skill up, pass
`--dest` with the path from its current documentation. See references/agent-compat.md.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

SKILL_NAME = "ai-dlc-engineering"
SKILL_DIR = Path(__file__).resolve().parent.parent

# agent -> (project-relative skills dir, user skills dir or None)
LAYOUTS = {
    "generic":     (".agents/skills",   "~/.agents/skills"),     # shared convention
    "codex":       (".agents/skills",   "~/.agents/skills"),
    "claude":      (".claude/skills",   "~/.claude/skills"),
    "antigravity": (".agents/skills",   "~/.gemini/antigravity/skills"),
    "gemini":      (".gemini/skills",   "~/.gemini/skills"),
    "cursor":      (".cursor/skills",   "~/.cursor/skills"),
    "copilot":     (".github/skills",   "~/.copilot/skills"),
    "cline":       (".cline/skills",    "~/.cline/skills"),
    "windsurf":    (".windsurf/skills", "~/.codeium/windsurf/skills"),
    "opencode":    (".opencode/skills", None),
}
NOTES = {
    "antigravity": "Antigravity's skill folders have changed between versions (.agent/ vs .agents/, "
                   "and the global path). If it is not detected, re-run with --dest set per its docs, "
                   "and restart the agent session.",
    "codex": "Codex also reads AGENTS.md; add --with-agents-md if you want the workflow always applied.",
}
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "tests", ".DS_Store", "*.skill", "dist")
MARK_BEGIN, MARK_END = "<!-- ai-dlc-engineering:begin -->", "<!-- ai-dlc-engineering:end -->"


def install_dir(src: Path, dest: Path, symlink: bool, dry: bool) -> str:
    target = dest / SKILL_NAME
    if target.resolve() == src.resolve():
        return f"skip (already the source): {target}"
    if dry:
        return f"would install: {target}"
    dest.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)
    if symlink:
        os.symlink(src, target, target_is_directory=True)
        return f"linked:    {target} -> {src}"
    shutil.copytree(src, target, ignore=IGNORE)
    return f"installed: {target}"


def append_block(path: Path, text: str, dry: bool) -> str:
    block = f"{MARK_BEGIN}\n{text.rstrip()}\n{MARK_END}\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if MARK_BEGIN in existing:
        return f"already present: {path}"
    if dry:
        return f"would append to: {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + sep + block, encoding="utf-8")
    return f"updated:   {path}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--agent", nargs="+", default=["generic"],
                    help="one or more of: " + ", ".join(LAYOUTS) + ", all")
    ap.add_argument("--scope", choices=["project", "user"], default="project")
    ap.add_argument("--project-dir", default=".", help="project root for project scope (default: cwd)")
    ap.add_argument("--dest", help="explicit skills directory (overrides --agent layouts)")
    ap.add_argument("--symlink", action="store_true", help="symlink instead of copying")
    ap.add_argument("--with-agents-md", action="store_true", help="append the workflow pointer to AGENTS.md")
    ap.add_argument("--with-cursor-rule", action="store_true", help="write .cursor/rules/ai-dlc.mdc")
    ap.add_argument("--with-copilot-instructions", action="store_true",
                    help="append the pointer to .github/copilot-instructions.md")
    ap.add_argument("--git-hook", action="store_true", help="install the git pre-commit gate hook")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    project = Path(args.project_dir).resolve()
    agents = list(LAYOUTS) if "all" in args.agent else args.agent
    bad = [a for a in agents if a not in LAYOUTS]
    if bad and not args.dest:
        print(f"unknown agent(s): {', '.join(bad)}. Known: {', '.join(LAYOUTS)}, all", file=sys.stderr)
        return 2

    dests: dict[Path, list] = {}
    if args.dest:
        dests[Path(args.dest).expanduser().resolve()] = ["custom"]
    else:
        for a in agents:
            proj_rel, user_dir = LAYOUTS[a]
            if args.scope == "user":
                if user_dir is None:
                    print(f"note: no user-level path known for {a}; skipping (use --dest)", file=sys.stderr)
                    continue
                d = Path(user_dir).expanduser().resolve()
            else:
                d = (project / proj_rel).resolve()
            dests.setdefault(d, []).append(a)

    results = []
    for d, names in dests.items():
        results.append(install_dir(SKILL_DIR, d, args.symlink, args.dry_run) + f"   [{', '.join(names)}]")
    for a in agents:
        if a in NOTES and not args.dest:
            results.append(f"note ({a}): {NOTES[a]}")

    first = next(iter(dests), None)
    rel = ".agents/skills/" + SKILL_NAME
    if first is not None and args.scope == "project":
        try:
            rel = str((first / SKILL_NAME).relative_to(project))
        except ValueError:
            rel = str(first / SKILL_NAME)
    adapters = SKILL_DIR / "adapters"

    def adapted(name: str) -> str:
        return (adapters / name).read_text(encoding="utf-8").replace(".agents/skills/" + SKILL_NAME, rel)

    if args.with_agents_md:
        results.append(append_block(project / "AGENTS.md", adapted("AGENTS.md.snippet"), args.dry_run))
    if args.with_copilot_instructions:
        results.append(append_block(project / ".github" / "copilot-instructions.md",
                                    adapted("copilot-instructions.md.snippet"), args.dry_run))
    if args.with_cursor_rule:
        target = project / ".cursor" / "rules" / "ai-dlc.mdc"
        if target.exists():
            results.append(f"already present: {target}")
        elif args.dry_run:
            results.append(f"would write: {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(adapted("cursor-rule.mdc"), encoding="utf-8")
            results.append(f"wrote:     {target}")
    if args.git_hook:
        git_dir = project / ".git"
        hook = git_dir / "hooks" / "pre-commit"
        if not git_dir.is_dir():
            results.append("git hook skipped: no .git directory in the project")
        elif hook.exists():
            results.append(f"git hook NOT installed, {hook} already exists; chain it manually "
                           f"(see adapters/git-pre-commit)")
        elif args.dry_run:
            results.append(f"would write: {hook}")
        else:
            hook.parent.mkdir(parents=True, exist_ok=True)
            body = (adapters / "git-pre-commit").read_text(encoding="utf-8").replace(
                ".agents/skills/" + SKILL_NAME, rel)
            hook.write_text(body, encoding="utf-8")
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            results.append(f"wrote:     {hook}")

    print("\n".join(results))
    print("\nNext: restart your agent session so it re-scans skills, then ask for the work "
          "(for example \"Build X. Use AI-DLC.\").\nEnforcement level depends on your harness: "
          "see references/agent-compat.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
