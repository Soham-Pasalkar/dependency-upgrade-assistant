"""Unified diff and output synthesis generator for requirements.txt updates."""
from __future__ import annotations

import difflib
from typing import List, Set

from src.core.models import EvaluationResult


def synthesize_updated_requirements(
    evaluations: List[EvaluationResult],
    accepted_line_numbers: Set[int],
) -> str:
    """Synthesize updated requirements text based on user-accepted upgrade selections.
    
    Only lines whose line_number is in accepted_line_numbers and which are valid
    upgrade candidates will be updated. All other lines (comments, blank lines,
    unaccepted candidates, unpinned lines, flags) are preserved byte-for-byte.
    """
    output_lines: List[str] = []

    for eval_result in evaluations:
        line = eval_result.line
        if (
            line.line_number in accepted_line_numbers
            and eval_result.is_upgrade_candidate
            and eval_result.candidate_version
        ):
            output_lines.append(line.reconstruct(new_version=eval_result.candidate_version))
        else:
            output_lines.append(line.reconstruct())

    return "".join(output_lines)


def generate_unified_diff(
    original_content: str,
    updated_content: str,
    fromfile: str = "a/requirements.txt",
    tofile: str = "b/requirements.txt",
) -> str:
    """Generate a standard unified diff between original and proposed requirements content.
    
    Returns an empty string if there are no differences.
    """
    if original_content == updated_content:
        return ""

    original_lines = original_content.splitlines(keepends=True)
    updated_lines = updated_content.splitlines(keepends=True)

    diff = difflib.unified_diff(
        original_lines,
        updated_lines,
        fromfile=fromfile,
        tofile=tofile,
        lineterm="",
    )

    return "".join(diff)
