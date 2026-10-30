"""AI-driven code improvement engine with AST pre-validation and working-copy containment.

Enforces strict safety boundaries:
- Proposes targeted code replacements via Ollama.
- Rejects path traversal and changes outside the working directory.
- Preserves the pristine reference repository intact.
- Pre-validates all proposed modifications with ast.parse() before disk writes.
- Generates reviewable unified diffs.
- Never executes model-generated shell commands.
- Never blindly overwrites files with unvalidated output.
"""
from __future__ import annotations

import ast
import hashlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.analysis.code_inspector import CodeFinding
from src.analysis.manifest_scanner import is_safe_relative_path
from src.core.diff_generator import generate_unified_diff
from src.services.ollama_client import OllamaClient, OllamaError

logger = logging.getLogger(__name__)


class ImprovementStatus(str, Enum):
    """Execution status of an AI-generated code improvement."""
    PROPOSED = "PROPOSED"
    APPLIED = "APPLIED"
    REJECTED_SYNTAX_ERROR = "REJECTED_SYNTAX_ERROR"
    REJECTED_SNIPPET_NOT_FOUND = "REJECTED_SNIPPET_NOT_FOUND"
    REJECTED_SECURITY = "REJECTED_SECURITY"
    REJECTED_OLLAMA_ERROR = "REJECTED_OLLAMA_ERROR"
    REJECTED_EMPTY_CHANGE = "REJECTED_EMPTY_CHANGE"


@dataclass(frozen=True)
class ProposedImprovement:
    """A proposed and validated code modification."""
    finding_id: str
    file_path: str                          # Relative path within working directory
    original_snippet: str
    replacement_snippet: str
    explanation: str
    status: ImprovementStatus
    diff: str = ""
    error_message: Optional[str] = None

    @property
    def is_applied(self) -> bool:
        return self.status == ImprovementStatus.APPLIED


@dataclass
class ImprovementBatchResult:
    """Aggregated output of a batch of code improvements."""
    applied_count: int = 0
    rejected_count: int = 0
    improvements: List[ProposedImprovement] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema for Ollama Code Improvement
# ---------------------------------------------------------------------------

IMPROVEMENT_PROMPT_SCHEMA = {
    "type": "object",
    "properties": {
        "original_snippet": {
            "type": "string",
            "description": "The exact character-for-character snippet from the source file to replace.",
        },
        "replacement_snippet": {
            "type": "string",
            "description": "The syntactically valid Python replacement snippet.",
        },
        "explanation": {
            "type": "string",
            "description": "Technical justification for why this change resolves the issue.",
        },
    },
    "required": ["original_snippet", "replacement_snippet", "explanation"],
}


# ---------------------------------------------------------------------------
# Reference Integrity Verification
# ---------------------------------------------------------------------------

def compute_directory_manifest_hash(directory: Path | str) -> Dict[str, str]:
    """Compute sha256 hashes for all files in a directory to detect unexpected mutations."""
    root = Path(directory).resolve()
    hashes: Dict[str, str] = {}
    if not root.is_dir():
        return hashes

    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel_str = str(path.relative_to(root))
            try:
                content = path.read_bytes()
                hashes[rel_str] = hashlib.sha256(content).hexdigest()
            except OSError:
                pass
    return hashes


def verify_reference_preservation(reference_dir: Path | str, baseline_hashes: Dict[str, str]) -> Tuple[bool, List[str]]:
    """Assert that the pristine reference copy has experienced zero file modifications."""
    current_hashes = compute_directory_manifest_hash(reference_dir)
    discrepancies: List[str] = []

    for rel_path, expected_hash in baseline_hashes.items():
        if rel_path not in current_hashes:
            discrepancies.append(f"Reference file deleted: {rel_path}")
        elif current_hashes[rel_path] != expected_hash:
            discrepancies.append(f"Reference file modified: {rel_path}")

    for rel_path in current_hashes:
        if rel_path not in baseline_hashes:
            discrepancies.append(f"Unexpected file created in reference: {rel_path}")

    return len(discrepancies) == 0, discrepancies


# ---------------------------------------------------------------------------
# Code Improver Core
# ---------------------------------------------------------------------------

