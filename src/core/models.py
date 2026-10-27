"""Domain models and dataclasses for Dependency Upgrade Assistant."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class LineType(str, Enum):
    """Categorization of lines found within a requirements.txt file."""
    EMPTY = "empty"
    COMMENT = "comment"
    PIP_FLAG = "pip_flag"                     # e.g., -r, -i, -e, --extra-index-url
    UNPARSEABLE = "unparseable"               # Malformed or unsupported syntax
    EXACT_PIN = "exact_pin"                   # package==version (Eligible for upgrade proposal)
    NON_PINNED_SPECIFIER = "non_pinned"       # e.g., >=, <=, ~=, >, <, compound constraints (Preserved)
    BARE_PACKAGE = "bare_package"             # e.g., package without version (Preserved)


class UpgradeStatus(str, Enum):
    """Evaluation status for a requirements line against the local catalog."""
    UPGRADE_AVAILABLE = "Upgrade Available"
    UP_TO_DATE = "Up to Date"
    UNKNOWN_PACKAGE = "Unknown Package (Local Catalog)"
    PRESERVED_NON_PIN = "Preserved (Non-Pinned / Compound)"
    PRESERVED_PASSTHROUGH = "Preserved (Comment / Flag / Unparseable)"


@dataclass
class RequirementLine:
    """Represents a single parsed line from a requirements.txt file."""
    line_number: int
    raw_text: str
    line_type: LineType
    package_name: Optional[str] = None
    extras: Optional[str] = None                    # e.g., "[security]"
    operator: Optional[str] = None                  # "==" for exact pin, or ">=", etc.
    version: Optional[str] = None                   # e.g., "2.28.0"
    environment_marker: Optional[str] = None        # e.g., 'python_version < "3.11"'
    inline_comment: Optional[str] = None            # e.g., "# needed for api"
    leading_whitespace: str = ""
    newline: str = "\n"

    def reconstruct(self, new_version: Optional[str] = None) -> str:
        """Reconstruct the line text, applying new_version if provided and eligible.
        
        Preserves leading whitespace, extras, environment markers, inline comments,
        and original newline endings.
        """
        # If not an exact pin or no new version provided, return raw text exactly
        if self.line_type != LineType.EXACT_PIN or new_version is None or self.package_name is None:
            return self.raw_text

        ver_to_use = new_version
        parts = [self.leading_whitespace, self.package_name]
        if self.extras:
            parts.append(self.extras)
        parts.append(f"=={ver_to_use}")
        if self.environment_marker:
            parts.append(f"; {self.environment_marker}")
        if self.inline_comment:
            parts.append(f" {self.inline_comment}")
        parts.append(self.newline)
        return "".join(parts)


@dataclass
class EvaluationResult:
    """Evaluation of a requirement line against the local package catalog."""
    line: RequirementLine
    status: UpgradeStatus
    current_version: Optional[str] = None
    candidate_version: Optional[str] = None
    status_message: str = ""
    is_upgrade_candidate: bool = False
    package_description: Optional[str] = None

    @property
    def display_package_name(self) -> str:
        if self.line.package_name:
            extras_str = self.line.extras or ""
            return f"{self.line.package_name}{extras_str}"
        return self.line.raw_text.strip()
