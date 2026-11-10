#!/usr/bin/env python3
"""Lightweight secrets scanner for AI-DLC quality gates. stdlib only.

Scans git-tracked files (or the directory tree if not a git repo) for common credential
patterns. It is a safety net, not a replacement for gitleaks/trufflehog in CI.

Usage: python scan_secrets.py [PATH] [--exclude GLOB ...] [--history]
Suppress a known false positive with the comment marker  aidlc:allow-secret  on the same line.
Exit code 1 when findings exist. Findings are masked in output.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = [
    ("AWS access key id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("AWS secret access key", re.compile(
        r"(?i)aws.{0,20}(secret|sk).{0,20}['\"][0-9a-zA-Z/+]{40}['\"]")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Stripe live key", re.compile(r"\b[sr]k_live_[0-9a-zA-Z]{24,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("Hard-coded credential", re.compile(
        r"(?i)\b(api[_-]?key|secret|passwd|password|token|client[_-]?secret)\b\s*[:=]\s*"
        r"['\"][^'\"\s]{12,}['\"]")),
]
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".aidlc", "dist", "build"}
MAX_BYTES = 1_000_000
ALLOW = "aidlc:allow-secret"
PLACEHOLDER = re.compile(r"(?i)(example|placeholder|changeme|your[_-]?|xxxx|<[^>]+>|\$\{|\{\{)")


def mask(s: str) -> str:
    return s[:4] + "…" + s[-2:] if len(s) > 8 else "…"


def candidate_files(root: Path):
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard"], capture_output=True, check=True).stdout
        for rel in out.decode("utf-8", "replace").split("\0"):
            if rel:
                yield root / rel
        return
    except (OSError, subprocess.CalledProcessError):
        pass
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            yield Path(dp) / f


def scan_text(text: str, name: str):
    findings = []
    for n, line in enumerate(text.splitlines(), 1):
        if ALLOW in line:
            continue
        for label, rx in PATTERNS:
            m = rx.search(line)
            if m and not (label == "Hard-coded credential" and PLACEHOLDER.search(m.group(0))):
                findings.append((name, n, label, mask(m.group(0))))
                break
    return findings


def scan_tree(root: Path, excludes=()):
    findings = []
    for p in candidate_files(root):
        rel = p.relative_to(root).as_posix() if p.is_absolute() else str(p)
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        if any(fnmatch.fnmatch(rel, g) for g in excludes):
            continue
        try:
            if not p.is_file() or p.stat().st_size > MAX_BYTES:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:2048]:
            continue
        findings += scan_text(data.decode("utf-8", "replace"), rel)
    return findings


def scan_history(root: Path):
    try:
        out = subprocess.run(["git", "-C", str(root), "log", "-p", "--all", "--no-color",
                              "--max-count=500"], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    added = "\n".join(l[1:] for l in out.decode("utf-8", "replace").splitlines()
                      if l.startswith("+") and not l.startswith("+++"))
    return [("git-history", n, label, m) for _, n, label, m in scan_text(added, "git-history")]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--history", action="store_true", help="also scan added lines in git history")
    args = ap.parse_args(argv)
    root = Path(args.path).resolve()
    findings = scan_tree(root, args.exclude)
    if args.history:
        findings += scan_history(root)
    for name, n, label, masked in findings:
        print(f"{name}:{n}: {label}: {masked}")
    print(f"{'FAIL' if findings else 'PASS'}: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
