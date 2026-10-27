"""Semantic version evaluation engine comparing dependencies against the local catalog."""
from __future__ import annotations

from typing import List
from packaging.version import InvalidVersion, parse as parse_version

from src.core.models import EvaluationResult, LineType, RequirementLine, UpgradeStatus
from src.data.catalog_loader import Catalog


def compare_requirements(lines: List[RequirementLine], catalog: Catalog) -> List[EvaluationResult]:
    """Compare parsed requirement lines against a package catalog using PEP 440 semantics.
    
    In accordance with user decisions:
    - Upgrade candidates are evaluated ONLY for simple exact pins (`==`).
    - All non-pinned specifiers, compound constraints, bare packages, comments,
      flags, and unparseable lines are categorized as preserved without upgrade proposals.
    """
    results: List[EvaluationResult] = []

    for line in lines:
        # 1. Non-exact pin lines: Passthrough or preserved non-pins
        if line.line_type in (LineType.COMMENT, LineType.EMPTY, LineType.PIP_FLAG, LineType.UNPARSEABLE):
            results.append(
                EvaluationResult(
                    line=line,
                    status=UpgradeStatus.PRESERVED_PASSTHROUGH,
                    status_message="Preserved unchanged (comment, flag, option, or unparseable line).",
                    is_upgrade_candidate=False,
                )
            )
            continue

        if line.line_type in (LineType.NON_PINNED_SPECIFIER, LineType.BARE_PACKAGE):
            pkg_name = line.package_name or "Unknown"
            candidate = catalog.get_candidate_version(pkg_name) if line.package_name else None
            pkg_info = catalog.get_package(pkg_name) if line.package_name else None
            results.append(
                EvaluationResult(
                    line=line,
                    status=UpgradeStatus.PRESERVED_NON_PIN,
                    current_version=line.operator,
                    candidate_version=candidate,
                    status_message="Non-pinned or bare requirement preserved unchanged per project policy.",
                    is_upgrade_candidate=False,
                    package_description=pkg_info.description if pkg_info else None,
                )
            )
            continue

        # 2. Exact pin lines (line_type == LineType.EXACT_PIN)
        pkg_name = line.package_name
        current_ver_str = line.version

        if not pkg_name or not current_ver_str:
            results.append(
                EvaluationResult(
                    line=line,
                    status=UpgradeStatus.PRESERVED_PASSTHROUGH,
                    status_message="Missing package name or version.",
                    is_upgrade_candidate=False,
                )
            )
            continue

        pkg_info = catalog.get_package(pkg_name)
        if not pkg_info:
            results.append(
                EvaluationResult(
                    line=line,
                    status=UpgradeStatus.UNKNOWN_PACKAGE,
                    current_version=current_ver_str,
                    candidate_version=None,
                    status_message=f"'{pkg_name}' not present in local prototype catalog.",
                    is_upgrade_candidate=False,
                )
            )
            continue

        candidate_ver_str = pkg_info.candidate_version

        # PEP 440 semantic comparison
        try:
            curr_v = parse_version(current_ver_str)
            cand_v = parse_version(candidate_ver_str)
        except InvalidVersion:
            results.append(
                EvaluationResult(
                    line=line,
                    status=UpgradeStatus.PRESERVED_PASSTHROUGH,
                    current_version=current_ver_str,
                    candidate_version=candidate_ver_str,
                    status_message="Version string is not PEP 440 compliant.",
                    is_upgrade_candidate=False,
                    package_description=pkg_info.description,
                )
            )
            continue

        if cand_v > curr_v:
            results.append(
                EvaluationResult(
                    line=line,
                    status=UpgradeStatus.UPGRADE_AVAILABLE,
                    current_version=current_ver_str,
                    candidate_version=candidate_ver_str,
                    status_message=f"Candidate upgrade available ({candidate_ver_str} > {current_ver_str}).",
                    is_upgrade_candidate=True,
                    package_description=pkg_info.description,
                )
            )
        else:
            results.append(
                EvaluationResult(
                    line=line,
                    status=UpgradeStatus.UP_TO_DATE,
                    current_version=current_ver_str,
                    candidate_version=candidate_ver_str,
                    status_message=f"Already at or ahead of catalog candidate ({current_ver_str} >= {candidate_ver_str}).",
                    is_upgrade_candidate=False,
                    package_description=pkg_info.description,
                )
            )

    return results
