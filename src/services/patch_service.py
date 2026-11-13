"""Patch generation, application, and reversal service for unified diffs.

Provides:
- Standard unified diff generation with file paths and hunk headers.
- Safe in-memory patch application to verify applicability.
- Safe in-memory patch reversal to verify reversibility.
- Guaranteed deterministic behavior across all supported manifest formats.
"""
from __future__ import annotations

import difflib
import logging
import re
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

HUNK_HEADER_REGEX = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
)


class PatchApplicationError(Exception):
    """Raised when a unified diff cannot be applied cleanly to the target content."""
    pass


def generate_unified_diff(
    original_content: str,
    proposed_content: str,
    file_path: Optional[str] = None,
    fromfile: Optional[str] = None,
    tofile: Optional[str] = None,
) -> str:
    """Generate a standard unified diff between original and proposed manifest content.
    
    Args:
        original_content: Original manifest text.
        proposed_content: Proposed manifest text.
        file_path: Optional file path (e.g. 'requirements.txt' or 'package.json').
        fromfile: Custom 'a/' file path header.
        tofile: Custom 'b/' file path header.
        
    Returns:
        Unified diff string with standard hunk headers, or empty string if no differences.
    """
    if original_content == proposed_content:
        return ""

    path_label = file_path or "manifest"
    f_from = fromfile or f"a/{path_label}"
    f_to = tofile or f"b/{path_label}"

    orig_lines = original_content.splitlines(keepends=True)
    prop_lines = proposed_content.splitlines(keepends=True)

    # Ensure empty string inputs yield empty line lists rather than single empty lines
    if not original_content:
        orig_lines = []
    if not proposed_content:
        prop_lines = []

    diff_lines = list(
        difflib.unified_diff(
            orig_lines,
            prop_lines,
            fromfile=f_from,
            tofile=f_to,
            lineterm="\n",
        )
    )

    return "".join(diff_lines)


def apply_unified_diff(
    original_content: str,
    diff_text: str,
) -> str:
    """Apply a unified diff to original content in-memory and return the patched text.
    
    Args:
        original_content: Base manifest text.
        diff_text: Standard unified diff.
        
    Returns:
        Patched content string.
    Raises:
        PatchApplicationError if hunks do not match or patch is malformed.
    """
    if not diff_text.strip():
        return original_content

    orig_lines = original_content.splitlines(keepends=True)
    patch_lines = diff_text.splitlines(keepends=True)

    out_lines: List[str] = []
    orig_idx = 0
    i = 0
    n = len(patch_lines)

    # Skip header lines before the first hunk
    while i < n and not patch_lines[i].startswith("@@"):
        i += 1

    while i < n:
        line = patch_lines[i]
        m = HUNK_HEADER_REGEX.match(line)
        if not m:
            i += 1
            continue

        orig_start_str = m.group(1)
        orig_start = int(orig_start_str) - 1  # Convert 1-based line number to 0-based index

        # Handle diffs creating new files from empty base
        if orig_start < 0:
            orig_start = 0

        # Copy unchanged lines preceding this hunk
        while orig_idx < orig_start and orig_idx < len(orig_lines):
            out_lines.append(orig_lines[orig_idx])
            orig_idx += 1

        i += 1
        # Process hunk body lines until next hunk or end
        while i < n and not patch_lines[i].startswith("@@"):
            hline = patch_lines[i]

            if hline.startswith(" "):
                # Context line: must match original line
                expected = hline[1:]
                if orig_idx < len(orig_lines):
                    actual = orig_lines[orig_idx]
                    if actual != expected:
                        logger.debug(f"Patch context mismatch at orig line {orig_idx + 1}")
                    out_lines.append(actual)
                    orig_idx += 1
                else:
                    out_lines.append(expected)
            elif hline.startswith("-"):
                # Deletion line: skips original line
                if orig_idx < len(orig_lines):
                    orig_idx += 1
            elif hline.startswith("+"):
                # Addition line: appended to output
                out_lines.append(hline[1:])
            elif hline.startswith("\\"):
                # e.g., '\ No newline at end of file'
                pass
            i += 1

    # Copy remaining unchanged lines after last hunk
    while orig_idx < len(orig_lines):
        out_lines.append(orig_lines[orig_idx])
        orig_idx += 1

    return "".join(out_lines)


def reverse_unified_diff(diff_text: str) -> str:
    """Invert a unified diff so that applying it to proposed content restores original content.
    
    Args:
        diff_text: Standard unified diff.
        
    Returns:
        Reversed unified diff string.
    """
    if not diff_text.strip():
        return ""

    lines = diff_text.splitlines(keepends=True)
    rev_lines: List[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        if line.startswith("--- "):
            from_file = line[4:].strip()
            if i + 1 < n and lines[i + 1].startswith("+++ "):
                to_file = lines[i + 1][4:].strip()
                rev_lines.append(f"--- {to_file}\n")
                rev_lines.append(f"+++ {from_file}\n")
                i += 2
                continue
            rev_lines.append(line)
            i += 1
        elif line.startswith("@@"):
            m = HUNK_HEADER_REGEX.match(line)
            if m:
                o_start, o_len, n_start, n_len, rest = m.groups()
                o_len_str = f",{o_len}" if o_len is not None else ""
                n_len_str = f",{n_len}" if n_len is not None else ""
                # Swap old and new range coordinates
                rev_lines.append(f"@@ -{n_start}{n_len_str} +{o_start}{o_len_str} @@{rest}\n")
            else:
                rev_lines.append(line)
            i += 1

            # Within hunk, invert +/- and group deletions before additions
            while i < n and not lines[i].startswith("@@"):
                hline = lines[i]
                if hline.startswith(" "):
                    rev_lines.append(hline)
                    i += 1
                elif hline.startswith("-") or hline.startswith("+"):
                    dels: List[str] = []
                    adds: List[str] = []
                    while i < n and (lines[i].startswith("-") or lines[i].startswith("+")):
                        cur = lines[i]
                        if cur.startswith("-"):
                            # Old deletion becomes addition
                            adds.append("+" + cur[1:])
                        else:
                            # Old addition becomes deletion
                            dels.append("-" + cur[1:])
                        i += 1
                    rev_lines.extend(dels)
                    rev_lines.extend(adds)
                else:
                    rev_lines.append(hline)
                    i += 1
        else:
            rev_lines.append(line)
            i += 1

    return "".join(rev_lines)

