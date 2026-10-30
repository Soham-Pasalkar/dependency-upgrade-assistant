"""Unit tests for Ollama REST client, dynamic model discovery, and JSON parsing."""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.services.ollama_client import (
    ChatMessage,
    ChatResponse,
    OllamaClient,
    OllamaConnectionError,
    OllamaHealthStatus,
    OllamaModelInfo,
    OllamaModelNotFoundError,
    OllamaParseError,
    OllamaResponseError,
    OllamaTimeoutError,
)


# ---------------------------------------------------------------------------
# Initialization & Health Check Tests
# ---------------------------------------------------------------------------

def test_client_initialization_defaults_and_custom_url():
    """Verify endpoint resolution and trailing slash normalization."""
    default_client = OllamaClient()
    assert default_client.base_url == "http://127.0.0.1:11434"

    custom_client = OllamaClient(base_url="http://custom-host:11434/")
    assert custom_client.base_url == "http://custom-host:11434"

    no_scheme_client = OllamaClient(base_url="localhost:11434")
    assert no_scheme_client.base_url == "http://localhost:11434"


@patch.object(requests.Session, "get")
def test_check_health_service_available(mock_get: MagicMock):
    """Verify health check returns available when /api/version responds with 200."""
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: {"version": "0.35.0"},
    )
    client = OllamaClient()
    health = client.check_health()

    assert health.is_available is True
    assert health.version == "0.35.0"
    assert health.error_message is None


@patch.object(requests.Session, "get")
def test_check_health_fallback_to_tags(mock_get: MagicMock):
    """Verify health check falls back to /api/tags if /api/version returns 404."""
    mock_get.side_effect = [
        MagicMock(status_code=404),  # /api/version fails
        MagicMock(status_code=200, json=lambda: {"models": []}),  # /api/tags succeeds
    ]
    client = OllamaClient()
    health = client.check_health()

    assert health.is_available is True
    assert health.version is None


@patch.object(requests.Session, "get")
def test_check_health_service_unavailable_connection_refused(mock_get: MagicMock):
    """Verify health check detects connection refused without raising unhandled exceptions."""
    mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")
    client = OllamaClient()
    health = client.check_health()

    assert health.is_available is False
    assert "Could not connect to Ollama" in str(health.error_message)


@patch.object(requests.Session, "get")
def test_check_health_timeout(mock_get: MagicMock):
    """Verify health check handles connection timeouts gracefully."""
    mock_get.side_effect = requests.exceptions.Timeout("Connection timed out")
    client = OllamaClient()
    health = client.check_health(timeout=2.0)

    assert health.is_available is False
    assert "timed out after 2.0s" in str(health.error_message)


# ---------------------------------------------------------------------------
# Model Discovery Tests
# ---------------------------------------------------------------------------

@patch.object(requests.Session, "get")
def test_list_models_successful_discovery(mock_get: MagicMock):
    """Verify discovery of installed models and metadata parsing from /api/tags."""
    sample_tags_response = {
        "models": [
            {
                "name": "llama3.1:8b",
                "model": "llama3.1:8b",
                "size": 4920755200,
                "digest": "46e0c10c039e",
                "modified_at": "2026-02-15T12:00:00Z",
                "details": {
                    "family": "llama",
                    "parameter_size": "8.0B",
                    "quantization_level": "Q4_0",
                },
            },
            {
                "name": "gemma3:4b",
                "model": "gemma3:4b",
                "size": 3310000000,
                "digest": "a2af6cc3eb7f",
                "modified_at": "2026-02-14T10:00:00Z",
                "details": {
                    "family": "gemma",
                    "parameter_size": "4B",
                },
            },
        ]
    }
    mock_get.return_value = MagicMock(status_code=200, json=lambda: sample_tags_response)

    client = OllamaClient()
    models = client.list_models()

    assert len(models) == 2
    
    m1 = models[0]
    assert m1.name == "llama3.1:8b"
    assert m1.family == "llama"
    assert m1.parameter_size == "8.0B"
    assert m1.size_gb == 4.58
    assert "8.0B" in m1.display_label

    m2 = models[1]
    assert m2.name == "gemma3:4b"
    assert m2.family == "gemma"
    assert m2.parameter_size == "4B"

    # Verify convenience list_model_names
    names = client.list_model_names()
    assert names == ["gemma3:4b", "llama3.1:8b"]


@patch.object(requests.Session, "get")
def test_list_models_empty_list(mock_get: MagicMock):
    """Verify clean empty list returned when no models are installed."""
    mock_get.return_value = MagicMock(status_code=200, json=lambda: {"models": []})
    client = OllamaClient()
    models = client.list_models()

    assert models == []
    assert client.list_model_names() == []


