"""Unit tests for the provider-independent AI interface and exceptions."""
from typing import List, Optional, Tuple
import pytest

from src.models.manifest import AnalyzedDependency, DependencyStatus, Ecosystem
from src.models.recommendation import (
    ModelMetadata,
    PackageUpgradeRecommendation,
    RecommendationCategory,
    RecommendationPriority,
    ValidationStatus,
)
from src.services.ai.base import (
    AIInferenceTimeoutError,
    AIModelNotFoundError,
    AIProviderError,
    AIProviderUnavailableError,
    AIRecommendationProvider,
    AIResponseParseError,
)


class DummyAIProvider(AIRecommendationProvider):
    """Concrete implementation for interface conformance testing."""

    def __init__(self, is_available: bool = True, models: Optional[List[str]] = None):
        self._available = is_available
        self._models = models or ["mock-model:7b"]

    @property
    def provider_id(self) -> str:
        return "dummy"

    def check_availability(self) -> Tuple[bool, str]:
        if self._available:
            return True, "Dummy AI provider active"
        return False, "Dummy AI provider offline"

    def list_models(self) -> List[ModelMetadata]:
        if not self._available:
            return []
        return [
            ModelMetadata(
                name=m,
                model="dummy-arch",
                size_bytes=1024,
                family="dummy",
                parameter_size="7B",
            )
            for m in self._models
        ]

    def generate_recommendations(
        self,
        dependencies: List[AnalyzedDependency],
        model: Optional[str] = None,
    ) -> List[PackageUpgradeRecommendation]:
        if not self._available:
            raise AIProviderUnavailableError("Dummy provider is unavailable.")
        if model and model not in self._models:
            raise AIModelNotFoundError(f"Model '{model}' not found in dummy provider.")

        results = []
        for dep in dependencies:
            results.append(
                PackageUpgradeRecommendation(
                    package_name=dep.name,
                    ecosystem=dep.ecosystem,
                    current_version=dep.current_version,
                    declared_specifier=dep.declared_specifier,
                    latest_registry_version=dep.latest_version,
                    candidate_version=dep.latest_version or dep.current_version,
                    category=RecommendationCategory.RECOMMENDED,
                    priority=RecommendationPriority.LOW,
                    explanation=f"Dummy recommendation for {dep.name}",
                    provider_used=self.provider_id,
                    model_used=model or self._models[0],
                    is_ai_generated=True,
                    validation_status=ValidationStatus.VALIDATED,
                )
            )
        return results


def test_ai_provider_interface_conformance():
    """Verify that a compliant class can be instantiated and behaves correctly."""
    provider = DummyAIProvider()
    assert provider.provider_id == "dummy"

    is_avail, msg = provider.check_availability()
    assert is_avail is True
    assert "active" in msg

    models = provider.list_models()
    assert len(models) == 1
    assert models[0].name == "mock-model:7b"

    dep = AnalyzedDependency(
        name="requests",
        original_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.25.0",
        latest_version="2.32.3",
        status=DependencyStatus.UPGRADE_AVAILABLE,
    )
    recs = provider.generate_recommendations([dep])
    assert len(recs) == 1
    assert recs[0].package_name == "requests"
    assert recs[0].candidate_version == "2.32.3"
    assert recs[0].is_ai_generated is True


def test_ai_provider_unavailable_error():
    """Verify AIProviderUnavailableError handling."""
    provider = DummyAIProvider(is_available=False)
    is_avail, msg = provider.check_availability()
    assert is_avail is False

    dep = AnalyzedDependency(
        name="requests",
        original_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.25.0",
        status=DependencyStatus.UPGRADE_AVAILABLE,
    )
    with pytest.raises(AIProviderUnavailableError) as exc_info:
        provider.generate_recommendations([dep])
    assert "unavailable" in str(exc_info.value)


def test_ai_model_not_found_error():
    """Verify AIModelNotFoundError handling."""
    provider = DummyAIProvider(is_available=True, models=["model-a:latest"])
    dep = AnalyzedDependency(
        name="requests",
        original_name="requests",
        ecosystem=Ecosystem.PYTHON,
        current_version="2.25.0",
        status=DependencyStatus.UPGRADE_AVAILABLE,
    )
    with pytest.raises(AIModelNotFoundError) as exc_info:
        provider.generate_recommendations([dep], model="non-existent-model:99b")
    assert "not found" in str(exc_info.value)


def test_custom_exception_hierarchy():
    """Verify inheritance structure of AI provider exceptions."""
    assert issubclass(AIProviderUnavailableError, AIProviderError)
    assert issubclass(AIModelNotFoundError, AIProviderError)
    assert issubclass(AIInferenceTimeoutError, AIProviderError)
    assert issubclass(AIResponseParseError, AIProviderError)
