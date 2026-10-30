"""Source code inspector for repository files with AST checks and structured Ollama analysis.

Features:
- Configurable source file discovery (default: 10 files).
- Priority scoring based on entry points, core logic, and dependency-upgrade references.
- Exclusion tracking (reporting inspected vs excluded files with reasons).
- Exclusion of .git, .venv, caches, build files, large files (>100KB), and detected secrets.
- AST-guided static scan for security hotspots, dangerous calls, and code smells.
- Structured AI analysis via Ollama with schema validation.
- Clear distinction between deterministic findings and advisory AI suggestions.
"""
from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.analysis.manifest_scanner import is_safe_relative_path
from src.services.ollama_client import OllamaClient, OllamaError

logger = logging.getLogger(__name__)

DEFAULT_INSPECTION_LIMIT = 10
MAX_INSPECTION_FILE_BYTES = 100 * 1024  # 100 KB limit for AI inspection

# Patterns that suggest presence of credentials, tokens, or private keys
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"(?:api_key|apikey|secret_key|auth_token|access_token|password)[\s]*=[\s]*['\"][a-zA-Z0-9_\-]{16,}['\"]", re.IGNORECASE),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
]


class IssueCategory(str, Enum):
    BUG = "BUG"
    SECURITY = "SECURITY"
    CODE_SMELL = "CODE_SMELL"
    MAINTAINABILITY = "MAINTAINABILITY"
    DEPRECATED_API = "DEPRECATED_API"
    COMPLEXITY = "COMPLEXITY"


class IssueSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFORMATIONAL = "INFORMATIONAL"


@dataclass(frozen=True)
class CodeFinding:
    """A detected bug, security concern, code smell, or maintainability issue."""
    finding_id: str
    file_path: str                          # Relative path within repository
    line_range: str                         # e.g. "12-24" or "45"
    issue_category: IssueCategory
    severity: IssueSeverity
    description: str
    reasoning: str
    suggested_improvement: str
    confidence: str                         # "HIGH", "MEDIUM", "LOW"
    is_ai_generated: bool = False
    requires_human_review: bool = True
    snippet: Optional[str] = None


@dataclass(frozen=True)
class PrioritizedFile:
    """A source file selected for inspection with its priority score."""
    path: Path
    relative_path: str
    priority_score: int
    size_bytes: int
    line_count: int
    reasons: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExcludedFile:
    """A source file excluded from inspection with the explicit reason."""
    relative_path: str
    reason: str


@dataclass
class InspectionReport:
    """Aggregated output of repository source code inspection."""
    inspected_files: List[PrioritizedFile] = field(default_factory=list)
    excluded_files: List[ExcludedFile] = field(default_factory=list)
    findings: List[CodeFinding] = field(default_factory=list)
    total_files_discovered: int = 0
    inspection_limit: int = DEFAULT_INSPECTION_LIMIT
    warnings: List[str] = field(default_factory=list)

    @property
    def total_findings(self) -> int:
        return len(self.findings)

    @property
    def critical_or_high_findings(self) -> List[CodeFinding]:
        return [f for f in self.findings if f.severity in (IssueSeverity.CRITICAL, IssueSeverity.HIGH)]


# ---------------------------------------------------------------------------
# File Prioritization & Discovery
# ---------------------------------------------------------------------------

def contains_likely_secrets(content: str) -> bool:
    """Check if file content matches known private key or hardcoded secret patterns."""
    for pattern in SECRET_PATTERNS:
        if pattern.search(content):
            return True
    return False


def calculate_file_priority(
    rel_path: str,
    content: str,
    upgraded_packages: Optional[Set[str]] = None,
) -> Tuple[int, List[str]]:
    """Compute heuristic priority score for a Python file."""
    score = 0
    reasons: List[str] = []

    path_lower = rel_path.lower()
    base_name = os.path.basename(path_lower)

    # 1. Entry point files
    if base_name in ("app.py", "main.py", "cli.py", "__main__.py", "server.py"):
        score += 50
        reasons.append("Application entrypoint")

    # 2. Core application directory
    if any(prefix in path_lower for prefix in ("src/", "core/", "app/", "lib/")):
        score += 30
        reasons.append("Core application source")

    # 3. Non-test files prioritized over tests
    if "test" in path_lower or "tests/" in path_lower:
        score -= 20
        reasons.append("Test suite file (lower priority)")
    else:
        score += 15

    # 4. Imports or references to identified upgrade candidate packages
    if upgraded_packages:
        for pkg in upgraded_packages:
            norm_pkg = pkg.lower().replace("-", "_")
            if re.search(rf"\b(import {norm_pkg}|from {norm_pkg})\b", content):
                score += 40
                reasons.append(f"Imports candidate dependency '{pkg}'")
                break

    # 5. File size sweet spot (between 30 and 500 lines)
    line_count = len(content.splitlines())
    if 30 <= line_count <= 500:
        score += 10
        reasons.append("Optimal module size")

    return max(0, score), reasons


