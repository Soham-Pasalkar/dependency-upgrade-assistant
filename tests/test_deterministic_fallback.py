"""Unit tests for the DeterministicRecommendationEngine fallback."""
import pytest

from src.models.manifest import AnalyzedDependency, DependencyStatus, Ecosystem
from src.models.recommendation import (
    RecommendationCategory,
    RecommendationPriority,
    ValidationStatus,
)
from src.services.ai.deterministic_engine import DeterministicRecommendationEngine


@pytest.fixture
def engine():
    return DeterministicRecommendationEngine()


def test_engine_basics(engine):
    assert engine.provider_id == "deterministic"
    is_avail, msg = engine.check_availability()
    assert is_avail is True
    assert "active" in msg

    models = engine.list_models()
    assert len(models) == 1
    assert models[0].name == "deterministic-semver"
    assert models[0].family == "rules"


def test_classify_major_version_bump(engine):
    dep = AnalyzedDependency(
        name="pydantic",
        original_name="pydantic",
        ecosystem=Ecosystem.PYTHON,
        current_version="1.10.12",
        latest_version="2.8.2",
        latest_compatible_version="1.10.12",
        status=DependencyStatus.UPGRADE_AVAILABLE,
        available_versions=["1.10.12", "2.0.0", "2.8.2"],
    )
    recs = engine.generate_recommendations([dep])
    assert len(recs) == 1
    rec = recs[0]
    assert rec.package_name == "pydantic"
    assert rec.category == RecommendationCategory.CAUTION
    assert rec.priority == RecommendationPriority.HIGH
    assert "Major" in rec.explanation or "major" in rec.explanation.lower()
    assert rec.breaking_changes_summary is not None
    assert rec.validation_status == ValidationStatus.FALLBACK_DETERMINISTIC
    assert rec.is_ai_generated is False


def test_classify_minor_version_bump(engine):
    dep = AnalyzedDependency(
        name="fastapi",
        original_name="fastapi",
        ecosystem=Ecosystem.PYTHON,
        current_version="0.100.0",
        latest_version="0.115.0",
        status=DependencyStatus.UPGRADE_AVAILABLE,
        available_versions=["0.100.0", "0.110.0", "0.115.0"],
    )
    recs = engine.generate_recommendations([dep])
    assert len(recs) == 1
    rec = recs[0]
    assert rec.category == RecommendationCategory.RECOMMENDED
    assert rec.priority == RecommendationPriority.MEDIUM
    assert "Minor" in rec.explanation or "minor" in rec.explanation.lower()


def test_classify_patch_version_bump(engine):
    dep = AnalyzedDependency(
        name="requests",
        original_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.32.0",
        latest_version="2.32.3",
        status=DependencyStatus.UPGRADE_AVAILABLE,
        available_versions=["2.32.0", "2.32.1", "2.32.3"],
    )
    recs = engine.generate_recommendations([dep])
    assert len(recs) == 1
    rec = recs[0]
    assert rec.category == RecommendationCategory.RECOMMENDED
    assert rec.priority == RecommendationPriority.LOW
    assert "Patch" in rec.explanation or "patch" in rec.explanation.lower()


def test_classify_yanked_or_deprecated(engine):
    dep = AnalyzedDependency(
        name="old-lib",
        original_name="old-lib",
        ecosystem=Ecosystem.NPM,
        current_version="1.0.0",
        latest_version="2.0.0",
        status=DependencyStatus.UPGRADE_AVAILABLE,
        yanked_or_deprecated=True,
        deprecation_reason="Deprecated: use new-lib instead",
    )
    recs = engine.generate_recommendations([dep])
    assert len(recs) == 1
    rec = recs[0]
    assert rec.category == RecommendationCategory.CAUTION
    assert rec.priority == RecommendationPriority.CRITICAL
    assert "deprecated" in rec.explanation.lower()
    assert "Deprecated: use new-lib instead" in (rec.breaking_changes_summary or "")


