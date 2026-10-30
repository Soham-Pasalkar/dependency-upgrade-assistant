"""Ollama REST API client for local LLM inference and dynamic model discovery.

Provides structured, safe communication with the local Ollama daemon:
- Health checking and daemon reachability detection.
- Dynamic model discovery via /api/tags without assuming model names.
- Chat completions via /api/chat with non-streaming execution.
- Structured JSON-mode response handling and schema enforcement.
- Robust error handling (connection refused, timeouts, 404 model not found, malformed JSON).
- Strict credential isolation (never logs or accepts repository secrets).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_REQUEST_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OllamaError(Exception):
    """Base exception for all Ollama client errors."""


class OllamaConnectionError(OllamaError):
    """Raised when unable to connect to the local Ollama daemon."""


class OllamaTimeoutError(OllamaError):
    """Raised when an Ollama API request times out."""


class OllamaResponseError(OllamaError):
    """Raised when the Ollama API returns an HTTP error status."""

    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class OllamaModelNotFoundError(OllamaResponseError):
    """Raised when the requested model is not installed locally in Ollama."""


class OllamaParseError(OllamaError):
    """Raised when the Ollama API response is malformed or invalid JSON."""


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OllamaModelInfo:
    """Metadata describing a locally installed Ollama model."""
    name: str
    model: str
    size_bytes: int
    modified_at: str
    digest: str
    family: Optional[str] = None
    parameter_size: Optional[str] = None
    quantization_level: Optional[str] = None

    @property
    def size_mb(self) -> float:
        """Return model size in megabytes."""
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def size_gb(self) -> float:
        """Return model size in gigabytes."""
        return round(self.size_bytes / (1024 * 1024 * 1024), 2)

    @property
    def display_label(self) -> str:
        """Human-readable label for UI selection."""
        details = []
        if self.parameter_size:
            details.append(self.parameter_size)
        if self.size_bytes > 0:
            details.append(f"{self.size_gb} GB")
        suffix = f" ({', '.join(details)})" if details else ""
        return f"{self.name}{suffix}"


@dataclass(frozen=True)
class OllamaHealthStatus:
    """Health and connectivity status of the Ollama service."""
    is_available: bool
    base_url: str
    version: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class ChatMessage:
    """A single chat message exchanged with Ollama."""
    role: str
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class ChatResponse:
    """Response returned from an Ollama chat request."""
    model: str
    message: ChatMessage
    done: bool
    total_duration_ns: Optional[int] = None
    prompt_eval_count: Optional[int] = None
    eval_count: Optional[int] = None
    raw_response: Optional[Dict[str, Any]] = None

    @property
    def content(self) -> str:
        """Convenience property for accessing the assistant message content."""
        return self.message.content


# ---------------------------------------------------------------------------
# Ollama Client
# ---------------------------------------------------------------------------

class OllamaClient:
    """Client for interacting with local Ollama runtime over HTTP REST API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        session: Optional[requests.Session] = None,
    ) -> None:
        """Initialize the Ollama client.
        
        Args:
            base_url: Base HTTP endpoint for Ollama (e.g., http://127.0.0.1:11434).
                      Defaults to OLLAMA_HOST env var or http://127.0.0.1:11434.
            timeout: Default request timeout in seconds.
            session: Optional custom requests.Session (for connection pooling or testing).
        """
        raw_url = base_url or os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_ENDPOINT
        # Ensure scheme is present
        if not raw_url.startswith(("http://", "https://")):
            raw_url = f"http://{raw_url}"
        self.base_url = raw_url.rstrip("/")
        self.default_timeout = max(1.0, float(timeout))
        self._session = session or requests.Session()

    def _url(self, path: str) -> str:
        """Construct full URL for a given API path."""
        clean_path = path.lstrip("/")
        return f"{self.base_url}/{clean_path}"

    def check_health(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> OllamaHealthStatus:
        """Check whether the local Ollama service is reachable and responsive.
        
        Queries /api/version (or /api/tags as fallback).
        """
        version: Optional[str] = None
        try:
            resp = self._session.get(self._url("/api/version"), timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    version = data.get("version")
                return OllamaHealthStatus(is_available=True, base_url=self.base_url, version=version)
        except Exception:
            pass

        # Fallback to /api/tags if /api/version is not supported by older Ollama versions
        try:
            resp = self._session.get(self._url("/api/tags"), timeout=timeout)
            if resp.status_code == 200:
                return OllamaHealthStatus(is_available=True, base_url=self.base_url, version=version)
            return OllamaHealthStatus(
                is_available=False,
                base_url=self.base_url,
                error_message=f"Ollama returned HTTP status {resp.status_code}",
            )
        except requests.exceptions.ConnectionError:
            return OllamaHealthStatus(
                is_available=False,
                base_url=self.base_url,
                error_message=(
                    f"Could not connect to Ollama at {self.base_url}. "
                    "Please ensure the Ollama daemon is running ('ollama serve')."
                ),
            )
        except requests.exceptions.Timeout:
            return OllamaHealthStatus(
                is_available=False,
                base_url=self.base_url,
                error_message=f"Connection to Ollama at {self.base_url} timed out after {timeout}s.",
            )
        except Exception as exc:
            return OllamaHealthStatus(
                is_available=False,
                base_url=self.base_url,
                error_message=f"Ollama health check error: {exc}",
            )

    def list_models(self, timeout: float = 10.0) -> List[OllamaModelInfo]:
        """Discover locally installed models via GET /api/tags.
        
        Returns:
            List of OllamaModelInfo objects describing available models.
            
        Raises:
            OllamaConnectionError: If daemon is unreachable.
            OllamaTimeoutError: If request times out.
            OllamaResponseError: If HTTP status is not 200.
            OllamaParseError: If response body is malformed.
        """
        try:
            resp = self._session.get(self._url("/api/tags"), timeout=timeout)
        except requests.exceptions.ConnectionError as err:
            raise OllamaConnectionError(
                f"Failed to connect to Ollama at {self.base_url} to discover models. "
                "Ensure Ollama is running ('ollama serve')."
            ) from err
        except requests.exceptions.Timeout as err:
            raise OllamaTimeoutError(
                f"Timed out after {timeout}s while discovering Ollama models from {self.base_url}."
            ) from err
        except Exception as err:
            raise OllamaError(f"Unexpected network error querying Ollama: {err}") from err

        if resp.status_code != 200:
            raise OllamaResponseError(
                f"Failed to list models from Ollama (HTTP {resp.status_code}): {resp.text}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        try:
            data = resp.json()
        except ValueError as err:
            raise OllamaParseError(f"Malformed JSON returned from /api/tags: {resp.text}") from err

        if not isinstance(data, dict) or "models" not in data:
            raise OllamaParseError(f"Unexpected JSON schema from /api/tags (missing 'models' array): {data}")

        models_list = data.get("models")
        if not isinstance(models_list, list):
            raise OllamaParseError(f"'models' field in /api/tags must be a list, got: {type(models_list)}")

        results: List[OllamaModelInfo] = []
        for item in models_list:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("model") or "unknown"
            details = item.get("details") or {}
            results.append(
                OllamaModelInfo(
                    name=name,
                    model=item.get("model", name),
                    size_bytes=int(item.get("size", 0)),
                    modified_at=str(item.get("modified_at", "")),
                    digest=str(item.get("digest", "")),
                    family=details.get("family"),
                    parameter_size=details.get("parameter_size"),
                    quantization_level=details.get("quantization_level"),
                )
            )

        return results

    def list_model_names(self, timeout: float = 10.0) -> List[str]:
        """Convenience helper returning sorted names of locally installed models."""
        models = self.list_models(timeout=timeout)
        return sorted([m.name for m in models])

    def chat(
        self,
        model: str,
        messages: List[Union[ChatMessage, Dict[str, str]]],
        format: Optional[Union[str, Dict[str, Any]]] = None,
        options: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> ChatResponse:
        """Send a chat completion request to /api/chat.
        
        Args:
            model: Installed model identifier (e.g., 'llama3.1:8b', 'gemma3:4b').
            messages: List of ChatMessage objects or dicts ({"role": "user", "content": "..."}).
            format: 'json' for JSON mode, or a dict schema for structured outputs.
            options: Model parameters, e.g. {"temperature": 0.1}.
            timeout: Request timeout in seconds (defaults to self.default_timeout).
            
        Returns:
            ChatResponse containing model output and token metrics.
            
        Raises:
            OllamaModelNotFoundError: If requested model is not installed.
            OllamaConnectionError: If connection cannot be established.
            OllamaTimeoutError: If request times out.
            OllamaResponseError: On non-200 HTTP statuses.
            OllamaParseError: If response is malformed.
        """
        if not model or not model.strip():
            raise ValueError("Model name must not be empty.")

        if not messages:
            raise ValueError("Chat messages list must not be empty.")

        # Normalize messages
        normalized_messages: List[Dict[str, str]] = []
        for msg in messages:
            if isinstance(msg, ChatMessage):
                normalized_messages.append(msg.to_dict())
            elif isinstance(msg, dict) and "role" in msg and "content" in msg:
                normalized_messages.append({"role": str(msg["role"]), "content": str(msg["content"])})
            else:
                raise ValueError(f"Invalid message format: {msg}. Expected ChatMessage or dict with 'role' and 'content'.")

        payload: Dict[str, Any] = {
            "model": model.strip(),
            "messages": normalized_messages,
            "stream": False,
        }

        if format is not None:
            payload["format"] = format

        default_options: Dict[str, Any] = {"temperature": 0.1}
        if options:
            default_options.update(options)
        payload["options"] = default_options

        req_timeout = timeout if timeout is not None else self.default_timeout

        try:
            resp = self._session.post(
                self._url("/api/chat"),
                json=payload,
                timeout=req_timeout,
            )
        except requests.exceptions.ConnectionError as err:
            raise OllamaConnectionError(
                f"Failed to connect to Ollama at {self.base_url}. Ensure the service is active."
            ) from err
        except requests.exceptions.Timeout as err:
            raise OllamaTimeoutError(
                f"Ollama chat request for model '{model}' timed out after {req_timeout}s."
            ) from err
        except Exception as err:
            raise OllamaError(f"Unexpected network error during chat request: {err}") from err

        if resp.status_code == 404:
            raise OllamaModelNotFoundError(
                f"Model '{model}' was not found in the local Ollama runtime (HTTP 404). "
                "Please verify the model is installed using list_models().",
                status_code=404,
                response_body=resp.text,
            )

        if resp.status_code != 200:
            raise OllamaResponseError(
                f"Ollama chat API returned error (HTTP {resp.status_code}): {resp.text}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        try:
            data = resp.json()
        except ValueError as err:
            raise OllamaParseError(f"Ollama returned invalid JSON response: {resp.text}") from err

        if not isinstance(data, dict):
            raise OllamaParseError(f"Expected dict response from /api/chat, got: {type(data)}")

        msg_dict = data.get("message")
        if not isinstance(msg_dict, dict) or "content" not in msg_dict:
            raise OllamaParseError(f"Missing 'message' or 'content' in Ollama chat response: {data}")

        return ChatResponse(
            model=data.get("model", model),
            message=ChatMessage(
                role=msg_dict.get("role", "assistant"),
                content=msg_dict.get("content", ""),
            ),
            done=bool(data.get("done", True)),
            total_duration_ns=data.get("total_duration"),
            prompt_eval_count=data.get("prompt_eval_count"),
            eval_count=data.get("eval_count"),
            raw_response=data,
        )

    def chat_json(
        self,
        model: str,
        messages: List[Union[ChatMessage, Dict[str, str]]],
        schema: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[Dict[str, Any], ChatResponse]:
        """Send a chat request enforcing JSON output and returning the parsed JSON object.
        
        Supports structured JSON schemas if provided, falling back to format="json".
        If response contains markdown code block wrappers (```json ... ```), extracts the JSON.
        
        Returns:
            Tuple of (parsed_json_dict, chat_response).
            
        Raises:
            OllamaParseError: If output cannot be parsed as valid JSON.
            OllamaModelNotFoundError: If model is not found.
            OllamaConnectionError: If connection fails.
            OllamaTimeoutError: If request times out.
        """
        # If schema is provided, pass schema directly to format; otherwise pass "json"
        format_param: Union[str, Dict[str, Any]] = schema if schema is not None else "json"

        response = self.chat(
            model=model,
            messages=messages,
            format=format_param,
            options=options,
            timeout=timeout,
        )

        content = response.content.strip()

        # 1. Direct JSON parse
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return parsed, response
        except json.JSONDecodeError:
            pass

        # 2. Extract from markdown code fences ```json ... ``` or ``` ... ```
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
        if fence_match:
            extracted_text = fence_match.group(1).strip()
            try:
                parsed = json.loads(extracted_text)
                if isinstance(parsed, dict):
                    return parsed, response
            except json.JSONDecodeError:
                pass

        # 3. Search for outermost JSON object { ... }
        brace_match = re.search(r"(\{[\s\S]*\})", content)
        if brace_match:
            try:
                parsed = json.loads(brace_match.group(1))
                if isinstance(parsed, dict):
                    return parsed, response
            except json.JSONDecodeError:
                pass

        raise OllamaParseError(
            f"Failed to parse JSON response from Ollama model '{model}'. "
            f"Raw content: {content[:300]}..."
        )