@patch.object(requests.Session, "get")
def test_list_models_malformed_response_missing_models_key(mock_get: MagicMock):
    """Verify OllamaParseError raised when /api/tags response lacks 'models' key."""
    mock_get.return_value = MagicMock(status_code=200, json=lambda: {"unexpected_key": 123})
    client = OllamaClient()

    with pytest.raises(OllamaParseError, match="missing 'models' array"):
        client.list_models()


@patch.object(requests.Session, "get")
def test_list_models_malformed_json_syntax(mock_get: MagicMock):
    """Verify OllamaParseError raised when /api/tags returns invalid JSON."""
    mock_resp = MagicMock(status_code=200, text="Internal Server Error: Bad Gateway")
    mock_resp.json.side_effect = ValueError("Invalid JSON")
    mock_get.return_value = mock_resp

    client = OllamaClient()
    with pytest.raises(OllamaParseError, match="Malformed JSON returned"):
        client.list_models()


@patch.object(requests.Session, "get")
def test_list_models_connection_refused(mock_get: MagicMock):
    """Verify OllamaConnectionError raised when connecting to /api/tags fails."""
    mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")
    client = OllamaClient()

    with pytest.raises(OllamaConnectionError, match="Failed to connect to Ollama"):
        client.list_models()


@patch.object(requests.Session, "get")
def test_list_models_timeout(mock_get: MagicMock):
    """Verify OllamaTimeoutError raised when /api/tags times out."""
    mock_get.side_effect = requests.exceptions.Timeout("Request timed out")
    client = OllamaClient()

    with pytest.raises(OllamaTimeoutError, match="Timed out after 10.0s"):
        client.list_models(timeout=10.0)


@patch.object(requests.Session, "get")
def test_list_models_unexpected_http_status_502(mock_get: MagicMock):
    """Verify OllamaResponseError raised on unexpected HTTP status."""
    mock_get.return_value = MagicMock(
        status_code=502,
        text="Bad Gateway",
    )
    client = OllamaClient()

    with pytest.raises(OllamaResponseError) as exc_info:
        client.list_models()
    assert exc_info.value.status_code == 502
    assert "HTTP 502" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Chat Completion Tests
# ---------------------------------------------------------------------------

@patch.object(requests.Session, "post")
def test_chat_successful_completion(mock_post: MagicMock):
    """Verify successful chat completion with standard payload and ChatResponse parsing."""
    sample_chat_response = {
        "model": "llama3.1:8b",
        "created_at": "2026-10-30T08:00:00Z",
        "message": {
            "role": "assistant",
            "content": "Here is the proposed analysis.",
        },
        "done": True,
        "total_duration": 1500000000,
        "prompt_eval_count": 25,
        "eval_count": 42,
    }
    mock_post.return_value = MagicMock(status_code=200, json=lambda: sample_chat_response)

    client = OllamaClient()
    response = client.chat(
        model="llama3.1:8b",
        messages=[ChatMessage(role="user", content="Analyze this requirement")],
    )

    assert isinstance(response, ChatResponse)
    assert response.model == "llama3.1:8b"
    assert response.message.role == "assistant"
    assert response.content == "Here is the proposed analysis."
    assert response.done is True
    assert response.eval_count == 42

    # Check payload sent to Ollama
    call_args = mock_post.call_args
    sent_payload = call_args[1]["json"]
    assert sent_payload["model"] == "llama3.1:8b"
    assert sent_payload["stream"] is False
    assert sent_payload["messages"] == [{"role": "user", "content": "Analyze this requirement"}]
    assert sent_payload["options"]["temperature"] == 0.1


@patch.object(requests.Session, "post")
def test_chat_json_mode_and_structured_schema(mock_post: MagicMock):
    """Verify chat_json with schema enforces JSON format and deserializes JSON response."""
    expected_dict = {
        "package_name": "requests",
        "risk_level": "LOW",
        "breaking_changes": "None detected",
    }
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "model": "llama3.1:8b",
            "message": {
                "role": "assistant",
                "content": json.dumps(expected_dict),
            },
            "done": True,
        },
    )

    schema = {
        "type": "object",
        "properties": {
            "package_name": {"type": "string"},
            "risk_level": {"type": "string"},
            "breaking_changes": {"type": "string"},
        },
        "required": ["package_name", "risk_level"],
    }

    client = OllamaClient()
    parsed_json, chat_resp = client.chat_json(
        model="llama3.1:8b",
        messages=[{"role": "user", "content": "Assess risk"}],
        schema=schema,
    )

    assert parsed_json == expected_dict
    assert chat_resp.model == "llama3.1:8b"

    # Assert schema was passed in format payload
    call_payload = mock_post.call_args[1]["json"]
    assert call_payload["format"] == schema