def test_classify_up_to_date(engine):
    dep = AnalyzedDependency(
        name="pytest",
        original_name="pytest",
        ecosystem=Ecosystem.PYTHON,
        current_version="8.3.3",
        latest_version="8.3.3",
        status=DependencyStatus.UP_TO_DATE,
    )
    recs = engine.generate_recommendations([dep])
    assert len(recs) == 1
    rec = recs[0]
    assert rec.category == RecommendationCategory.NO_UPGRADE_NEEDED
    assert rec.candidate_version == "8.3.3"
    assert rec.validation_status == ValidationStatus.SKIPPED


def test_classify_preserved(engine):
    dep = AnalyzedDependency(
        name="internal-pkg",
        original_name="internal-pkg",
        ecosystem=Ecosystem.PYTHON,
        status=DependencyStatus.PRESERVED,
        status_reason="Direct VCS URL constraint preserved",
    )
    recs = engine.generate_recommendations([dep])
    assert len(recs) == 1
    rec = recs[0]
    assert rec.category == RecommendationCategory.PRESERVED_UNSUPPORTED
    assert rec.candidate_version is None
    assert rec.validation_status == ValidationStatus.SKIPPED


def test_deterministic_idempotency(engine):
    dep = AnalyzedDependency(
        name="express",
        original_name="express",
        ecosystem=Ecosystem.NPM,
        current_version="4.18.2",
        latest_version="4.19.2",
        status=DependencyStatus.UPGRADE_AVAILABLE,
        available_versions=["4.18.2", "4.19.2"],
    )
    run1 = engine.generate_recommendations([dep])
    run2 = engine.generate_recommendations([dep])
    assert run1[0].model_dump() == run2[0].model_dump()


def test_deterministic_range_constraint_prefers_compatible(engine):
    """Verify range constraints strictly prefer latest_compatible_version and never substitute latest_version."""
    dep = AnalyzedDependency(
        name="urllib3",
        original_name="urllib3",
        ecosystem=Ecosystem.PYTHON,
        current_version="1.26.5",
        declared_specifier="<2.0.0",
        latest_version="2.2.3",
        latest_compatible_version="1.26.18",
        status=DependencyStatus.UPGRADE_AVAILABLE,
        available_versions=["1.26.5", "1.26.18", "2.0.0", "2.2.3"],
    )
    recs = engine.generate_recommendations([dep])
    assert len(recs) == 1
    rec = recs[0]
    assert rec.candidate_version == "1.26.18"
    assert rec.category == RecommendationCategory.RECOMMENDED
    assert rec.priority == RecommendationPriority.LOW  # Patch bump


def test_deterministic_range_constraint_no_compatible_returns_safe_no_upgrade(engine):
    """Verify range constraint with no newer compatible release returns safe no-upgrade without picking latest_version."""
    dep = AnalyzedDependency(
        name="urllib3",
        original_name="urllib3",
        ecosystem=Ecosystem.PYTHON,
        current_version="1.26.18",
        declared_specifier="<2.0.0",
        latest_version="2.2.3",
        latest_compatible_version="1.26.18",
        status=DependencyStatus.UPGRADE_AVAILABLE,
        available_versions=["1.26.5", "1.26.18", "2.0.0", "2.2.3"],
    )
    recs = engine.generate_recommendations([dep])
    assert len(recs) == 1
    rec = recs[0]
    assert rec.candidate_version is None
    assert rec.category == RecommendationCategory.NO_UPGRADE_NEEDED
    assert "No compatible upgrade candidate found" in rec.explanation


def test_deterministic_exact_pin_bump_caution_m4_notice(engine):
    """Verify exact pin bump selects latest_version with CAUTION category and M4 manifest notice."""
    dep = AnalyzedDependency(
        name="requests",
        original_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.25.0",
        declared_specifier="==2.25.0",
        latest_version="2.32.3",
        latest_compatible_version="2.32.3",
        status=DependencyStatus.UPGRADE_AVAILABLE,
        available_versions=["2.25.0", "2.31.0", "2.32.3"],
    )
    recs = engine.generate_recommendations([dep])
    assert len(recs) == 1
    rec = recs[0]
    assert rec.candidate_version == "2.32.3"
    assert rec.category == RecommendationCategory.RECOMMENDED
    assert "The exact pin in the manifest will be updated" in rec.explanation

