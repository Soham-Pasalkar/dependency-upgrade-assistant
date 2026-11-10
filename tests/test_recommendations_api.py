"""FastAPI endpoint tests for AI status and recommendation generation."""
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes.recommendations import get_recommendation_service
from src.models.manifest import (
    AnalyzedDependency,
    DependencyStatus,
    Ecosystem,
    ManifestAnalysisResponse,
)
from src.models.recommendation import (
    ModelMetadata,
    PackageUpgradeRecommendation,
    RecommendationCategory,
    RecommendationPriority,
    ValidationStatus,
)
from src.services.ai.deterministic_engine import DeterministicRecommendationEngine
from src.services.ai.validator import RegistryGroundTruthValidator
from src.services.recommendation_service import RecommendationService


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def sample_manifest_analysis():
    return ManifestAnalysisResponse(
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        total_dependencies=2,
        up_to_date_count=1,
        upgrade_available_count=1,
        dependencies=[
            AnalyzedDependency(
                name="requests",
                original_name="requests",
                ecosystem=Ecosystem.PYTHON,
                current_version="2.25.0",
                declared_specifier="==2.25.0",
                latest_version="2.32.3",
                latest_compatible_version="2.32.3",
                status=DependencyStatus.UPGRADE_AVAILABLE,
                available_versions=["2.25.0", "2.31.0", "2.32.3"],
            ),
            AnalyzedDependency(
                name="flask",
                original_name="flask",
                ecosystem=Ecosystem.PYTHON,
                current_version="3.0.0",
                declared_specifier="==3.0.0",
                latest_version="3.0.0",
                status=DependencyStatus.UP_TO_DATE,
            ),
        ],
    )


def test_ai_status_endpoint_offline(client):
    """Verify /api/ai/status reports truthful offline status when daemon is inactive."""
    mock_ai = MagicMock()
    mock_ai.provider_id = "ollama"
    mock_ai.check_availability.return_value = (False, "Ollama daemon offline")
    mock_ai.list_models.return_value = []

    mock_service = RecommendationService(
        ai_provider=mock_ai,
        deterministic_engine=DeterministicRecommendationEngine(),
        validator=RegistryGroundTruthValidator(),
    )
    app.dependency_overrides[get_recommendation_service] = lambda: mock_service

    try:
        response = client.get("/api/ai/status")
        assert response.status_code == 200
        data = response.json()
        assert data["is_available"] is False
        assert data["provider_name"] == "ollama"
        assert data["discovered_models"] == []
        assert "offline" in data["message"].lower()

        # Check v1 route parity
        resp_v1 = client.get("/api/v1/ai/status")
        assert resp_v1.status_code == 200
        assert resp_v1.json() == data
    finally:
        app.dependency_overrides.clear()


def test_ai_status_endpoint_online_with_models(client):
    """Verify /api/ai/status returns discovered models when online."""
    mock_ai = MagicMock()
    mock_ai.provider_id = "ollama"
    mock_ai.check_availability.return_value = (True, "Ollama 0.5.1 active")
    mock_ai.list_models.return_value = [
        ModelMetadata(name="llama3.2:latest", parameter_size="3B", family="llama")
    ]

    mock_service = RecommendationService(
        ai_provider=mock_ai,
        deterministic_engine=DeterministicRecommendationEngine(),
        validator=RegistryGroundTruthValidator(),
    )
    app.dependency_overrides[get_recommendation_service] = lambda: mock_service

    try:
        response = client.get("/api/ai/status")
        assert response.status_code == 200
        data = response.json()
        assert data["is_available"] is True
        assert len(data["discovered_models"]) == 1
        assert data["discovered_models"][0]["name"] == "llama3.2:latest"
        assert data["default_model"] == "llama3.2:latest"

        # Check alias /api/recommendations/status
        resp_alias = client.get("/api/recommendations/status")
        assert resp_alias.status_code == 200
        assert resp_alias.json()["is_available"] is True
    finally:
        app.dependency_overrides.clear()


