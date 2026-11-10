"""Unit tests for OllamaProvider with mocked OllamaClient."""
from unittest.mock import MagicMock
import pytest

from src.models.manifest import AnalyzedDependency, DependencyStatus, Ecosystem
from src.models.recommendation import (
    RecommendationCategory,
    RecommendationPriority,
    ValidationStatus,
)
from src.services.ai.base import (
    AIInferenceTimeoutError,
    AIModelNotFoundError,
    AIProviderUnavailableError,
    AIResponseParseError,
)
from src.services.ai.ollama_provider import OllamaProvider
from src.services.ollama_client import (
    ChatResponse,
    ChatMessage,
    OllamaConnectionError,
    OllamaHealthStatus,
    OllamaModelInfo,
    OllamaModelNotFoundError,
    OllamaParseError,
    OllamaTimeoutError,
)


@pytest.fixture
def mock_ollama_client():
    client = MagicMock()
    client.base_url = "http://127.0.0.1:11434"
    return client


def test_ollama_provider_check_availability_healthy(mock_ollama_client):
    mock_ollama_client.check_health.return_value = OllamaHealthStatus(
        is_available=True,
        base_url="http://127.0.0.1:11434",
        version="0.5.1",
    )
    provider = OllamaProvider(client=mock_ollama_client)
    is_avail, msg = provider.check_availability()
    assert is_avail is True
    assert "0.5.1" in msg


def test_ollama_provider_check_availability_offline(mock_ollama_client):
    mock_ollama_client.check_health.return_value = OllamaHealthStatus(
        is_available=False,
        base_url="http://127.0.0.1:11434",
        error_message="Connection refused",
    )
    provider = OllamaProvider(client=mock_ollama_client)
    is_avail, msg = provider.check_availability()
    assert is_avail is False
    assert "Connection refused" in msg


def test_ollama_provider_list_models_dynamic(mock_ollama_client):
    mock_ollama_client.list_models.return_value = [
        OllamaModelInfo(
            name="llama3.2:latest",
            model="llama3.2:latest",
            size_bytes=2000000000,
            modified_at="2026-11-01T12:00:00Z",
            digest="sha256:123456",
            family="llama",
            parameter_size="3B",
        ),
        OllamaModelInfo(
            name="codellama:7b",
            model="codellama:7b",
            size_bytes=4000000000,
            modified_at="2026-11-02T12:00:00Z",
            digest="sha256:654321",
            family="llama",
            parameter_size="7B",
        ),
    ]
    provider = OllamaProvider(client=mock_ollama_client)
    models = provider.list_models()
    assert len(models) == 2
    assert models[0].name == "llama3.2:latest"
    assert models[0].parameter_size == "3B"
    assert models[1].name == "codellama:7b"


def test_ollama_provider_list_models_when_offline(mock_ollama_client):
    mock_ollama_client.list_models.side_effect = OllamaConnectionError("Offline")
    provider = OllamaProvider(client=mock_ollama_client)
    models = provider.list_models()
    assert models == []


def test_ollama_provider_generate_recommendations_success(mock_ollama_client):
    mock_ollama_client.check_health.return_value = OllamaHealthStatus(
        is_available=True,
        base_url="http://127.0.0.1:11434",
        version="0.5.1",
    )
    mock_ollama_client.list_models.return_value = [
        OllamaModelInfo(
            name="llama3.2:latest",
            model="llama3.2:latest",
            size_bytes=2000000000,
            modified_at="2026-11-01T12:00:00Z",
            digest="abc",
        )
    ]
    mock_ollama_client.chat_json.return_value = (
        {
            "recommendations": [
                {
                    "package_name": "requests",
                    "candidate_version": "2.32.3",
                    "category": "RECOMMENDED",
                    "priority": "LOW",
                    "explanation": "Security patch with bug fixes.",
                    "breaking_changes_summary": None,
                }
            ]
        },
        ChatResponse(
            model="llama3.2:latest",
            message=ChatMessage(role="assistant", content="{}"),
            done=True,
        ),
    )

    provider = OllamaProvider(client=mock_ollama_client)
    deps = [
        AnalyzedDependency(
            name="requests",
            original_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.25.0",
            latest_version="2.32.3",
            status=DependencyStatus.UPGRADE_AVAILABLE,
            available_versions=["2.25.0", "2.31.0", "2.32.3"],
        )
    ]

    recs = provider.generate_recommendations(deps)
    assert len(recs) == 1
    assert recs[0].package_name == "requests"
    assert recs[0].candidate_version == "2.32.3"
    assert recs[0].category == RecommendationCategory.RECOMMENDED
    assert recs[0].priority == RecommendationPriority.LOW
    assert recs[0].is_ai_generated is True
    assert recs[0].model_used == "llama3.2:latest"


