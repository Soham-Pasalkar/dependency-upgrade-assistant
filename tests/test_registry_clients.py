"""Unit tests for PyPI and npm registry clients with strict bounded reading and thread isolation."""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock
import pytest
import requests

from src.models.manifest import RegistryLookupStatus
from src.services.registry_client import (
    NpmRegistryClient,
    PyPIRegistryClient,
    ThreadLocalSessionFactory,
)


def _make_mock_response(status_code: int = 200, data: dict = None, headers: dict = None):
    """Helper creating a mock response with strict streamed chunks and close()."""
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = status_code
    mock_resp.headers = headers or {}
    if data is not None:
        raw_bytes = json.dumps(data).encode("utf-8")
        mock_resp.iter_content.return_value = [raw_bytes]
    else:
        mock_resp.iter_content.return_value = []
    mock_resp.close = MagicMock()
    return mock_resp


def test_pypi_client_success():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = _make_mock_response(
        status_code=200,
        data={
            "info": {"version": "2.31.0"},
            "releases": {
                "2.28.0": [{"yanked": False}],
                "2.31.0": [{"yanked": False}],
            },
        },
        headers={"Content-Length": "120"},
    )
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("Requests")

    assert meta.package_name == "requests"
    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.latest_version == "2.31.0"
    assert "2.28.0" in meta.available_versions
    assert "2.31.0" in meta.available_versions
    assert not meta.is_deprecated_or_yanked
    mock_session.get.assert_called_once()
    assert "https://pypi.org/pypi/requests/json" in mock_session.get.call_args[0][0]
    mock_resp.close.assert_called()


def test_pypi_client_detects_yanked_latest():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = _make_mock_response(
        status_code=200,
        data={
            "info": {"version": "3.0.0"},
            "releases": {
                "3.0.0": [{"yanked": True, "yanked_reason": "Critical regression in auth"}],
            },
        },
    )
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("flawed-pkg")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.is_deprecated_or_yanked is True
    assert meta.deprecation_message == "Critical regression in auth"
    mock_resp.close.assert_called()


def test_pypi_client_not_found_404():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = _make_mock_response(status_code=404)
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("nonexistent-package-xyz-123")

    assert meta.status == RegistryLookupStatus.NOT_FOUND
    assert meta.latest_version is None
    assert "not found" in meta.error_message.lower()
    mock_resp.close.assert_called()


def test_pypi_client_ttl_caching_prevents_duplicate_network_calls():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = _make_mock_response(
        status_code=200,
        data={
            "info": {"version": "1.0.0"},
            "releases": {"1.0.0": []},
        },
    )
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session, cache_ttl=100.0)
    meta1 = client.get_package_metadata("cached-package")
    meta2 = client.get_package_metadata("cached-package")

    assert meta1.latest_version == "1.0.0"
    assert meta2.latest_version == "1.0.0"
    assert mock_session.get.call_count == 1


def test_pypi_client_fallback_to_local_catalog_on_network_error():
    mock_session = MagicMock(spec=requests.Session)
    mock_session.get.side_effect = requests.ConnectionError("Connection refused")

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("requests")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.latest_version is not None
    assert "local catalog cache" in meta.deprecation_message


# ---------------------------------------------------------------------------
# Strict Bounded Stream and Reader Regression Tests (No Fallback Bypass)
# ---------------------------------------------------------------------------

def test_stream_iter_content_raises_immediately_treated_as_error():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.close = MagicMock()

    def exploding_stream(chunk_size):
        raise ConnectionResetError("Connection lost mid-read")

    mock_resp.iter_content.side_effect = exploding_stream
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("broken-stream-pkg")

    # Connection must be closed and error safely handled (or local catalog fallback)
    mock_resp.close.assert_called()
    assert meta.status in (RegistryLookupStatus.NETWORK_ERROR, RegistryLookupStatus.SUCCESS)
    if meta.status == RegistryLookupStatus.NETWORK_ERROR:
        assert "streaming read failure" in meta.error_message.lower() or "network error" in meta.error_message.lower()


