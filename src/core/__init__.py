"""Core business logic for Dependency Upgrade Assistant."""
from src.core.comparator import compare_requirements
from src.core.diff_generator import generate_unified_diff, synthesize_updated_requirements
from src.core.models import (
    EvaluationResult,
    LineType,
    RequirementLine,
    UpgradeStatus,
)
from src.core.parser import parse_requirements_content

__all__ = [
    "LineType",
    "RequirementLine",
    "UpgradeStatus",
    "EvaluationResult",
    "parse_requirements_content",
    "compare_requirements",
    "synthesize_updated_requirements",
    "generate_unified_diff",
]