def test_generate_recommendations_offline_fallback(client, sample_manifest_analysis):
    """Verify automatic deterministic fallback when AI provider is offline."""
    mock_ai = MagicMock()
    mock_ai.provider_id = "ollama"
    mock_ai.check_availability.return_value = (False, "Ollama daemon offline")
    mock_ai.list_models.return_value = []

    mock_service = RecommendationService(
        ai_provider=mock_ai,
        deterministic_engine=DeterministicRecommendationEngine(),
        validator=RegistryGroundTruthValidator(),
    )
    app.dependency_overrides[get_recommendation_service] = lambda: mock_service

    try:
        payload = {
            "manifest_analysis": sample_manifest_analysis.model_dump(),
            "force_deterministic": False,
        }
        response = client.post("/api/recommendations/generate", json=payload)
        assert response.status_code == 200
        data = response.json()

        assert data["provider_used"] == "deterministic"
        assert data["is_offline_fallback"] is True
        assert data["total_upgrades_recommended"] == 1
        assert len(data["recommendations"]) == 2

        # requests recommendation
        req_rec = next(r for r in data["recommendations"] if r["package_name"] == "requests")
        assert req_rec["candidate_version"] == "2.32.3"
        assert req_rec["is_ai_generated"] is False
        assert req_rec["validation_status"] == "FALLBACK_DETERMINISTIC"

        # flask recommendation
        flask_rec = next(r for r in data["recommendations"] if r["package_name"] == "flask")
        assert flask_rec["category"] == "NO_UPGRADE_NEEDED"
        assert flask_rec["candidate_version"] == "3.0.0"

        # Check v1 route parity
        resp_v1 = client.post("/api/v1/recommendations/generate", json=payload)
        assert resp_v1.status_code == 200
        assert resp_v1.json()["total_upgrades_recommended"] == 1
    finally:
        app.dependency_overrides.clear()


def test_generate_recommendations_with_ai_provider(client, sample_manifest_analysis):
    """Verify AI provider execution and ground truth validation."""
    mock_ai = MagicMock()
    mock_ai.provider_id = "ollama"
    mock_ai.check_availability.return_value = (True, "Ollama 0.5.1 active")
    mock_ai.list_models.return_value = [
        ModelMetadata(name="llama3.2:latest", family="llama")
    ]
    mock_ai.generate_recommendations.return_value = [
        PackageUpgradeRecommendation(
            package_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.25.0",
            declared_specifier="==2.25.0",
            latest_registry_version="2.32.3",
            candidate_version="2.32.3",
            category=RecommendationCategory.RECOMMENDED,
            priority=RecommendationPriority.LOW,
            explanation="AI advises patch upgrade for security fixes.",
            is_ai_generated=True,
            provider_used="ollama",
            model_used="llama3.2:latest",
        ),
        PackageUpgradeRecommendation(
            package_name="flask",
            ecosystem=Ecosystem.PYTHON,
            current_version="3.0.0",
            candidate_version="3.0.0",
            category=RecommendationCategory.NO_UPGRADE_NEEDED,
            priority=RecommendationPriority.LOW,
            explanation="Already up to date.",
            is_ai_generated=False,
            provider_used="ollama",
        ),
    ]

    mock_service = RecommendationService(
        ai_provider=mock_ai,
        deterministic_engine=DeterministicRecommendationEngine(),
        validator=RegistryGroundTruthValidator(),
    )
    app.dependency_overrides[get_recommendation_service] = lambda: mock_service

    try:
        payload = {
            "manifest_analysis": sample_manifest_analysis.model_dump(),
            "preferred_model": "llama3.2:latest",
            "force_deterministic": False,
        }
        response = client.post("/api/recommendations/generate", json=payload)
        assert response.status_code == 200
        data = response.json()

        assert data["provider_used"] == "ollama"
        assert data["model_used"] == "llama3.2:latest"
        assert data["is_offline_fallback"] is False

        req_rec = next(r for r in data["recommendations"] if r["package_name"] == "requests")
        assert req_rec["candidate_version"] == "2.32.3"
        assert req_rec["is_ai_generated"] is True
        assert req_rec["validation_status"] == "VALIDATED"
    finally:
        app.dependency_overrides.clear()


def test_generate_recommendations_force_deterministic(client, sample_manifest_analysis):
    """Verify force_deterministic bypasses AI provider even if available."""
    mock_ai = MagicMock()
    mock_ai.provider_id = "ollama"
    mock_ai.check_availability.return_value = (True, "Ollama online")
    mock_ai.list_models.return_value = [ModelMetadata(name="llama3.2:latest")]

    mock_service = RecommendationService(
        ai_provider=mock_ai,
        deterministic_engine=DeterministicRecommendationEngine(),
        validator=RegistryGroundTruthValidator(),
    )
    app.dependency_overrides[get_recommendation_service] = lambda: mock_service

    try:
        payload = {
            "manifest_analysis": sample_manifest_analysis.model_dump(),
            "force_deterministic": True,
        }
        response = client.post("/api/recommendations/generate", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["provider_used"] == "deterministic"
        mock_ai.generate_recommendations.assert_not_called()
    finally:
        app.dependency_overrides.clear()
