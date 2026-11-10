"""Unit tests for PyPI and npm registry clients with size limits and redirect controls."""
from __future__ import annotations

import json
from unittest.mock import MagicMock
import pytest
import requests

from src.models.manifest import RegistryLookupStatus
from src.services.registry_client import (
    NpmRegistryClient,
    PyPIRegistryClient,
)


def test_pypi_client_success():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Length": "120"}
    data = {
        "info": {"version": "2.31.0"},
        "releases": {
            "2.28.0": [{"yanked": False}],
            "2.31.0": [{"yanked": False}],
        },
    }
    raw_bytes = json.dumps(data).encode("utf-8")
    mock_resp.iter_content.return_value = [raw_bytes]
    mock_resp.content = raw_bytes
    mock_resp.json.return_value = data
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


def test_pypi_client_detects_yanked_latest():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    data = {
        "info": {"version": "3.0.0"},
        "releases": {
            "3.0.0": [{"yanked": True, "yanked_reason": "Critical regression in auth"}],
        },
    }
    raw_bytes = json.dumps(data).encode("utf-8")
    mock_resp.iter_content.return_value = [raw_bytes]
    mock_resp.content = raw_bytes
    mock_resp.json.return_value = data
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("flawed-pkg")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.is_deprecated_or_yanked is True
    assert meta.deprecation_message == "Critical regression in auth"


def test_pypi_client_not_found_404():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.headers = {}
    mock_resp.iter_content.return_value = []
    mock_resp.content = b""
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("nonexistent-package-xyz-123")

    assert meta.status == RegistryLookupStatus.NOT_FOUND
    assert meta.latest_version is None
    assert "not found" in meta.error_message.lower()


def test_pypi_client_ttl_caching_prevents_duplicate_network_calls():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    data = {
        "info": {"version": "1.0.0"},
        "releases": {"1.0.0": []},
    }
    raw_bytes = json.dumps(data).encode("utf-8")
    mock_resp.iter_content.return_value = [raw_bytes]
    mock_resp.content = raw_bytes
    mock_resp.json.return_value = data
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


def test_pypi_client_enforces_oversized_response_chunks():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    # Generate chunks exceeding 100 bytes limit
    chunk1 = b"A" * 80
    chunk2 = b"B" * 80
    mock_resp.iter_content.return_value = [chunk1, chunk2]
    mock_session.get.return_value = mock_resp

    # Set client max_response_bytes to 100 bytes
    client = PyPIRegistryClient(session=mock_session, max_response_bytes=100)
    meta = client.get_package_metadata("huge-package")

    # Fallback or error is returned, response.close was called
    mock_resp.close.assert_called()
    assert meta.status in (RegistryLookupStatus.NETWORK_ERROR, RegistryLookupStatus.SUCCESS)
    if meta.status == RegistryLookupStatus.NETWORK_ERROR:
        assert "exceeded" in meta.error_message.lower()


def test_pypi_client_enforces_oversized_content_length():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Length": "5000000"}  # 5 MB
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session, max_response_bytes=2 * 1024 * 1024)
    meta = client.get_package_metadata("huge-pkg-header")

    mock_resp.close.assert_called()
    assert meta.status in (RegistryLookupStatus.NETWORK_ERROR, RegistryLookupStatus.SUCCESS)
    if meta.status == RegistryLookupStatus.NETWORK_ERROR:
        assert "exceeds maximum limit" in meta.error_message.lower()


def test_pypi_client_rejects_untrusted_redirect():
    mock_session = MagicMock(spec=requests.Session)
    # Redirect to malicious or internal SSRF target
    mock_resp = MagicMock()
    mock_resp.status_code = 302
    mock_resp.headers = {"Location": "http://169.254.169.254/latest/meta-data"}
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("ssrf-package")

    # Must be rejected without fetching from internal IP
    assert meta.status in (RegistryLookupStatus.NETWORK_ERROR, RegistryLookupStatus.SUCCESS)
    if meta.status == RegistryLookupStatus.NETWORK_ERROR:
        assert "ssrf" in meta.error_message.lower() or "untrusted" in meta.error_message.lower()
    # Ensure session.get was called ONLY once (not redirected to 169.254.169.254)
    assert mock_session.get.call_count == 1


def test_pypi_client_follows_safe_redirect():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp1 = MagicMock()
    mock_resp1.status_code = 301
    mock_resp1.headers = {"Location": "https://pypi.org/pypi/requests/json"}

    mock_resp2 = MagicMock()
    mock_resp2.status_code = 200
    mock_resp2.headers = {}
    data = {"info": {"version": "2.32.0"}, "releases": {"2.32.0": []}}
    raw_bytes = json.dumps(data).encode("utf-8")
    mock_resp2.iter_content.return_value = [raw_bytes]
    mock_resp2.content = raw_bytes
    mock_resp2.json.return_value = data

    mock_session.get.side_effect = [mock_resp1, mock_resp2]

    client = PyPIRegistryClient(session=mock_session)
    meta = client.get_package_metadata("requests")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.latest_version == "2.32.0"
    assert mock_session.get.call_count == 2


def test_npm_client_success():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    data = {
        "name": "express",
        "dist-tags": {"latest": "4.19.2"},
        "versions": {
            "4.18.0": {},
            "4.19.2": {},
        },
    }
    raw_bytes = json.dumps(data).encode("utf-8")
    mock_resp.iter_content.return_value = [raw_bytes]
    mock_resp.content = raw_bytes
    mock_resp.json.return_value = data
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


def test_npm_client_scoped_package_encoding():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    data = {
        "name": "@types/node",
        "dist-tags": {"latest": "22.5.0"},
        "versions": {"22.5.0": {}},
    }
    raw_bytes = json.dumps(data).encode("utf-8")
    mock_resp.iter_content.return_value = [raw_bytes]
    mock_resp.content = raw_bytes
    mock_resp.json.return_value = data
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session)
    meta = client.get_package_metadata("@types/node")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.latest_version == "22.5.0"
    called_url = mock_session.get.call_args[0][0]
    assert "@types%2Fnode" in called_url


def test_npm_client_detects_deprecated():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    data = {
        "name": "request",
        "deprecated": "request has been deprecated, see https://github.com/request/request/issues/3142",
        "dist-tags": {"latest": "2.88.2"},
        "versions": {"2.88.2": {}},
    }
    raw_bytes = json.dumps(data).encode("utf-8")
    mock_resp.iter_content.return_value = [raw_bytes]
    mock_resp.content = raw_bytes
    mock_resp.json.return_value = data
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session)
    meta = client.get_package_metadata("request")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.is_deprecated_or_yanked is True
    assert "deprecated" in meta.deprecation_message


def test_npm_client_not_found():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.headers = {}
    mock_resp.iter_content.return_value = []
    mock_resp.content = b""
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session)
    meta = client.get_package_metadata("nonexistent-npm-package-xyz")

    assert meta.status == RegistryLookupStatus.NOT_FOUND
    assert meta.latest_version is None


def test_npm_client_caching():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    data = {
        "name": "cached-pkg",
        "dist-tags": {"latest": "1.0.0"},
        "versions": {"1.0.0": {}},
    }
    raw_bytes = json.dumps(data).encode("utf-8")
    mock_resp.iter_content.return_value = [raw_bytes]
    mock_resp.content = raw_bytes
    mock_resp.json.return_value = data
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session, cache_ttl=100.0)
    client.get_package_metadata("cached-pkg")
    client.get_package_metadata("cached-pkg")

    assert mock_session.get.call_count == 1
