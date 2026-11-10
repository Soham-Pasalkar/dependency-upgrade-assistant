"""Unit tests for PyPI and npm registry clients."""
from __future__ import annotations

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
    mock_resp.json.return_value = {
        "info": {"version": "2.31.0"},
        "releases": {
            "2.28.0": [{"yanked": False}],
            "2.31.0": [{"yanked": False}],
        },
    }
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
    mock_resp.json.return_value = {
        "info": {"version": "3.0.0"},
        "releases": {
            "3.0.0": [{"yanked": True, "yanked_reason": "Critical regression in auth"}],
        },
    }
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
    mock_resp.json.return_value = {
        "info": {"version": "1.0.0"},
        "releases": {"1.0.0": []},
    }
    mock_session.get.return_value = mock_resp

    client = PyPIRegistryClient(session=mock_session, cache_ttl=100.0)
    meta1 = client.get_package_metadata("cached-package")
    meta2 = client.get_package_metadata("cached-package")

    assert meta1.latest_version == "1.0.0"
    assert meta2.latest_version == "1.0.0"
    # Even though called twice, network get was executed only once
    assert mock_session.get.call_count == 1


def test_pypi_client_fallback_to_local_catalog_on_network_error():
    mock_session = MagicMock(spec=requests.Session)
    mock_session.get.side_effect = requests.ConnectionError("Connection refused")

    client = PyPIRegistryClient(session=mock_session)
    # "requests" exists in the local fallback catalog
    meta = client.get_package_metadata("requests")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.latest_version is not None
    assert "local catalog cache" in meta.deprecation_message


def test_npm_client_success():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "express",
        "dist-tags": {"latest": "4.19.2"},
        "versions": {
            "4.18.0": {},
            "4.19.2": {},
        },
    }
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
    mock_resp.json.return_value = {
        "name": "@types/node",
        "dist-tags": {"latest": "22.5.0"},
        "versions": {"22.5.0": {}},
    }
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session)
    meta = client.get_package_metadata("@types/node")

    assert meta.status == RegistryLookupStatus.SUCCESS
    assert meta.latest_version == "22.5.0"
    # Verify slash is escaped properly as %2F
    called_url = mock_session.get.call_args[0][0]
    assert "@types%2Fnode" in called_url


def test_npm_client_detects_deprecated():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "request",
        "deprecated": "request has been deprecated, see https://github.com/request/request/issues/3142",
        "dist-tags": {"latest": "2.88.2"},
        "versions": {"2.88.2": {}},
    }
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
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session)
    meta = client.get_package_metadata("nonexistent-npm-package-xyz")

    assert meta.status == RegistryLookupStatus.NOT_FOUND
    assert meta.latest_version is None


def test_npm_client_caching():
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "name": "cached-pkg",
        "dist-tags": {"latest": "1.0.0"},
        "versions": {"1.0.0": {}},
    }
    mock_session.get.return_value = mock_resp

    client = NpmRegistryClient(session=mock_session, cache_ttl=100.0)
    client.get_package_metadata("cached-pkg")
    client.get_package_metadata("cached-pkg")

    assert mock_session.get.call_count == 1
