"""Parser for requirements.txt files with lossless line preservation."""
from __future__ import annotations

import re
from typing import List, Optional, Tuple
from packaging.version import InvalidVersion, parse as parse_version

from src.core.models import LineType, RequirementLine

# PEP 508 compliant package name pattern
PACKAGE_NAME_REGEX = r"([A-Za-z0-9][A-Za-z0-9._-]*)"

# Regex matching simple exact pins: package[extras]==version; marker # comment
EXACT_PIN_PATTERN = re.compile(
    r"^([ \t]*)"                                   # 1: leading whitespace
    rf"{PACKAGE_NAME_REGEX}"                       # 2: package name
    r"(\[[^\]]*\])?"                               # 3: optional extras, e.g. [security]
    r"[ \t]*(==)[ \t]*"                            # 4: operator "=="
    r"([A-Za-z0-9_.\-+]+)"                         # 5: version string
    r"(?:[ \t]*;[ \t]*([^#\r\n]+?))?"              # 6: optional environment marker
    r"(?:[ \t]*(#[^\r\n]*))?"                      # 7: optional inline comment
    r"$"
)

# Regex matching non-pinned or compound specifiers (e.g., >=, <=, ~=, >, <, !=, ===)
NON_PIN_PATTERN = re.compile(
    r"^([ \t]*)"                                   # 1: leading whitespace
    rf"{PACKAGE_NAME_REGEX}"                       # 2: package name
    r"(\[[^\]]*\])?"                               # 3: optional extras
    r"[ \t]*([><=~!][><=~0-9A-Za-z_.,\-+* \t]*)"  # 4: non-pinned or compound specifiers
    r"(?:[ \t]*;[ \t]*([^#\r\n]+?))?"              # 5: optional environment marker
    r"(?:[ \t]*(#[^\r\n]*))?"                      # 6: optional inline comment
    r"$"
)

# Regex matching bare package names without specifiers: package or package[extras]
BARE_PACKAGE_PATTERN = re.compile(
    r"^([ \t]*)"                                   # 1: leading whitespace
    rf"{PACKAGE_NAME_REGEX}"                       # 2: package name
    r"(\[[^\]]*\])?"                               # 3: optional extras
    r"(?:[ \t]*;[ \t]*([^#\r\n]+?))?"              # 4: optional environment marker
    r"(?:[ \t]*(#[^\r\n]*))?"                      # 5: optional inline comment
    r"$"
)


def _detect_newline(raw_line: str) -> Tuple[str, str]:
    """Split line text and its line-ending characters ('\\r\\n', '\\n', or '')."""
    if raw_line.endswith("\r\n"):
        return raw_line[:-2], "\r\n"
    if raw_line.endswith("\n"):
        return raw_line[:-1], "\n"
    if raw_line.endswith("\r"):
        return raw_line[:-1], "\r"
    return raw_line, ""


def parse_line(raw_line: str, line_number: int) -> RequirementLine:
    """Parse a single line from a requirements file into a RequirementLine object.
    
    Ensures that any unrecognized, non-pinned, or malformed lines are preserved
    safely as non-destructive passthrough representations.
    """
    body, newline = _detect_newline(raw_line)
    stripped = body.strip()

    # 1. Empty lines or pure whitespace
    if not stripped:
        return RequirementLine(
            line_number=line_number,
            raw_text=raw_line,
            line_type=LineType.EMPTY,
            newline=newline,
        )

    # 2. Comment lines
    if stripped.startswith("#"):
        return RequirementLine(
            line_number=line_number,
            raw_text=raw_line,
            line_type=LineType.COMMENT,
            newline=newline,
        )

    # 3. Pip flags, options, editable installs, URLs, or VCS repositories
    if stripped.startswith("-") or "://" in stripped or stripped.startswith(("git+", "hg+", "svn+", "bzr+")):
        return RequirementLine(
            line_number=line_number,
            raw_text=raw_line,
            line_type=LineType.PIP_FLAG,
            newline=newline,
        )

    # 4. Check for simple exact pins (==)
    pin_match = EXACT_PIN_PATTERN.match(body)
    if pin_match:
        leading_ws, pkg, extras, op, ver, marker, comment = pin_match.groups()
        # Wildcard pins like ==2.28.* are prefix matching, not exact pins
        if "*" not in ver:
            try:
                # Validate that the version string is a valid PEP 440 version
                parse_version(ver)
                return RequirementLine(
                    line_number=line_number,
                    raw_text=raw_line,
                    line_type=LineType.EXACT_PIN,
                    package_name=pkg,
                    extras=extras,
                    operator="==",
                    version=ver,
                    environment_marker=marker.strip() if marker else None,
                    inline_comment=comment.strip() if comment else None,
                    leading_whitespace=leading_ws or "",
                    newline=newline,
                )
            except InvalidVersion:
                # If version string is malformed (e.g. requests==alpha..), treat as unparseable
                return RequirementLine(
                    line_number=line_number,
                    raw_text=raw_line,
                    line_type=LineType.UNPARSEABLE,
                    package_name=pkg,
                    newline=newline,
                )

    # 5. Check for non-pinned specifiers (>=, <=, ~=, compound, wildcards)
    non_pin_match = NON_PIN_PATTERN.match(body)
    if non_pin_match:
        leading_ws, pkg, extras, spec, marker, comment = non_pin_match.groups()
        return RequirementLine(
            line_number=line_number,
            raw_text=raw_line,
            line_type=LineType.NON_PINNED_SPECIFIER,
            package_name=pkg,
            extras=extras,
            operator=spec.strip(),
            environment_marker=marker.strip() if marker else None,
            inline_comment=comment.strip() if comment else None,
            leading_whitespace=leading_ws or "",
            newline=newline,
        )

    # 6. Check for bare package names without constraints (e.g. requests)
    bare_match = BARE_PACKAGE_PATTERN.match(body)
    if bare_match:
        leading_ws, pkg, extras, marker, comment = bare_match.groups()
        return RequirementLine(
            line_number=line_number,
            raw_text=raw_line,
            line_type=LineType.BARE_PACKAGE,
            package_name=pkg,
            extras=extras,
            environment_marker=marker.strip() if marker else None,
            inline_comment=comment.strip() if comment else None,
            leading_whitespace=leading_ws or "",
            newline=newline,
        )

    # 7. Unparseable or unsupported syntax
    return RequirementLine(
        line_number=line_number,
        raw_text=raw_line,
        line_type=LineType.UNPARSEABLE,
        newline=newline,
    )


def parse_requirements_content(content: str) -> List[RequirementLine]:
    """Parse requirements.txt content into a list of RequirementLine objects.
    
    Preserves all lines and original line endings.
    """
    if not content:
        return []

    lines = content.splitlines(keepends=True)
    return [parse_line(line, idx + 1) for idx, line in enumerate(lines)]
