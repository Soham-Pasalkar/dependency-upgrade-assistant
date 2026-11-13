#!/usr/bin/env python3
"""Harness-agnostic gate enforcement for AI-DLC projects (stdlib only).

Three ways to run it, from most to least universal:

  1. Git pre-commit hook (works with every agent that commits through git):
         python gate_hook.py --git-pre-commit
     Blocks the commit when .aidlc/ fails `aidlc.py verify`, or when source files are staged while
     G1-G3 are not all approved.

  2. Direct path check (for wrappers, scripts and tests):
         python gate_hook.py --check-path src/app.py [more paths...]
     Exit 2 and a reason on stderr if editing those paths is not allowed right now.

  3. Tool-call hook for harnesses that can run a command before a tool call and read a JSON payload
     on stdin (no arguments):
         python gate_hook.py
     Exit code 2 blocks the call and shows the reason to the agent. The payload parser is tolerant:
     it accepts tool-name and tool-input keys in several spellings and common file-edit, shell and
     patch tool names. Only the Claude Code payload shape is verified by this project's tests; for
     any other harness, confirm its hook payload and blocking convention in its docs and test with
     `--check-path` first (see references/agent-compat.md).

Rules (when an AI-DLC project, i.e. .aidlc/state.json, exists; otherwise everything is allowed):
  * edits to .aidlc/ are blocked (state changes only through aidlc.py)
  * source edits are blocked until G1-G3 are approved
  * source edits are blocked when no milestone is in_progress (none started, or the open one is
    awaiting approval)
  * shell commands that write into .aidlc/ without aidlc.py are blocked (best effort heuristic)
Documentation files (docs/, *.md, *.rst, *.txt, LICENSE) are always editable.

Escape hatch for the human: export AIDLC_HOOK=off before launching the agent.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Original Claude Code names kept for backward compatibility.
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Normalised (lowercase, no separators) names seen across harnesses.
_EDIT_NORM = {
    "write", "edit", "multiedit", "notebookedit", "writefile", "replace", "editfile", "createfile",
    "strreplace", "strreplaceeditor", "strreplacebasededittool", "insert", "savefile", "updatefile",
    "applypatch", "patch", "searchreplace", "replaceinfile", "writetofile", "deletefile", "movefile",
}
_PATCH_NORM = {"applypatch", "patch"}
_SHELL_NORM = {
    "bash", "shell", "runshellcommand", "runcommand", "executecommand", "terminal", "localshell",
    "execcommand", "runterminalcmd", "runinterminal", "execute",
}
PATH_KEYS = ("file_path", "path", "filePath", "file", "target_file", "targetFile", "notebook_path",
             "absolute_path", "filename", "target")
DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
DOC_NAMES = {"LICENSE", "LICENCE", "NOTICE", ".gitignore", "CODEOWNERS"}
BASH_WRITE = re.compile(r"(>|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\btruncate\b|\bdd\b|"
                        r"\bperl\s+-i|python[0-9.]*\s+-c|\bnode\s+-e)")
PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$|^\*\*\* Move to:\s*(.+?)\s*$"
                        r"|^\+\+\+ (?:b/)?(\S+)", re.M)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def find_root(start: Path):
    for d in [start, *start.parents]:
        if (d / ".aidlc" / "state.json").is_file():
            return d
    return None


def is_doc(root: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    parts = rel.parts
    return (bool(parts) and parts[0] == "docs") or path.suffix.lower() in DOC_SUFFIXES \
        or path.name in DOC_NAMES


def _load_state(root: Path):
    try:
        return json.loads((root / ".aidlc" / "state.json").read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError):
        return None, "AI-DLC: .aidlc/state.json is unreadable; run `aidlc.py verify`."


def _gate_missing(state) -> list:
    return [g for g in ("G1", "G2", "G3") if state["gates"][g]["status"] != "approved"]


def check_path(root: Path, cwd: Path, raw: str, state: dict, *, require_milestone: bool = True):
    """Return (\"allow\"|\"deny\", reason) for an edit of `raw`."""
    path = Path(raw)
    path = (path if path.is_absolute() else cwd / path).resolve()
    try:
        rel = path.relative_to(root)
    except ValueError:
        return "allow", ""
    if rel.parts and rel.parts[0] == ".aidlc":
        return "deny", ("AI-DLC: .aidlc/ is managed by aidlc.py (validated, hash-chained). "
                        "Use the CLI instead of editing it.")
    if is_doc(root, path):
        return "allow", ""
    missing = _gate_missing(state)
    if missing:
        return "deny", (f"AI-DLC: source edits are blocked until {', '.join(missing)} "
                        "is approved by the user (inception). Run `aidlc.py next`.")
    if not require_milestone:
        return "allow", ""
    ms = state.get("milestones", [])
    if any(m["status"] == "in_progress" for m in ms):
        return "allow", ""
    waiting = [m["id"] for m in ms if m["status"] == "awaiting_approval"]
    if waiting:
        return "deny", (f"AI-DLC: {waiting[0]} is submitted and awaiting approval or acceptance; "
                        "code is frozen until it is accepted or approved.")
    return "deny", ("AI-DLC: no milestone is in progress. Run `aidlc.py next` and "
                    "`aidlc.py milestone start <ID>` (or `milestone add --replan` for new work).")


def _first(d: dict, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _paths_from_input(tool_norm: str, inp: dict) -> list:
    paths = []
    for k in PATH_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v:
            paths.append(v)
    if tool_norm in _PATCH_NORM or "patch" in inp or "input" in inp:
        blob = " ".join(str(inp.get(k, "")) for k in ("patch", "input", "diff", "command", "content"))
        for m in PATCH_FILE.finditer(blob):
            p = next((g for g in m.groups() if g), None)
            if p and p != "/dev/null":
                paths.append(p)
    return list(dict.fromkeys(paths))


def decide(payload: dict):
    """Return ("allow"|"deny", reason) for a tool-call hook payload."""
    cwd = Path(_first(payload, ("cwd", "workingDirectory", "working_directory", "workspace")) or
               os.getcwd()).resolve()
    root = find_root(cwd)
    if root is None:
        return "allow", ""
    state, err = _load_state(root)
    if state is None:
        return "deny", err
    if not state.get("config", {}).get("hook_enforcement", True):
        return "allow", ""
    tool = str(_first(payload, ("tool_name", "toolName", "tool", "name")) or "")
    inp = _first(payload, ("tool_input", "toolInput", "input", "arguments", "args", "parameters")) or {}
    if not isinstance(inp, dict):
        inp = {"command": inp}
    tn = _norm(tool)

    if tn in _SHELL_NORM:
        cmd = inp.get("command", inp.get("cmd", ""))
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        cmd = str(cmd)
        if ".aidlc" in cmd and "aidlc.py" not in cmd and BASH_WRITE.search(cmd):
            return "deny", ("AI-DLC: do not modify .aidlc/ directly. Use "
                            "`python <skill-dir>/scripts/aidlc.py ...` so transitions are validated "
                            "and logged.")
        if "apply_patch" in cmd or "*** Begin Patch" in cmd:
            for m in PATCH_FILE.finditer(cmd):
                p = next((g for g in m.groups() if g), None)
                if p:
                    verdict, reason = check_path(root, cwd, p, state)
                    if verdict == "deny":
                        return verdict, reason
        return "allow", ""

    if tn not in _EDIT_NORM and tool not in EDIT_TOOLS:
        return "allow", ""
    for raw in _paths_from_input(tn, inp):
        verdict, reason = check_path(root, cwd, raw, state)
        if verdict == "deny":
            return verdict, reason
    return "allow", ""


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def git_pre_commit(cwd: Path):
    root = find_root(cwd)
    if root is None:
        return "allow", ""
    state, err = _load_state(root)
    if state is None:
        return "deny", err
    if not state.get("config", {}).get("hook_enforcement", True):
        return "allow", ""
    aidlc = Path(__file__).resolve().parent / "aidlc.py"
    v = subprocess.run([sys.executable, str(aidlc), "--root", str(root), "verify"],
                       capture_output=True, text=True)
    if v.returncode != 0:
        return "deny", "AI-DLC: `aidlc.py verify` failed, so state or audit log is inconsistent:\n" \
            + (v.stdout + v.stderr).strip()
    staged = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMRD"], root)
    if staged.returncode != 0:
        return "allow", ""
    for name in staged.stdout.splitlines():
        p = (root / name).resolve()
        if name.startswith(".aidlc/") or is_doc(root, p):
            continue
        # At commit time only the inception gates are enforced: committing a finished milestone
        # after its approval is normal, and in-flight work is judged by the milestone state.
        verdict, reason = check_path(root, root, name, state, require_milestone=False)
        if verdict == "deny":
            return verdict, reason + f" (staged: {name})"
    return "allow", ""


def main(argv=None) -> int:
    if os.environ.get("AIDLC_HOOK", "").lower() == "off":
        return 0
    ap = argparse.ArgumentParser(description="AI-DLC gate enforcement")
    ap.add_argument("--git-pre-commit", action="store_true", help="run as a git pre-commit hook")
    ap.add_argument("--check-path", nargs="+", metavar="PATH", help="check whether editing PATH is allowed")
    ap.add_argument("--root", help="directory to start searching for .aidlc/ (default: cwd)")
    args = ap.parse_args(argv)
    cwd = Path(args.root or os.getcwd()).resolve()

    if args.git_pre_commit:
        verdict, reason = git_pre_commit(cwd)
    elif args.check_path:
        root = find_root(cwd)
        verdict, reason = "allow", ""
        if root is not None:
            state, err = _load_state(root)
            if state is None:
                verdict, reason = "deny", err
            elif state.get("config", {}).get("hook_enforcement", True):
                for raw in args.check_path:
                    verdict, reason = check_path(root, cwd, raw, state)
                    if verdict == "deny":
                        break
    else:
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError:
            return 0  # fails open on garbage input; documented in SECURITY.md
        if not isinstance(payload, dict):
            return 0
        verdict, reason = decide(payload)
    if verdict == "deny":
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