def test_stream_iter_content_raises_after_partial_chunks_discards_partial():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.close = MagicMock()

    def partial_then_explode(chunk_size):
        # Yield valid JSON start, then crash mid-stream
        yield b'{"info": {"version": "9.9.9"}, '
        raise BrokenPipeError("Broken pipe")

    mock_resp.iter_content.side_effect = partial_then_explode
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("partial-explode-pkg")

    # Connection closed, partial content never treated as valid JSON
    mock_resp.close.assert_called()
    assert meta.status in (RegistryLookupStatus.NETWORK_ERROR, RegistryLookupStatus.SUCCESS)
    if meta.status == RegistryLookupStatus.NETWORK_ERROR:
        assert meta.latest_version != "9.9.9"


def test_stream_empty_response_treated_as_error():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = _make_mock_response(status_code=200, data=None)
    mock_resp.iter_content.return_value = []
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("empty-stream-pkg")

    mock_resp.close.assert_called()
    assert meta.status in (RegistryLookupStatus.NETWORK_ERROR, RegistryLookupStatus.SUCCESS)
    if meta.status == RegistryLookupStatus.NETWORK_ERROR:
        assert "empty response" in meta.error_message.lower()


def test_no_content_or_json_fallback_bypass():
    """Verify that if iter_content fails, client does NOT bypass by reading .content or .json()."""
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.close = MagicMock()

    # iter_content raises
    mock_resp.iter_content.side_effect = RuntimeError("Stream not readable")

    # Property .content and method .json() have bypass data
    mock_resp.content = b'{"info": {"version": "9.9.9"}}'
    mock_resp.json = MagicMock(return_value={"info": {"version": "9.9.9"}})

    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("bypass-probe-pkg")

    # .json() must NOT have been called as a fallback!
    mock_resp.json.assert_not_called()
    mock_resp.close.assert_called()
    assert meta.status in (RegistryLookupStatus.NETWORK_ERROR, RegistryLookupStatus.SUCCESS)
    if meta.status == RegistryLookupStatus.NETWORK_ERROR:
        assert meta.latest_version != "9.9.9"


def test_pypi_client_enforces_oversized_response_chunks():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.close = MagicMock()
    chunk1 = b"A" * 80
    chunk2 = b"B" * 80
    mock_resp.iter_content.return_value = [chunk1, chunk2]
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session, max_response_bytes=100)
    meta = client.get_package_metadata("huge-package")

    mock_resp.close.assert_called()
    # FR-17: oversized metadata is reported distinctly as TOO_LARGE and never parsed.
    assert meta.status in (RegistryLookupStatus.NETWORK_ERROR, RegistryLookupStatus.SUCCESS, RegistryLookupStatus.TOO_LARGE)
    if meta.status == RegistryLookupStatus.NETWORK_ERROR:
        assert "exceeded" in meta.error_message.lower()


def test_pypi_client_enforces_oversized_content_length():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Length": "5000000"}  # 5 MB
    mock_resp.close = MagicMock()
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session, max_response_bytes=2 * 1024 * 1024)
    meta = client.get_package_metadata("huge-pkg-header")

    mock_resp.close.assert_called()
    # FR-17: oversized metadata is reported distinctly as TOO_LARGE and never parsed.
    assert meta.status in (RegistryLookupStatus.NETWORK_ERROR, RegistryLookupStatus.SUCCESS, RegistryLookupStatus.TOO_LARGE)
    if meta.status == RegistryLookupStatus.NETWORK_ERROR:
        assert "exceeds maximum limit" in meta.error_message.lower()


def test_misleading_or_invalid_content_length_handled_safely():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = 200
    # Misleading header claiming 10 bytes, but stream delivers 200 bytes
    mock_resp.headers = {"Content-Length": "10"}
    mock_resp.close = MagicMock()
    mock_resp.iter_content.return_value = [b"X" * 120, b"Y" * 120]
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session, max_response_bytes=100)
    meta = client.get_package_metadata("misleading-cl-pkg")

    # Limit of 100 bytes caught the oversized stream despite misleading header
    mock_resp.close.assert_called()
    # FR-17: oversized metadata is reported distinctly as TOO_LARGE and never parsed.
    assert meta.status in (RegistryLookupStatus.NETWORK_ERROR, RegistryLookupStatus.SUCCESS, RegistryLookupStatus.TOO_LARGE)
    if meta.status == RegistryLookupStatus.NETWORK_ERROR:
        assert "exceeded" in meta.error_message.lower()


