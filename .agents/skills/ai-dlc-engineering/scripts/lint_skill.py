#!/usr/bin/env python3
"""Lint this skill package (for contributors and CI). stdlib only.

Checks: frontmatter validity, SKILL.md size, referenced files exist, no orphan references or
templates, single SKILL.md, scripts compile. Exit 1 on any error.

Usage: python scripts/lint_skill.py [SKILL_DIR]
"""
from __future__ import annotations

import py_compile
import re
import sys
from pathlib import Path

ALLOWED_KEYS = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
SKIP_DIRS = {"__pycache__", "node_modules", ".git", "evals"}


def parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise ValueError("no YAML frontmatter")
    out, key, buf, folded = {}, None, [], False
    for line in m.group(1).splitlines():
        if re.match(r"^[A-Za-z][\w-]*:", line):
            if key is not None:
                out[key] = " ".join(buf).strip() if folded else "\n".join(buf).strip()
            key, rest = line.split(":", 1)
            rest = rest.strip()
            folded = rest in (">", ">-", "|", "|-")
            buf = [] if folded or not rest else [rest.strip("'\"")]
        elif key is not None:
            buf.append(line.strip())
    if key is not None:
        out[key] = " ".join(buf).strip() if folded else "\n".join(buf).strip()
    return out


def main(argv=None) -> int:
    root = Path((argv or sys.argv[1:] or ["."])[0]).resolve()
    errors, warnings = [], []
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        print("ERROR SKILL.md not found")
        return 1
    text = skill_md.read_text(encoding="utf-8")
    try:
        fm = parse_frontmatter(text)
    except ValueError as e:
        print(f"ERROR {e}")
        return 1

    for k in set(fm) - ALLOWED_KEYS:
        errors.append(f"unexpected frontmatter key: {k}")
    name, desc = fm.get("name", ""), fm.get("description", "")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) or len(name) > 64:
        errors.append(f"name '{name}' must be kebab-case, at most 64 chars")
    if not desc:
        errors.append("description missing")
    if len(desc) > 1024:
        errors.append(f"description is {len(desc)} chars (max 1024)")
    if "<" in desc or ">" in desc:
        errors.append("description must not contain angle brackets")
    if len(fm.get("compatibility", "")) > 500:
        errors.append("compatibility over 500 chars")
    n_lines = len(text.splitlines())
    if n_lines > 500:
        errors.append(f"SKILL.md has {n_lines} lines (max 500); move detail into references/")

    skills = [p for p in root.rglob("SKILL.md")
              if not any(part in SKIP_DIRS for part in p.relative_to(root).parts)]
    if len(skills) != 1:
        errors.append(f"expected exactly one SKILL.md, found {len(skills)}")

    # referenced files
    refs_in = {}
    for p in [skill_md, *sorted((root / "references").glob("*.md"))]:
        body = p.read_text(encoding="utf-8")
        for rel in set(re.findall(r"`((?:references|templates|scripts|examples)/[\w./-]+?)`", body)):
            refs_in.setdefault(rel, set()).add(p.name)
            target = root / rel
            if not target.exists() and not rel.endswith("/"):
                errors.append(f"{p.name} references missing file: {rel}")
    for folder in ("references", "templates"):
        for f in sorted((root / folder).glob("*.md")):
            rel = f"{folder}/{f.name}"
            mentioned = rel in refs_in or any(
                f.stem in other.read_text(encoding="utf-8") and other != f
                for other in [skill_md, *(root / "references").glob("*.md")])
            if not mentioned:
                warnings.append(f"orphan file not referenced anywhere: {rel}")

    for py in sorted((root / "scripts").glob("*.py")):
        try:
            py_compile.compile(str(py), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"{py.name} does not compile: {e.msg}")
        if not py.read_text(encoding="utf-8").startswith("#!"):
            warnings.append(f"{py.name} has no shebang")

    for f in sorted((root / "references").glob("*.md")):
        if re.search(r"\bTODO:|\{\{", f.read_text(encoding="utf-8")):
            warnings.append(f"{f.name} contains TODO or a placeholder")

    for w in warnings:
        print("WARN ", w)
    for e in errors:
        print("ERROR", e)
    print(f"{'FAIL' if errors else 'PASS'}: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
