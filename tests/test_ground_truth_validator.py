"""Unit tests for RegistryGroundTruthValidator."""
import pytest

from src.models.manifest import AnalyzedDependency, DependencyStatus, Ecosystem
from src.models.recommendation import (
    PackageUpgradeRecommendation,
    RecommendationCategory,
    RecommendationPriority,
    ValidationStatus,
)
from src.services.ai.validator import RegistryGroundTruthValidator


@pytest.fixture
def validator():
    return RegistryGroundTruthValidator()


def test_validator_accepts_valid_registry_version(validator):
    dep = AnalyzedDependency(
        name="requests",
        original_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.25.0",
        latest_version="2.32.3",
        latest_compatible_version="2.31.0",
        status=DependencyStatus.UPGRADE_AVAILABLE,
        available_versions=["2.25.0", "2.31.0", "2.32.3"],
    )
    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.25.0",
        latest_registry_version="2.32.3",
        candidate_version="2.32.3",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.LOW,
        explanation="Valid upgrade suggested by AI",
        is_ai_generated=True,
        validation_status=ValidationStatus.VALIDATED,
    )

    validated = validator.validate_recommendation(rec, dep)
    assert validated.validation_status == ValidationStatus.VALIDATED
    assert validated.candidate_version == "2.32.3"
    assert validated.is_ai_generated is True


def test_validator_rejects_hallucinated_ai_version(validator):
    dep = AnalyzedDependency(
        name="requests",
        original_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.25.0",
        latest_version="2.32.3",
        latest_compatible_version="2.32.3",
        status=DependencyStatus.UPGRADE_AVAILABLE,
        available_versions=["2.25.0", "2.31.0", "2.32.3"],
    )
    # AI hallucinated version 2.99.0
    rec = PackageUpgradeRecommendation(
        package_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.25.0",
        latest_registry_version="2.32.3",
        candidate_version="2.99.0",
        category=RecommendationCategory.RECOMMENDED,
        priority=RecommendationPriority.LOW,
        explanation="AI hallucinated version 2.99.0",
        is_ai_generated=True,
        validation_status=ValidationStatus.VALIDATED,
    )

    validated = validator.validate_recommendation(rec, dep)
    # Must reject hallucination
    assert validated.validation_status == ValidationStatus.REJECTED_HALLUCINATION
    # Must revert to safe candidate
    assert validated.candidate_version == "2.32.3"
    assert validated.is_ai_generated is False
    assert "[REJECTED HALLUCINATION]" in validated.explanation
    assert "2.99.0" in validated.explanation


def test_validator_skips_up_to_date_and_preserved(validator):
    dep_up = AnalyzedDependency(
        name="flask",
        original_name="flask",
        ecosystem=Ecosystem.PYTHON,
        current_version="3.0.0",
        latest_version="3.0.0",
        status=DependencyStatus.UP_TO_DATE,
    )
    rec_up = PackageUpgradeRecommendation(
        package_name="flask",
        ecosystem=Ecosystem.PYTHON,
        current_version="3.0.0",
        candidate_version="3.0.0",
        category=RecommendationCategory.NO_UPGRADE_NEEDED,
        priority=RecommendationPriority.LOW,
        explanation="Up to date",
        is_ai_generated=False,
    )
    validated_up = validator.validate_recommendation(rec_up, dep_up)
    assert validated_up.validation_status == ValidationStatus.SKIPPED

    dep_pres = AnalyzedDependency(
        name="git-dep",
        original_name="git-dep",
        ecosystem=Ecosystem.PYTHON,
        status=DependencyStatus.PRESERVED,
    )
    rec_pres = PackageUpgradeRecommendation(
        package_name="git-dep",
        ecosystem=Ecosystem.PYTHON,
        candidate_version=None,
        category=RecommendationCategory.PRESERVED_UNSUPPORTED,
        priority=RecommendationPriority.LOW,
        explanation="Preserved",
        is_ai_generated=False,
    )
    validated_pres = validator.validate_recommendation(rec_pres, dep_pres)
    assert validated_pres.validation_status == ValidationStatus.SKIPPED


def test_validator_batch_validation(validator):
    deps = [
        AnalyzedDependency(
            name="valid-pkg",
            original_name="valid-pkg",
            ecosystem=Ecosystem.NPM,
            current_version="1.0.0",
            latest_version="1.2.0",
            status=DependencyStatus.UPGRADE_AVAILABLE,
            available_versions=["1.0.0", "1.1.0", "1.2.0"],
        ),
        AnalyzedDependency(
            name="hallucinated-pkg",
            original_name="hallucinated-pkg",
            ecosystem=Ecosystem.NPM,
            current_version="1.0.0",
            latest_version="1.1.0",
            status=DependencyStatus.UPGRADE_AVAILABLE,
            available_versions=["1.0.0", "1.1.0"],
        ),
    ]

    recs = [
        PackageUpgradeRecommendation(
            package_name="valid-pkg",
            ecosystem=Ecosystem.NPM,
            current_version="1.0.0",
            candidate_version="1.2.0",
            category=RecommendationCategory.RECOMMENDED,
            priority=RecommendationPriority.MEDIUM,
            explanation="Valid",
            is_ai_generated=True,
        ),
        PackageUpgradeRecommendation(
            package_name="hallucinated-pkg",
            ecosystem=Ecosystem.NPM,
            current_version="1.0.0",
            candidate_version="9.9.9",
            category=RecommendationCategory.RECOMMENDED,
            priority=RecommendationPriority.HIGH,
            explanation="Hallucinated",
            is_ai_generated=True,
        ),
    ]

    batch = validator.validate_batch(recs, deps)
    assert len(batch) == 2
    assert batch[0].validation_status == ValidationStatus.VALIDATED
    assert batch[0].candidate_version == "1.2.0"

    assert batch[1].validation_status == ValidationStatus.REJECTED_HALLUCINATION
    assert batch[1].candidate_version == "1.1.0"