class CodeImprover:
    """Synthesizes, validates, and applies AI-generated code improvements strictly to the working copy."""

    def __init__(self, ollama_client: OllamaClient, model: str) -> None:
        self.client = ollama_client
        self.model = model

    def generate_fix(
        self,
        finding: CodeFinding,
        file_content: str,
        timeout: float = 45.0,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """Query Ollama for a targeted replacement snippet to resolve a finding.
        
        Returns:
            Tuple of (original_snippet, replacement_snippet, explanation, error_message)
        """
        prompt = (
            f"You are a Senior Python Engineer fixing a code quality finding.\n"
            f"File: {finding.file_path}\n"
            f"Finding: [{finding.severity.value}] {finding.description}\n"
            f"Line Range: {finding.line_range}\n"
            f"Suggested Fix: {finding.suggested_improvement}\n\n"
            f"Full Source Code:\n```python\n{file_content}\n```\n\n"
            "Provide a targeted, minimal fix strictly in JSON.\n"
            "1. 'original_snippet': An EXACT, contiguous block of text from the source code that needs replacement.\n"
            "2. 'replacement_snippet': The syntactically valid Python code to replace it with.\n"
            "3. 'explanation': Technical rationale."
        )

        messages = [
            {"role": "system", "content": "You are a code refactoring specialist. Return only valid JSON."},
            {"role": "user", "content": prompt},
        ]

        try:
            parsed, _ = self.client.chat_json(
                model=self.model,
                messages=messages,
                schema=IMPROVEMENT_PROMPT_SCHEMA,
                options={"temperature": 0.1},
                timeout=timeout,
            )

            orig = parsed.get("original_snippet", "").strip()
            repl = parsed.get("replacement_snippet", "").strip()
            expl = parsed.get("explanation", "").strip()

            if not orig or not repl:
                return None, None, None, "Model returned empty original or replacement snippet."

            return orig, repl, expl, None

        except OllamaError as err:
            return None, None, None, f"Ollama error: {err}"
        except Exception as exc:
            return None, None, None, f"Unexpected error querying Ollama: {exc}"

    def propose_improvement(
        self,
        finding: CodeFinding,
        working_dir: Path | str,
        timeout: float = 45.0,
    ) -> ProposedImprovement:
        """Synthesize, pre-validate with AST, and generate unified diff without writing to disk.
        
        Enforces path containment and AST syntax verification.
        """
        work_root = Path(working_dir).resolve()
        target_path = (work_root / finding.file_path).resolve()

        # 1. Security Check: Path Traversal
        if not is_safe_relative_path(work_root, target_path):
            return ProposedImprovement(
                finding_id=finding.finding_id,
                file_path=finding.file_path,
                original_snippet="",
                replacement_snippet="",
                explanation="Security rejection: file path resolves outside working directory.",
                status=ImprovementStatus.REJECTED_SECURITY,
                error_message="Path traversal attempt blocked.",
            )

        if not target_path.is_file():
            return ProposedImprovement(
                finding_id=finding.finding_id,
                file_path=finding.file_path,
                original_snippet="",
                replacement_snippet="",
                explanation="Target file not found in working copy.",
                status=ImprovementStatus.REJECTED_SECURITY,
                error_message=f"File does not exist: {target_path}",
            )

        try:
            original_content = target_path.read_text(encoding="utf-8")
        except Exception as err:
            return ProposedImprovement(
                finding_id=finding.finding_id,
                file_path=finding.file_path,
                original_snippet="",
                replacement_snippet="",
                explanation=f"Cannot read target file: {err}",
                status=ImprovementStatus.REJECTED_SECURITY,
                error_message=str(err),
            )

        # 2. Query Ollama for fix
        orig_snip, repl_snip, explanation, err_msg = self.generate_fix(
            finding=finding,
            file_content=original_content,
            timeout=timeout,
        )

        if err_msg or not orig_snip or not repl_snip:
            return ProposedImprovement(
                finding_id=finding.finding_id,
                file_path=finding.file_path,
                original_snippet=orig_snip or "",
                replacement_snippet=repl_snip or "",
                explanation=explanation or "Failed to generate fix.",
                status=ImprovementStatus.REJECTED_OLLAMA_ERROR,
                error_message=err_msg or "Model returned empty replacement.",
            )

        # 3. Check snippet existence in source
        if orig_snip not in original_content:
            return ProposedImprovement(
                finding_id=finding.finding_id,
                file_path=finding.file_path,
                original_snippet=orig_snip,
                replacement_snippet=repl_snip,
                explanation="Original snippet not found in source file text.",
                status=ImprovementStatus.REJECTED_SNIPPET_NOT_FOUND,
                error_message="The model's original snippet does not match file contents.",
            )

        if orig_snip == repl_snip:
            return ProposedImprovement(
                finding_id=finding.finding_id,
                file_path=finding.file_path,
                original_snippet=orig_snip,
                replacement_snippet=repl_snip,
                explanation="Replacement snippet is identical to original snippet.",
                status=ImprovementStatus.REJECTED_EMPTY_CHANGE,
                error_message="Zero net changes proposed.",
            )

        # 4. Synthesize proposed content
        proposed_content = original_content.replace(orig_snip, repl_snip, 1)

        # 5. AST Pre-Validation: Ensure proposed file is syntactically valid Python
        try:
            ast.parse(proposed_content, filename=finding.file_path)
        except SyntaxError as syntax_err:
            return ProposedImprovement(
                finding_id=finding.finding_id,
                file_path=finding.file_path,
                original_snippet=orig_snip,
                replacement_snippet=repl_snip,
                explanation="Proposed change generates invalid Python syntax.",
                status=ImprovementStatus.REJECTED_SYNTAX_ERROR,
                error_message=f"AST SyntaxError: {syntax_err.msg} at line {syntax_err.lineno}",
            )

        # 6. Generate unified diff
        diff_str = generate_unified_diff(
            original_content=original_content,
            updated_content=proposed_content,
            fromfile=f"a/{finding.file_path}",
            tofile=f"b/{finding.file_path}",
        )

        return ProposedImprovement(
            finding_id=finding.finding_id,
            file_path=finding.file_path,
            original_snippet=orig_snip,
            replacement_snippet=repl_snip,
            explanation=explanation,
            status=ImprovementStatus.PROPOSED,
            diff=diff_str,
        )

    def commit_improvement(
        self,
        improvement: ProposedImprovement,
        working_dir: Path | str,
    ) -> ProposedImprovement:
        """Apply a pre-validated proposed improvement to disk in the working directory."""
        if improvement.status not in (ImprovementStatus.PROPOSED, ImprovementStatus.APPLIED):
            return improvement

        work_root = Path(working_dir).resolve()
        target_path = (work_root / improvement.file_path).resolve()

        if not is_safe_relative_path(work_root, target_path) or not target_path.is_file():
            return ProposedImprovement(
                finding_id=improvement.finding_id,
                file_path=improvement.file_path,
                original_snippet=improvement.original_snippet,
                replacement_snippet=improvement.replacement_snippet,
                explanation="Target file security error or not found.",
                status=ImprovementStatus.REJECTED_SECURITY,
                error_message="Target file access error during commit.",
            )

        try:
            current_content = target_path.read_text(encoding="utf-8")
        except Exception as err:
            return ProposedImprovement(
                finding_id=improvement.finding_id,
                file_path=improvement.file_path,
                original_snippet=improvement.original_snippet,
                replacement_snippet=improvement.replacement_snippet,
                explanation=f"Cannot read file: {err}",
                status=ImprovementStatus.REJECTED_SECURITY,
                error_message=str(err),
            )

        if improvement.original_snippet not in current_content:
            return ProposedImprovement(
                finding_id=improvement.finding_id,
                file_path=improvement.file_path,
                original_snippet=improvement.original_snippet,
                replacement_snippet=improvement.replacement_snippet,
                explanation="Original snippet no longer found in current file.",
                status=ImprovementStatus.REJECTED_SNIPPET_NOT_FOUND,
                error_message="Original snippet mismatch during commit.",
            )

        new_content = current_content.replace(improvement.original_snippet, improvement.replacement_snippet, 1)

        try:
            ast.parse(new_content, filename=improvement.file_path)
        except SyntaxError as syntax_err:
            return ProposedImprovement(
                finding_id=improvement.finding_id,
                file_path=improvement.file_path,
                original_snippet=improvement.original_snippet,
                replacement_snippet=improvement.replacement_snippet,
                explanation="Syntax error during commit.",
                status=ImprovementStatus.REJECTED_SYNTAX_ERROR,
                error_message=f"AST SyntaxError: {syntax_err.msg}",
            )

        try:
            target_path.write_text(new_content, encoding="utf-8")
        except Exception as write_err:
            return ProposedImprovement(
                finding_id=improvement.finding_id,
                file_path=improvement.file_path,
                original_snippet=improvement.original_snippet,
                replacement_snippet=improvement.replacement_snippet,
                explanation=f"Disk write error: {write_err}",
                status=ImprovementStatus.REJECTED_SECURITY,
                error_message=str(write_err),
            )

        return ProposedImprovement(
            finding_id=improvement.finding_id,
            file_path=improvement.file_path,
            original_snippet=improvement.original_snippet,
            replacement_snippet=improvement.replacement_snippet,
            explanation=improvement.explanation,
            status=ImprovementStatus.APPLIED,
            diff=improvement.diff,
        )

    def apply_improvement(
        self,
        finding: CodeFinding,
        working_dir: Path | str,
        timeout: float = 45.0,
    ) -> ProposedImprovement:
        """Generate, validate, and apply an improvement to a file inside the working directory.
        
        Enforces AST pre-validation and path containment.
        """
        proposed = self.propose_improvement(finding, working_dir, timeout=timeout)
        if proposed.status == ImprovementStatus.PROPOSED:
            return self.commit_improvement(proposed, working_dir)
        return proposed

    def apply_batch(
        self,
        findings: List[CodeFinding],
        working_dir: Path | str,
        timeout: float = 45.0,
    ) -> ImprovementBatchResult:
        """Apply a collection of selected code improvements sequentially to the working copy."""
        results: List[ProposedImprovement] = []
        applied_count = 0
        rejected_count = 0
        warnings: List[str] = []

        for finding in findings:
            imp = self.apply_improvement(finding, working_dir, timeout=timeout)
            results.append(imp)
            if imp.is_applied:
                applied_count += 1
            else:
                rejected_count += 1
                warnings.append(f"Improvement '{finding.finding_id}' ({finding.file_path}) was rejected: {imp.error_message}")

        return ImprovementBatchResult(
            applied_count=applied_count,
            rejected_count=rejected_count,
            improvements=results,
            warnings=warnings,
        )