def discover_and_prioritize_files(
    repo_root: Path | str,
    limit: int = DEFAULT_INSPECTION_LIMIT,
    upgraded_packages: Optional[Set[str]] = None,
    max_file_bytes: int = MAX_INSPECTION_FILE_BYTES,
) -> Tuple[List[PrioritizedFile], List[ExcludedFile], int]:
    """Discover Python files in repo_root, filter out exclusions, and rank by priority.
    
    Returns:
        Tuple of (inspected_files, excluded_files, total_discovered)
    """
    root = Path(repo_root).resolve()
    excluded_dirs = {
        ".git", ".github", ".venv", "venv", "env", "__pycache__",
        ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build",
        ".tox", "node_modules",
    }

    candidates: List[PrioritizedFile] = []
    excluded: List[ExcludedFile] = []
    total_discovered = 0

    for current_dir, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current_dir)
        if not is_safe_relative_path(root, current_path):
            dirs[:] = []
            continue

        # Prune excluded dirs
        dirs[:] = [d for d in dirs if d not in excluded_dirs and not d.endswith(".egg-info")]

        for file_name in files:
            if not file_name.endswith(".py"):
                continue

            total_discovered += 1
            file_path = current_path / file_name

            if not is_safe_relative_path(root, file_path):
                excluded.append(ExcludedFile(file_name, "Symlink or traversal outside repository root"))
                continue

            rel_str = str(file_path.relative_to(root))

            # Check file size
            try:
                size_bytes = file_path.stat().st_size
            except OSError as err:
                excluded.append(ExcludedFile(rel_str, f"Cannot stat file: {err}"))
                continue

            if size_bytes > max_file_bytes:
                excluded.append(
                    ExcludedFile(rel_str, f"File size ({size_bytes // 1024} KB) exceeds maximum {max_file_bytes // 1024} KB")
                )
                continue

            # Check readability and UTF-8 encoding
            try:
                content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                excluded.append(ExcludedFile(rel_str, "Non-UTF-8 binary or corrupt text file"))
                continue
            except OSError as err:
                excluded.append(ExcludedFile(rel_str, f"Read error: {err}"))
                continue

            # Check for secrets
            if contains_likely_secrets(content):
                excluded.append(ExcludedFile(rel_str, "Contains detected secrets or credentials"))
                continue

            # Calculate priority
            score, reasons = calculate_file_priority(rel_str, content, upgraded_packages)
            candidates.append(
                PrioritizedFile(
                    path=file_path,
                    relative_path=rel_str,
                    priority_score=score,
                    size_bytes=size_bytes,
                    line_count=len(content.splitlines()),
                    reasons=reasons,
                )
            )

    # Sort descending by priority score, then by path
    candidates.sort(key=lambda f: (-f.priority_score, f.relative_path))

    inspected = candidates[:limit]
    overflow = candidates[limit:]
    for f in overflow:
        excluded.append(
            ExcludedFile(f.relative_path, f"Exceeded inspection limit of {limit} (priority score {f.priority_score})")
        )

    return inspected, excluded, total_discovered


# ---------------------------------------------------------------------------
# Static AST Inspection
# ---------------------------------------------------------------------------