def test_ollama_provider_passthrough_up_to_date_and_preserved(mock_ollama_client):
    mock_ollama_client.check_health.return_value = OllamaHealthStatus(
        is_available=True,
        base_url="http://127.0.0.1:11434",
    )
    mock_ollama_client.list_models.return_value = [
        OllamaModelInfo(name="llama3.2:latest", model="llama3.2:latest", size_bytes=10, modified_at="", digest="")
    ]

    provider = OllamaProvider(client=mock_ollama_client)
    deps = [
        AnalyzedDependency(
            name="flask",
            original_name="flask",
            ecosystem=Ecosystem.PYTHON,
            current_version="3.0.0",
            latest_version="3.0.0",
            status=DependencyStatus.UP_TO_DATE,
        ),
        AnalyzedDependency(
            name="custom-pkg",
            original_name="custom-pkg",
            ecosystem=Ecosystem.PYTHON,
            status=DependencyStatus.PRESERVED,
            status_reason="Git URL dependency",
        ),
    ]

    recs = provider.generate_recommendations(deps)
    # Chat should not even be called when all packages are up to date or preserved
    mock_ollama_client.chat_json.assert_not_called()
    assert len(recs) == 2
    assert recs[0].category == RecommendationCategory.NO_UPGRADE_NEEDED
    assert recs[0].validation_status == ValidationStatus.SKIPPED
    assert recs[1].category == RecommendationCategory.PRESERVED_UNSUPPORTED
    assert recs[1].validation_status == ValidationStatus.SKIPPED


def test_ollama_provider_requested_model_not_found(mock_ollama_client):
    mock_ollama_client.check_health.return_value = OllamaHealthStatus(is_available=True, base_url="http://127.0.0.1:11434")
    mock_ollama_client.list_models.return_value = [
        OllamaModelInfo(name="llama3.2:latest", model="llama3.2:latest", size_bytes=10, modified_at="", digest="")
    ]
    provider = OllamaProvider(client=mock_ollama_client)
    deps = [
        AnalyzedDependency(
            name="requests",
            original_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.25.0",
            status=DependencyStatus.UPGRADE_AVAILABLE,
        )
    ]
    with pytest.raises(AIModelNotFoundError):
        provider.generate_recommendations(deps, model="nonexistent:latest")


def test_ollama_provider_offline_raises_unavailable(mock_ollama_client):
    mock_ollama_client.check_health.return_value = OllamaHealthStatus(is_available=False, base_url="http://127.0.0.1:11434")
    provider = OllamaProvider(client=mock_ollama_client)
    deps = [
        AnalyzedDependency(
            name="requests",
            original_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.25.0",
            status=DependencyStatus.UPGRADE_AVAILABLE,
        )
    ]
    with pytest.raises(AIProviderUnavailableError):
        provider.generate_recommendations(deps)


def test_ollama_provider_timeout_maps_to_ai_timeout(mock_ollama_client):
    mock_ollama_client.check_health.return_value = OllamaHealthStatus(is_available=True, base_url="http://127.0.0.1:11434")
    mock_ollama_client.list_models.return_value = [
        OllamaModelInfo(name="llama3.2:latest", model="llama3.2:latest", size_bytes=10, modified_at="", digest="")
    ]
    mock_ollama_client.chat_json.side_effect = OllamaTimeoutError("Timed out")
    provider = OllamaProvider(client=mock_ollama_client)
    deps = [
        AnalyzedDependency(
            name="requests",
            original_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.25.0",
            status=DependencyStatus.UPGRADE_AVAILABLE,
        )
    ]
    with pytest.raises(AIInferenceTimeoutError):
        provider.generate_recommendations(deps)