@patch.object(requests.Session, "post")
def test_chat_json_markdown_code_fence_fallback(mock_post: MagicMock):
    """Verify chat_json extracts JSON when wrapped in markdown ```json ... ``` blocks."""
    inner_data = {"status": "ok", "recommendations": ["update urllib3"]}
    raw_markdown = f"Here is the result:\n```json\n{json.dumps(inner_data)}\n```\nHope this helps!"

    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "model": "gemma3:4b",
            "message": {"role": "assistant", "content": raw_markdown},
            "done": True,
        },
    )

    client = OllamaClient()
    parsed, _ = client.chat_json(
        model="gemma3:4b",
        messages=[{"role": "user", "content": "Provide suggestions"}],
    )

    assert parsed == inner_data


@patch.object(requests.Session, "post")
def test_chat_json_invalid_json_raises_parse_error(mock_post: MagicMock):
    """Verify OllamaParseError raised when model output cannot be parsed as JSON."""
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "model": "llama3.1:8b",
            "message": {"role": "assistant", "content": "This is completely non-JSON text with no braces."},
            "done": True,
        },
    )

    client = OllamaClient()
    with pytest.raises(OllamaParseError, match="Failed to parse JSON response"):
        client.chat_json(
            model="llama3.1:8b",
            messages=[{"role": "user", "content": "Give me JSON"}],
        )


@patch.object(requests.Session, "post")
def test_chat_model_not_found_404(mock_post: MagicMock):
    """Verify OllamaModelNotFoundError raised when requested model is not installed."""
    mock_post.return_value = MagicMock(
        status_code=404,
        text="model 'non-existent-model' not found, try pulling it first",
    )

    client = OllamaClient()
    with pytest.raises(OllamaModelNotFoundError) as exc_info:
        client.chat(model="non-existent-model", messages=[{"role": "user", "content": "hi"}])
    
    assert exc_info.value.status_code == 404
    assert "was not found in the local Ollama runtime" in str(exc_info.value)


@patch.object(requests.Session, "post")
def test_chat_unexpected_http_status_500(mock_post: MagicMock):
    """Verify OllamaResponseError raised on internal server errors from Ollama."""
    mock_post.return_value = MagicMock(
        status_code=500,
        text="Out of memory in VRAM",
    )

    client = OllamaClient()
    with pytest.raises(OllamaResponseError) as exc_info:
        client.chat(model="llama3.1:8b", messages=[{"role": "user", "content": "hi"}])

    assert exc_info.value.status_code == 500
    assert "HTTP 500" in str(exc_info.value)


@patch.object(requests.Session, "post")
def test_chat_connection_refused(mock_post: MagicMock):
    """Verify OllamaConnectionError raised when POST /api/chat connection fails."""
    mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")
    client = OllamaClient()

    with pytest.raises(OllamaConnectionError, match="Failed to connect to Ollama"):
        client.chat(model="llama3.1:8b", messages=[{"role": "user", "content": "hi"}])


@patch.object(requests.Session, "post")
def test_chat_timeout(mock_post: MagicMock):
    """Verify OllamaTimeoutError raised when POST /api/chat exceeds timeout."""
    mock_post.side_effect = requests.exceptions.Timeout("Read timeout")
    client = OllamaClient()

    with pytest.raises(OllamaTimeoutError, match="timed out after 30.0s"):
        client.chat(model="llama3.1:8b", messages=[{"role": "user", "content": "hi"}], timeout=30.0)


@patch.object(requests.Session, "post")
def test_chat_malformed_api_response_missing_content(mock_post: MagicMock):
    """Verify OllamaParseError raised when /api/chat responds with missing content."""
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"model": "llama3.1:8b", "done": True},  # Missing "message"
    )

    client = OllamaClient()
    with pytest.raises(OllamaParseError, match="Missing 'message' or 'content'"):
        client.chat(model="llama3.1:8b", messages=[{"role": "user", "content": "hi"}])


def test_chat_input_validation():
    """Verify client rejects empty model name or empty messages list."""
    client = OllamaClient()

    with pytest.raises(ValueError, match="Model name must not be empty"):
        client.chat(model="", messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(ValueError, match="Chat messages list must not be empty"):
        client.chat(model="llama3.1:8b", messages=[])

    with pytest.raises(ValueError, match="Invalid message format"):
        client.chat(model="llama3.1:8b", messages=["not a dict or ChatMessage"])  # type: ignore