class StaticASTScanner(ast.NodeVisitor):
    """AST visitor detecting deterministic security and maintainability concerns."""

    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.findings: List[CodeFinding] = []
        self._finding_counter = 0

    def _next_id(self) -> str:
        self._finding_counter += 1
        return f"STATIC-{self._finding_counter:03d}"

    def visit_Call(self, node: ast.Call) -> None:
        # Detect eval() / exec()
        if isinstance(node.func, ast.Name):
            if node.func.id in ("eval", "exec"):
                self.findings.append(
                    CodeFinding(
                        finding_id=self._next_id(),
                        file_path=self.relative_path,
                        line_range=str(node.lineno),
                        issue_category=IssueCategory.SECURITY,
                        severity=IssueSeverity.CRITICAL,
                        description=f"Use of dangerous dynamic execution function '{node.func.id}()'.",
                        reasoning="Executing arbitrary code via eval/exec exposes the application to code injection vulnerabilities.",
                        suggested_improvement="Refactor to use safe deserialization or explicit mapping instead of dynamic evaluation.",
                        confidence="HIGH",
                        is_ai_generated=False,
                        requires_human_review=False,
                    )
                )

        # Detect dangerous subprocess.call(..., shell=True)
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("call", "Popen", "run", "check_call"):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    self.findings.append(
                        CodeFinding(
                            finding_id=self._next_id(),
                            file_path=self.relative_path,
                            line_range=str(node.lineno),
                            issue_category=IssueCategory.SECURITY,
                            severity=IssueSeverity.HIGH,
                            description="Use of subprocess with 'shell=True'.",
                            reasoning="Invoking system shells with shell=True is susceptible to shell injection if arguments are untrusted.",
                            suggested_improvement="Pass command arguments as an array of strings and omit shell=True.",
                            confidence="HIGH",
                            is_ai_generated=False,
                            requires_human_review=False,
                        )
                    )

        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        # Detect bare except: or except Exception: pass
        if node.type is None:
            self.findings.append(
                CodeFinding(
                    finding_id=self._next_id(),
                    file_path=self.relative_path,
                    line_range=str(node.lineno),
                    issue_category=IssueCategory.CODE_SMELL,
                    severity=IssueSeverity.MEDIUM,
                    description="Bare 'except:' clause suppresses all exceptions including SystemExit and KeyboardInterrupt.",
                    reasoning="Catching all exceptions indiscriminately masks critical bugs and prevents clean shutdown.",
                    suggested_improvement="Catch specific exception types (e.g., 'except Exception:' or domain-specific exceptions).",
                    confidence="HIGH",
                    is_ai_generated=False,
                    requires_human_review=False,
                )
            )
        elif len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            self.findings.append(
                CodeFinding(
                    finding_id=self._next_id(),
                    file_path=self.relative_path,
                    line_range=str(node.lineno),
                    issue_category=IssueCategory.CODE_SMELL,
                    severity=IssueSeverity.LOW,
                    description="Silent exception suppression ('except ...: pass').",
                    reasoning="Silently ignoring exceptions makes debugging and error diagnostics difficult.",
                    suggested_improvement="Log the caught exception or handle the error condition gracefully.",
                    confidence="HIGH",
                    is_ai_generated=False,
                    requires_human_review=False,
                )
            )

        self.generic_visit(node)


def scan_file_statically(file_path: Path, relative_path: str) -> List[CodeFinding]:
    """Execute deterministic AST-based code inspection on a single Python file."""
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
        scanner = StaticASTScanner(relative_path=relative_path)
        scanner.visit(tree)
        return scanner.findings
    except SyntaxError as err:
        return [
            CodeFinding(
                finding_id="SYNTAX-001",
                file_path=relative_path,
                line_range=str(err.lineno or 1),
                issue_category=IssueCategory.BUG,
                severity=IssueSeverity.CRITICAL,
                description=f"SyntaxError in source file: {err.msg}",
                reasoning="The source file contains invalid Python syntax and cannot be compiled or executed.",
                suggested_improvement="Correct the syntax error.",
                confidence="HIGH",
                is_ai_generated=False,
                requires_human_review=False,
            )
        ]
    except Exception as exc:
        logger.warning(f"Error during static scan of {relative_path}: {exc}")
        return []


# ---------------------------------------------------------------------------
# Structured AI Code Inspection via Ollama
# ---------------------------------------------------------------------------

AI_INSPECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "line_range": {"type": "string"},
                    "issue_category": {
                        "type": "string",
                        "enum": ["BUG", "SECURITY", "CODE_SMELL", "MAINTAINABILITY", "DEPRECATED_API", "COMPLEXITY"],
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL"],
                    },
                    "description": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "suggested_improvement": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["HIGH", "MEDIUM", "LOW"],
                    },
                },
                "required": [
                    "finding_id", "line_range", "issue_category",
                    "severity", "description", "reasoning", "suggested_improvement",
                ],
            },
        }
    },
    "required": ["findings"],
}


