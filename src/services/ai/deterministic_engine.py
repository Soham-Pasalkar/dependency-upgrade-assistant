"""Deterministic rule-based recommendation engine for offline and fallback operation."""
from __future__ import annotations

from typing import List, Optional, Set, Tuple

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version as PyVersion

from src.analysis.semver_evaluator import SemVer, semver_satisfies
from src.models.manifest import AnalyzedDependency, DependencyStatus, Ecosystem
from src.models.recommendation import (
    ModelMetadata,
    PackageUpgradeRecommendation,
    RecommendationCategory,
    RecommendationPriority,
    ValidationStatus,
)
from src.services.ai.base import AIRecommendationProvider


class DeterministicRecommendationEngine(AIRecommendationProvider):
    """Deterministic, zero-hallucination upgrade recommendation engine.
    
    Uses pure semantic versioning arithmetic, registry metadata, and declared constraints
    to generate safe, structured upgrade recommendations when local AI daemon is offline
    or when deterministic operation is explicitly enforced.
    
    Grounded Principles:
    1. Prefer latest_compatible_version only when it is present, valid, and strictly newer.
    2. Never substitute latest_version blindly for a missing compatible range candidate.
    3. If the candidate is missing, invalid, or identical to current: return safe no-upgrade.
    4. Exact pin bumps are treated as out-of-range targets requiring CAUTION and M4 pin updates.
    5. Downgrades are strictly prevented under all conditions.
    """

    @property
    def provider_id(self) -> str:
        return "deterministic"

    def check_availability(self) -> Tuple[bool, str]:
        return True, "Deterministic rule-based engine active (always available)"

    def list_models(self) -> List[ModelMetadata]:
        return [
            ModelMetadata(
                name="deterministic-semver",
                model="rule-based",
                size_bytes=0,
                family="rules",
                parameter_size="0B",
                display_label="Deterministic SemVer Engine (Rule-based, Grounded Metadata)",
            )
        ]

    def generate_recommendations(
        self,
        dependencies: List[AnalyzedDependency],
        model: Optional[str] = None,
    ) -> List[PackageUpgradeRecommendation]:
        """Generate structured upgrade recommendations deterministically."""
        recommendations: List[PackageUpgradeRecommendation] = []

        for dep in dependencies:
            rec = self._evaluate_dependency(dep)
            recommendations.append(rec)

        return recommendations

    def _evaluate_dependency(self, dep: AnalyzedDependency) -> PackageUpgradeRecommendation:
        """Evaluate a single analyzed dependency into a structured recommendation."""
        # 1. Preserved / Unsupported
        if dep.status == DependencyStatus.PRESERVED:
            return PackageUpgradeRecommendation(
                package_name=dep.name,
                ecosystem=dep.ecosystem,
                current_version=dep.current_version,
                declared_specifier=dep.declared_specifier,
                latest_registry_version=dep.latest_version,
                candidate_version=None,
                category=RecommendationCategory.PRESERVED_UNSUPPORTED,
                priority=RecommendationPriority.LOW,
                explanation=dep.status_reason or dep.diagnostic_message or f"Dependency '{dep.name}' is preserved without automated upgrade.",
                breaking_changes_summary=None,
                confidence_score=1.0,
                provider_used=self.provider_id,
                model_used=None,
                is_ai_generated=False,
                validation_status=ValidationStatus.SKIPPED,
                raw_line=dep.raw_line,
                line_number=dep.line_number,
            )

        # 2. Already Up to Date
        if dep.status == DependencyStatus.UP_TO_DATE:
            return PackageUpgradeRecommendation(
                package_name=dep.name,
                ecosystem=dep.ecosystem,
                current_version=dep.current_version,
                declared_specifier=dep.declared_specifier,
                latest_registry_version=dep.latest_version,
                candidate_version=dep.current_version,
                category=RecommendationCategory.NO_UPGRADE_NEEDED,
                priority=RecommendationPriority.LOW,
                explanation=f"Package '{dep.name}' is already up to date with latest release ({dep.latest_version or dep.current_version}).",
                breaking_changes_summary=None,
                confidence_score=1.0,
                provider_used=self.provider_id,
                model_used=None,
                is_ai_generated=False,
                validation_status=ValidationStatus.SKIPPED,
                raw_line=dep.raw_line,
                line_number=dep.line_number,
            )

        # 3. Upgrade Available: Determine candidate grounded in M2 evidence
        specifier_str = dep.declared_specifier.strip() if dep.declared_specifier else ""
        # Check if declared specifier contains range operators
        is_range = any(op in specifier_str for op in ("^", "~", "<", ">", "!="))

        known_versions: Set[str] = set(dep.available_versions)
        if dep.latest_version:
            known_versions.add(dep.latest_version)
        if dep.latest_compatible_version:
            known_versions.add(dep.latest_compatible_version)

        candidate: Optional[str] = None
        category: RecommendationCategory = RecommendationCategory.RECOMMENDED
        priority: RecommendationPriority = RecommendationPriority.LOW
        explanation: str = ""
        breaking_changes: Optional[str] = None

        if is_range:
            # Range constraint: strictly prefer latest_compatible_version. Never substitute latest_version blindly.
            cand_cand = dep.latest_compatible_version
            if cand_cand and cand_cand in known_versions and self._is_strictly_newer(dep.ecosystem, dep.current_version, cand_cand):
                candidate = cand_cand
                category, priority, explanation, breaking_changes = self._classify_upgrade(dep, candidate)
            else:
                # No compatible upgrade found in range
                return PackageUpgradeRecommendation(
                    package_name=dep.name,
                    ecosystem=dep.ecosystem,
                    current_version=dep.current_version,
                    declared_specifier=dep.declared_specifier,
                    latest_registry_version=dep.latest_version,
                    candidate_version=None,
                    category=RecommendationCategory.NO_UPGRADE_NEEDED,
                    priority=RecommendationPriority.LOW,
                    explanation=(
                        f"No compatible upgrade candidate found satisfying constraint '{dep.declared_specifier}'. "
                        f"Current version ({dep.current_version or 'resolved'}) is at latest compatible release."
                    ),
                    breaking_changes_summary=None,
                    confidence_score=1.0,
                    provider_used=self.provider_id,
                    model_used=None,
                    is_ai_generated=False,
                    validation_status=ValidationStatus.FALLBACK_DETERMINISTIC,
                    raw_line=dep.raw_line,
                    line_number=dep.line_number,
                )
        else:
            # Exact pin or unpinned: target is latest_version
            cand_cand = dep.latest_version or dep.latest_compatible_version
            if cand_cand and cand_cand in known_versions and self._is_strictly_newer(dep.ecosystem, dep.current_version, cand_cand):
                candidate = cand_cand
                category, priority, explanation, breaking_changes = self._classify_upgrade(dep, candidate)
                if specifier_str.startswith("==") or specifier_str.startswith("==="):
                    explanation = f"Exact pin bump from '{dep.current_version}' to '{candidate}'. Requires manifest pin update in M4. {explanation}".strip()
            else:
                return PackageUpgradeRecommendation(
                    package_name=dep.name,
                    ecosystem=dep.ecosystem,
                    current_version=dep.current_version,
                    declared_specifier=dep.declared_specifier,
                    latest_registry_version=dep.latest_version,
                    candidate_version=dep.current_version,
                    category=RecommendationCategory.NO_UPGRADE_NEEDED,
                    priority=RecommendationPriority.LOW,
                    explanation=f"Dependency '{dep.name}' has no verified newer release in registry metadata.",
                    breaking_changes_summary=None,
                    confidence_score=1.0,
                    provider_used=self.provider_id,
                    model_used=None,
                    is_ai_generated=False,
                    validation_status=ValidationStatus.SKIPPED,
                    raw_line=dep.raw_line,
                    line_number=dep.line_number,
                )

        return PackageUpgradeRecommendation(
            package_name=dep.name,
            ecosystem=dep.ecosystem,
            current_version=dep.current_version,
            declared_specifier=dep.declared_specifier,
            latest_registry_version=dep.latest_version,
            candidate_version=candidate,
            category=category,
            priority=priority,
            explanation=explanation,
            breaking_changes_summary=breaking_changes,
            confidence_score=1.0,
            provider_used=self.provider_id,
            model_used=None,
            is_ai_generated=False,
            validation_status=ValidationStatus.FALLBACK_DETERMINISTIC,
            raw_line=dep.raw_line,
            line_number=dep.line_number,
        )

    def _is_strictly_newer(self, ecosystem: Ecosystem, current_str: Optional[str], candidate_str: str) -> bool:
        """Check if candidate is strictly newer than current version (preventing downgrades)."""
        if not current_str:
            return True

        if ecosystem == Ecosystem.PYTHON:
            try:
                curr_v = PyVersion(current_str)
                cand_v = PyVersion(candidate_str)
                # Disallow prereleases if current is stable
                if cand_v.is_prerelease and not curr_v.is_prerelease:
                    return False
                return cand_v > curr_v
            except InvalidVersion:
                return False
        else:  # NPM
            curr_s = SemVer.parse(current_str)
            cand_s = SemVer.parse(candidate_str)
            if curr_s is None or cand_s is None:
                return False
            if cand_s.is_prerelease() and not curr_s.is_prerelease():
                return False
            return cand_s > curr_s

    def _classify_upgrade(
        self,
        dep: AnalyzedDependency,
        candidate: str,
    ) -> Tuple[RecommendationCategory, RecommendationPriority, str, Optional[str]]:
        """Classify an upgrade candidate by SemVer bump magnitude."""
        curr = dep.current_version or ""
        is_major, is_minor, _ = self._compare_versions(dep.ecosystem, curr, candidate)

        if dep.yanked_or_deprecated:
            reason = dep.deprecation_reason or "Marked as yanked on PyPI or deprecated on npm"
            return (
                RecommendationCategory.CAUTION,
                RecommendationPriority.CRITICAL,
                f"Package '{dep.name}' is deprecated or yanked upstream ({reason}). Upgrade to '{candidate}' is urgently recommended in M4.",
                f"Critical advisory: {reason}. Review migration guide.",
            )

        if is_major:
            return (
                RecommendationCategory.CAUTION,
                RecommendationPriority.HIGH,
                f"Major version bump for '{dep.name}' from {curr} to {candidate}. Requires breaking change testing in M4.",
                f"Major version jump ({curr} -> {candidate}). Backwards-incompatible API changes are likely.",
            )

        if is_minor:
            return (
                RecommendationCategory.RECOMMENDED,
                RecommendationPriority.MEDIUM,
                f"Minor version upgrade for '{dep.name}' from {curr} to {candidate}.",
                None,
            )

        return (
            RecommendationCategory.RECOMMENDED,
            RecommendationPriority.LOW,
            f"Patch version upgrade for '{dep.name}' from {curr} to {candidate}.",
            None,
        )

    def _compare_versions(
        self,
        ecosystem: Ecosystem,
        current_str: str,
        candidate_str: str,
    ) -> Tuple[bool, bool, bool]:
        """Compare current and candidate versions to determine major, minor, or patch delta."""
        # Try SemVer 2.0 first
        curr_sem = SemVer.parse(current_str)
        cand_sem = SemVer.parse(candidate_str)

        if curr_sem and cand_sem:
            if cand_sem.major > curr_sem.major:
                return True, False, False
            if cand_sem.minor > curr_sem.minor:
                return False, True, False
            if cand_sem.patch > curr_sem.patch:
                return False, False, True
            return False, False, False

        # Try PEP 440 packaging Version for Python
        if ecosystem == Ecosystem.PYTHON:
            try:
                curr_py = PyVersion(current_str)
                cand_py = PyVersion(candidate_str)
                curr_major = curr_py.release[0] if curr_py.release else 0
                cand_major = cand_py.release[0] if cand_py.release else 0
                curr_minor = curr_py.release[1] if len(curr_py.release) > 1 else 0
                cand_minor = cand_py.release[1] if len(cand_py.release) > 1 else 0

                if cand_major > curr_major:
                    return True, False, False
                if cand_minor > curr_minor:
                    return False, True, False
                return False, False, True
            except InvalidVersion:
                pass

        # Fallback heuristic: split by dots
        try:
            curr_parts = [int(p) for p in current_str.split(".") if p.isdigit()]
            cand_parts = [int(p) for p in candidate_str.split(".") if p.isdigit()]
            if curr_parts and cand_parts:
                if cand_parts[0] > curr_parts[0]:
                    return True, False, False
                if len(curr_parts) > 1 and len(cand_parts) > 1 and cand_parts[1] > curr_parts[1]:
                    return False, True, False
                return False, False, True
        except Exception:
            pass

        return False, True, False