def test_ollama_provider_parse_error_maps_to_ai_parse_error(mock_ollama_client):
    mock_ollama_client.check_health.return_value = OllamaHealthStatus(is_available=True, base_url="http://127.0.0.1:11434")
    mock_ollama_client.list_models.return_value = [
        OllamaModelInfo(name="llama3.2:latest", model="llama3.2:latest", size_bytes=10, modified_at="", digest="")
    ]
    mock_ollama_client.chat_json.side_effect = OllamaParseError("Malformed output")
    provider = OllamaProvider(client=mock_ollama_client)
    deps = [
        AnalyzedDependency(
            name="requests",
            original_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.25.0",
            status=DependencyStatus.UPGRADE_AVAILABLE,
        )
    ]
    with pytest.raises(AIResponseParseError):
        provider.generate_recommendations(deps)


def test_ollama_provider_untrusted_data_handling(mock_ollama_client):
    """Verify provider rejects unknown packages, deduplicates, and bounds strings."""
    mock_ollama_client.check_health.return_value = OllamaHealthStatus(is_available=True, base_url="http://127.0.0.1:11434")
    mock_ollama_client.list_models.return_value = [
        OllamaModelInfo(name="llama3.2:latest", model="llama3.2:latest", size_bytes=10, modified_at="", digest="")
    ]
    # AI returns:
    # 1. requests (valid)
    # 2. malicious-pkg (unknown, should be skipped)
    # 3. requests (duplicate, should be ignored)
    # 4. urllib3 (valid, with oversized string fields and invalid enums)
    mock_ollama_client.chat_json.return_value = (
        {
            "recommendations": [
                {
                    "package_name": "requests",
                    "candidate_version": "2.32.3",
                    "category": "RECOMMENDED",
                    "priority": "LOW",
                    "explanation": "Valid update",
                },
                {
                    "package_name": "malicious-pkg",
                    "candidate_version": "9.9.9",
                    "category": "RECOMMENDED",
                    "priority": "HIGH",
                    "explanation": "Injected unknown package",
                },
                {
                    "package_name": "requests",
                    "candidate_version": "2.31.0",
                    "category": "CAUTION",
                    "priority": "HIGH",
                    "explanation": "Duplicate recommendation",
                },
                {
                    "package_name": "urllib3",
                    "candidate_version": "2.2.3",
                    "category": "TOTALLY_INVALID_CATEGORY",
                    "priority": "TOTALLY_INVALID_PRIORITY",
                    "explanation": "X" * 1000,  # Oversized explanation
                    "breaking_changes_summary": "Y" * 800,  # Oversized summary
                },
            ]
        },
        MagicMock(),
    )

    provider = OllamaProvider(client=mock_ollama_client)
    deps = [
        AnalyzedDependency(
            name="requests",
            original_name="requests",
            ecosystem=Ecosystem.PYTHON,
            current_version="2.25.0",
            latest_version="2.32.3",
            status=DependencyStatus.UPGRADE_AVAILABLE,
        ),
        AnalyzedDependency(
            name="urllib3",
            original_name="urllib3",
            ecosystem=Ecosystem.PYTHON,
            current_version="1.26.5",
            latest_version="2.2.3",
            status=DependencyStatus.UPGRADE_AVAILABLE,
        ),
    ]

    recs = provider.generate_recommendations(deps)
    assert len(recs) == 2
    # First is requests
    assert recs[0].package_name == "requests"
    assert recs[0].candidate_version == "2.32.3"

    # Second is urllib3 (malicious-pkg and duplicate requests were dropped)
    assert recs[1].package_name == "urllib3"
    assert recs[1].candidate_version == "2.2.3"
    # Safe enum fallbacks applied
    assert recs[1].category == RecommendationCategory.RECOMMENDED
    assert recs[1].priority == RecommendationPriority.MEDIUM
    # Text bounds enforced (<= 500 characters)
    assert len(recs[1].explanation) <= 500
    assert len(recs[1].breaking_changes_summary or "") <= 500