def scan_file_with_ollama(
    client: OllamaClient,
    model: str,
    file_path: Path,
    relative_path: str,
    upgraded_packages: Optional[Set[str]] = None,
    timeout: float = 45.0,
) -> List[CodeFinding]:
    """Query Ollama for qualitative code inspection and bug detection on a Python source file."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as err:
        logger.warning(f"Failed to read {relative_path} for Ollama scan: {err}")
        return []

    lines = content.splitlines()
    # Number lines for precise LLM line referencing
    numbered_content = "\n".join(f"{idx+1:4d}: {line}" for idx, line in enumerate(lines[:350]))

    dep_context = ""
    if upgraded_packages:
        dep_context = (
            f"\nNote: The project is upgrading dependencies: {', '.join(sorted(upgraded_packages))}. "
            "Pay close attention to calls or imports using these packages."
        )

    prompt = (
        f"You are a Senior Python Software Architect performing code inspection on '{relative_path}'.\n"
        f"Source code (first {min(len(lines), 350)} lines with line numbers):\n\n"
        f"{numbered_content}\n"
        f"{dep_context}\n\n"
        "Identify potential bugs, security concerns, code smells, poor error handling, "
        "or deprecated API usage.\n"
        "Do not invent fake issues; only report genuine concerns. Answer strictly in JSON."
    )

    messages = [
        {"role": "system", "content": "You are a specialized code quality auditor. Respond strictly in valid JSON."},
        {"role": "user", "content": prompt},
    ]

    try:
        parsed_json, _ = client.chat_json(
            model=model,
            messages=messages,
            schema=AI_INSPECTION_SCHEMA,
            options={"temperature": 0.1},
            timeout=timeout,
        )

        raw_findings = parsed_json.get("findings", [])
        if not isinstance(raw_findings, list):
            return []

        validated_findings: List[CodeFinding] = []
        for idx, item in enumerate(raw_findings):
            if not isinstance(item, dict):
                continue

            cat_str = str(item.get("issue_category", "CODE_SMELL")).upper()
            try:
                category = IssueCategory(cat_str)
            except ValueError:
                category = IssueCategory.CODE_SMELL

            sev_str = str(item.get("severity", "MEDIUM")).upper()
            try:
                severity = IssueSeverity(sev_str)
            except ValueError:
                severity = IssueSeverity.MEDIUM

            conf = str(item.get("confidence", "MEDIUM")).upper()
            fid = item.get("finding_id") or f"AI-{idx+1:03d}"

            validated_findings.append(
                CodeFinding(
                    finding_id=f"AI-{fid}",
                    file_path=relative_path,
                    line_range=str(item.get("line_range", "unknown")),
                    issue_category=category,
                    severity=severity,
                    description=str(item.get("description", "Potential code quality issue.")),
                    reasoning=str(item.get("reasoning", "")),
                    suggested_improvement=str(item.get("suggested_improvement", "")),
                    confidence=conf if conf in ("HIGH", "MEDIUM", "LOW") else "MEDIUM",
                    is_ai_generated=True,
                    requires_human_review=True,
                )
            )

        return validated_findings

    except OllamaError as err:
        logger.info(f"Ollama inspection unavailable for {relative_path}: {err}")
        return []
    except Exception as exc:
        logger.warning(f"Unexpected error during Ollama scan of {relative_path}: {exc}")
        return []


# ---------------------------------------------------------------------------
# Code Inspector Coordinator
# ---------------------------------------------------------------------------

class CodeInspector:
    """Coordinates static AST inspection and advisory Ollama analysis across prioritized files."""

    def __init__(
        self,
        ollama_client: Optional[OllamaClient] = None,
        ollama_model: Optional[str] = None,
        inspection_limit: int = DEFAULT_INSPECTION_LIMIT,
        max_file_bytes: int = MAX_INSPECTION_FILE_BYTES,
    ) -> None:
        self.ollama_client = ollama_client
        self.ollama_model = ollama_model
        self.inspection_limit = max(1, int(inspection_limit))
        self.max_file_bytes = max_file_bytes

    def inspect_repository(
        self,
        repo_dir: Path | str,
        upgraded_packages: Optional[Set[str]] = None,
        enable_ai: bool = False,
    ) -> InspectionReport:
        """Execute full code inspection on the repository workspace.
        
        Args:
            repo_dir: Root directory of working repository copy.
            upgraded_packages: Set of packages with candidate upgrades.
            enable_ai: Whether to query Ollama for AI inspection.
            
        Returns:
            InspectionReport detailing inspected files, excluded files, and findings.
        """
        root = Path(repo_dir).resolve()
        inspected_files, excluded_files, total_discovered = discover_and_prioritize_files(
            repo_root=root,
            limit=self.inspection_limit,
            upgraded_packages=upgraded_packages,
            max_file_bytes=self.max_file_bytes,
        )

        all_findings: List[CodeFinding] = []
        warnings: List[str] = []

        for p_file in inspected_files:
            # 1. Deterministic AST static scan
            static_findings = scan_file_statically(p_file.path, p_file.relative_path)
            all_findings.extend(static_findings)

            # 2. Ollama AI scan (if enabled and client available)
            if enable_ai and self.ollama_client and self.ollama_model:
                ai_findings = scan_file_with_ollama(
                    client=self.ollama_client,
                    model=self.ollama_model,
                    file_path=p_file.path,
                    relative_path=p_file.relative_path,
                    upgraded_packages=upgraded_packages,
                )
                all_findings.extend(ai_findings)

        return InspectionReport(
            inspected_files=inspected_files,
            excluded_files=excluded_files,
            findings=all_findings,
            total_files_discovered=total_discovered,
            inspection_limit=self.inspection_limit,
            warnings=warnings,
        )

