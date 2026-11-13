#!/usr/bin/env python3
"""Validate AI-DLC artifacts: required sections, filled placeholders, table rows.

stdlib only. Usage:
    python validate_artifacts.py [--root DIR] [--artifact KEY ...] [--id M1]

Exit code 0 = no errors (warnings allowed), 1 = errors.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DOCS = "docs/aidlc"

# rows: table rows whose first cell matches PREFIX-<n>; the listed cells must be non-empty.
ARTIFACTS = {
    "requirements": {
        "path": DOCS + "/requirements.md",
        "sections": ["Intent", "Functional requirements", "Non-functional requirements",
                     "Assumptions", "Out of scope"],
        "rows": [
            {"prefix": "FR", "min": 1, "cells": [2], "labels": ["acceptance criterion"]},
            {"prefix": "NFR", "min": 1, "cells": [2], "labels": ["acceptance criterion"]},
        ],
    },
    "architecture": {
        "path": DOCS + "/architecture.md",
        "sections": ["Overview", "Components", "Data", "Failure modes", "Decision records"],
        "contains": [{"regex": r"\bADR-\d+\b", "min": 1, "label": "decision record (ADR-n)"}],
    },
    "threat-model": {
        "path": DOCS + "/threat-model.md",
        "sections": ["Assets", "Trust boundaries", "Threats"],
        "rows": [{"prefix": "T", "min": 3, "min_tier3": 5, "cells": [4],
                  "labels": ["mitigation"]}],
    },
    "milestone-plan": {
        "path": DOCS + "/milestone-plan.md",
        "sections": ["Milestones", "Test strategy", "Risks"],
        "rows": [{"prefix": "M", "min": 1, "cells": [3, 4],
                  "labels": ["acceptance criteria", "test approach"]}],
    },
    "milestone-report": {
        "path": DOCS + "/reports/{id}.md",
        "sections": ["Summary", "Tests run", "Acceptance criteria", "Known limitations",
                     "Deviations"],
    },
    "completion-report": {
        "path": DOCS + "/completion-report.md",
        "sections": ["Summary", "Verification performed", "Not verified", "Security review",
                     "Performance", "Documentation", "Residual risks",
                     "Release recommendation"],
    },
}

_COMMENT = re.compile(r"<!--.*?-->", re.S)


def artifact_path(root, key, mid=None):
    return Path(root) / ARTIFACTS[key]["path"].format(id=mid or "")


def strip_comments(text: str) -> str:
    """Remove HTML comments but keep line numbering stable."""
    return _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def parse_sections(text: str):
    """Return [(level, title_lower, body)] ignoring headings inside code fences."""
    lines = text.splitlines()
    heads, in_fence = [], False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        m = None if in_fence else re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip().lower()))
    out = []
    for idx, (i, level, title) in enumerate(heads):
        end = len(lines)
        for j, lvl2, _ in heads[idx + 1:]:
            if lvl2 <= level:
                end = j
                break
        out.append((level, title, "\n".join(lines[i + 1:end])))
    return out


def table_rows(text: str):
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            yield n, cells


def check_artifact(root, key, mid=None, tier=2):
    """Return (errors, warnings) for one artifact."""
    spec = ARTIFACTS[key]
    p = artifact_path(root, key, mid)
    rel = spec["path"].format(id=mid or "")
    if not p.is_file():
        return [f"missing artifact: {rel}"], []
    raw = p.read_text(encoding="utf-8")
    if not raw.strip():
        return [f"{rel}: file is empty"], []
    text = strip_comments(raw)
    errors, warnings = [], []

    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if "{{" in line:
            errors.append(f"{rel}:{n}: unfilled placeholder")
        elif re.search(r"\bTODO\b", line):
            errors.append(f"{rel}:{n}: TODO left in artifact")
        elif re.search(r"\bTBD\b", line):
            warnings.append(f"{rel}:{n}: TBD present; resolve or move to open questions")

    sections = parse_sections(text)
    for want in spec.get("sections", []):
        w = want.lower()
        hit = [s for s in sections if s[1].startswith(w)]
        if not hit:
            errors.append(f"{rel}: missing section '{want}'")
        elif not hit[0][2].strip():
            errors.append(f"{rel}: section '{want}' is empty")

    for rule in spec.get("rows", []):
        pat = re.compile(rf"^{rule['prefix']}-?\d+$", re.I)
        found = 0
        for n, cells in table_rows(text):
            if cells and pat.match(cells[0]):
                found += 1
                for idx, label in zip(rule["cells"], rule["labels"]):
                    if len(cells) <= idx or len(cells[idx]) < 3:
                        errors.append(f"{rel}:{n}: {cells[0]} has no {label}")
        need = rule.get("min_tier3", rule["min"]) if tier >= 3 else rule["min"]
        if found < need:
            errors.append(f"{rel}: needs at least {need} {rule['prefix']}-n row(s), found {found}")

    for rule in spec.get("contains", []):
        if len(re.findall(rule["regex"], text)) < rule["min"]:
            errors.append(f"{rel}: needs at least {rule['min']} {rule['label']}")
    return errors, warnings


def check_plan_against_state(root, state):
    """Every milestone in state must appear in the plan, and vice versa."""
    p = artifact_path(root, "milestone-plan")
    if not p.is_file():
        return ["missing artifact: " + ARTIFACTS["milestone-plan"]["path"]]
    in_plan = set()
    for _, cells in table_rows(strip_comments(p.read_text(encoding="utf-8"))):
        if cells and re.match(r"^M\d+$", cells[0]):
            in_plan.add(cells[0])
    in_state = {m["id"] for m in state.get("milestones", [])}
    errs = []
    for mid in sorted(in_state - in_plan):
        errs.append(f"milestone {mid} is registered but missing from milestone-plan.md")
    for mid in sorted(in_plan - in_state):
        errs.append(f"milestone {mid} is in milestone-plan.md but not registered (milestone add)")
    return errs


def _load_state(root):
    p = Path(root) / ".aidlc" / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--artifact", action="append", choices=sorted(ARTIFACTS))
    ap.add_argument("--id", help="milestone id for milestone-report")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    state = _load_state(root)
    tier = (state or {}).get("tier", 2)
    keys = args.artifact or [k for k in ARTIFACTS
                             if k != "milestone-report" and artifact_path(root, k).is_file()]
    all_err, all_warn = [], []
    for k in keys:
        e, w = check_artifact(root, k, args.id, tier)
        all_err += e
        all_warn += w
    if state and "milestone-plan" in keys:
        all_err += check_plan_against_state(root, state)
    for w in all_warn:
        print("WARN ", w)
    for e in all_err:
        print("ERROR", e)
    if not keys:
        print("no artifacts found under docs/aidlc/")
    print(f"{'FAIL' if all_err else 'PASS'}: {len(all_err)} error(s), {len(all_warn)} warning(s)")
    return 1 if all_err else 0


if __name__ == "__main__":
    sys.exit(main())