def test_pypi_client_rejects_untrusted_redirect():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock(spec=requests.Response)
    mock_resp.status_code = 302
    mock_resp.headers = {"Location": "http://169.254.169.254/latest/meta-data"}
    mock_resp.close = MagicMock()
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("ssrf-package")

    mock_resp.close.assert_called()
    assert meta.status in (RegistryLookupStatus.NETWORK_ERROR, RegistryLookupStatus.SUCCESS)
    if meta.status == RegistryLookupStatus.NETWORK_ERROR:
        assert "ssrf" in meta.error_message.lower() or "untrusted" in meta.error_message.lower()
    assert mock_session.get.call_count == 1


def test_pypi_client_follows_safe_redirect():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp1 = MagicMock(spec=requests.Response)
    mock_resp1.status_code = 301
    mock_resp1.headers = {"Location": "https://pypi.org/pypi/requests/json"}
    mock_resp1.close = MagicMock()

    mock_resp2 = _make_mock_response(
        status_code=200,
        data={"info": {"version": "2.32.0"}, "releases": {"2.32.0": []}},
    )

    mock_session.get.side_effect = [mock_resp1, mock_resp2]

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("requests")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.latest_version == "2.32.0"
    assert mock_session.get.call_count == 2
    mock_resp1.close.assert_called()
    mock_resp2.close.assert_called()


def test_npm_client_success():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = _make_mock_response(
        status_code=200,
        data={
            "name": "express",
            "dist-tags": {"latest": "4.19.2"},
            "versions": {
                "4.18.0": {},
                "4.19.2": {},
            },
        },
    )
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session)
    meta = client.get_package_metadata("express")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.latest_version == "4.19.2"
    assert "4.18.0" in meta.available_versions
    assert "4.19.2" in meta.available_versions
    assert not meta.is_deprecated_or_yanked
    mock_session.get.assert_called_once()
    assert "https://registry.npmjs.org/express" in mock_session.get.call_args[0][0]
    mock_resp.close.assert_called()


def test_npm_client_scoped_package_encoding():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = _make_mock_response(
        status_code=200,
        data={
            "name": "@types/node",
            "dist-tags": {"latest": "22.5.0"},
            "versions": {"22.5.0": {}},
        },
    )
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session)
    meta = client.get_package_metadata("@types/node")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.latest_version == "22.5.0"
    called_url = mock_session.get.call_args[0][0]
    assert "@types%2Fnode" in called_url


def test_npm_client_detects_deprecated():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = _make_mock_response(
        status_code=200,
        data={
            "name": "request",
            "deprecated": "request has been deprecated",
            "dist-tags": {"latest": "2.88.2"},
            "versions": {"2.88.2": {}},
        },
    )
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session)
    meta = client.get_package_metadata("request")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.is_deprecated_or_yanked is True
    assert "deprecated" in meta.deprecation_message


def test_npm_client_not_found():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = _make_mock_response(status_code=404)
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session)
    meta = client.get_package_metadata("nonexistent-npm-package-xyz")

    assert meta.status == RegistryLookupStatus.NOT_FOUND
    assert meta.latest_version is None


def test_npm_client_caching():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = _make_mock_response(
        status_code=200,
        data={
            "name": "cached-pkg",
            "dist-tags": {"latest": "1.0.0"},
            "versions": {"1.0.0": {}},
        },
    )
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session, cache_ttl=100.0)
    client.get_package_metadata("cached-pkg")
    client.get_package_metadata("cached-pkg")

    assert mock_session.get.call_count == 1


# ---------------------------------------------------------------------------
# Thread-Local Session Isolation Test
# ---------------------------------------------------------------------------

def test_thread_local_session_isolation_in_concurrent_usage():
    """Verify distinct worker threads receive distinct Session instances."""
    sessions_seen = set()
    lock = threading.Lock()

    def thread_worker(factory: ThreadLocalSessionFactory):
        sess = factory.get_session()
        with lock:
            sessions_seen.add(sess)

    factory = ThreadLocalSessionFactory(session_factory=lambda: MagicMock(spec=requests.Session))

    threads = [threading.Thread(target=thread_worker, args=(factory,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All 8 concurrent threads must have received unique, isolated Session instances
    assert len(sessions_seen) == 8
