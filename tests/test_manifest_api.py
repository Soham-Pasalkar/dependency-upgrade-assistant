"""Integration tests for FastAPI manifest ingestion endpoints."""
from __future__ import annotations

import io
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.models.manifest import RegistryLookupStatus
from src.services.registry_client import RegistryPackageMetadata


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def mock_registry_lookups():
    """Mock external registry queries to make API tests fully offline and deterministic."""
    with patch(
        "src.services.registry_client.PyPIRegistryClient.get_package_metadata"
    ) as mock_pypi, patch(
        "src.services.registry_client.NpmRegistryClient.get_package_metadata"
    ) as mock_npm:

        def pypi_side_effect(name):
            if name == "requests":
                return RegistryPackageMetadata(
                    package_name="requests",
                    latest_version="2.32.0",
                    available_versions=["2.28.0", "2.31.0", "2.32.0"],
                    status=RegistryLookupStatus.SUCCESS,
                )
            return RegistryPackageMetadata(
                package_name=name,
                latest_version="1.0.0",
                available_versions=["1.0.0"],
                status=RegistryLookupStatus.SUCCESS,
            )

        def npm_side_effect(name):
            if name == "express":
                return RegistryPackageMetadata(
                    package_name="express",
                    latest_version="4.19.2",
                    available_versions=["4.18.0", "4.19.2"],
                    status=RegistryLookupStatus.SUCCESS,
                )
            return RegistryPackageMetadata(
                package_name=name,
                latest_version="1.0.0",
                available_versions=["1.0.0"],
                status=RegistryLookupStatus.SUCCESS,
            )

        mock_pypi.side_effect = pypi_side_effect
        mock_npm.side_effect = npm_side_effect
        yield


def test_analyze_raw_requirements_txt(client):
    payload = {
        "manifest_type": "requirements.txt",
        "content": "requests==2.28.0\npytest>=7.0.0\n",
    }
    resp = client.post("/api/manifest/analyze", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["manifest_type"] == "requirements.txt"
    assert data["ecosystem"] == "python"
    assert data["total_dependencies"] == 2
    assert data["upgrade_available_count"] == 1
    assert data["preserved_count"] == 1

    deps = data["dependencies"]
    req = next(d for d in deps if d["name"] == "requests")
    assert req["status"] == "UPGRADE_AVAILABLE"
    assert req["current_version"] == "2.28.0"
    assert req["latest_version"] == "2.32.0"


def test_analyze_raw_package_json(client):
    payload = {
        "manifest_type": "package.json",
        "content": '{"dependencies": {"express": "^4.18.0"}}',
    }
    resp = client.post("/api/manifest/analyze", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["manifest_type"] == "package.json"
    assert data["ecosystem"] == "npm"
    assert data["total_dependencies"] == 1
    assert data["up_to_date_count"] == 1


def test_analyze_empty_content_fails(client):
    payload = {"manifest_type": "requirements.txt", "content": "   \n  "}
    resp = client.post("/api/manifest/analyze", json=payload)
    assert resp.status_code == 422
    assert "cannot be empty" in resp.json()["detail"]


def test_analyze_malformed_json_fails_gracefully(client):
    payload = {"manifest_type": "package.json", "content": "{ invalid json content "}
    resp = client.post("/api/manifest/analyze", json=payload)
    assert resp.status_code == 422
    data = resp.json()
    assert "Invalid JSON syntax" in data["detail"]
    assert "diagnostics" in data


def test_upload_requirements_file_multipart(client):
    file_bytes = b"requests==2.28.0\n# comment\n"
    files = {"file": ("requirements.txt", io.BytesIO(file_bytes), "text/plain")}
    resp = client.post("/api/manifest/upload", files=files)
    assert resp.status_code == 200
    data = resp.json()
    assert data["manifest_type"] == "requirements.txt"
    assert data["ecosystem"] == "python"
    assert data["total_dependencies"] == 1


def test_upload_package_json_file_multipart(client):
    file_bytes = b'{"dependencies": {"express": "^4.18.0"}}'
    files = {"file": ("package.json", io.BytesIO(file_bytes), "application/json")}
    resp = client.post("/api/manifest/upload", files=files)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ecosystem"] == "npm"
    assert data["total_dependencies"] == 1


def test_upload_with_custom_manifest_type_form_param(client):
    file_bytes = b"requests==2.28.0\n"
    files = {"file": ("custom_manifest.deps", io.BytesIO(file_bytes), "text/plain")}
    data = {"manifest_type": "requirements.txt"}
    resp = client.post("/api/manifest/upload", files=files, data=data)
    assert resp.status_code == 200
    assert resp.json()["ecosystem"] == "python"


def test_upload_unsupported_file_fails(client):
    file_bytes = b"<project></project>"
    files = {"file": ("pom.xml", io.BytesIO(file_bytes), "application/xml")}
    resp = client.post("/api/manifest/upload", files=files)
    assert resp.status_code == 400
    assert "Unsupported file" in resp.json()["detail"]


def test_upload_empty_file_fails(client):
    files = {"file": ("requirements.txt", io.BytesIO(b""), "text/plain")}
    resp = client.post("/api/manifest/upload", files=files)
    assert resp.status_code == 422
    assert "empty" in resp.json()["detail"]


def test_upload_oversized_file_fails(client):
    oversized_bytes = b"A" * (1024 * 1024 + 2048)  # > 1 MB
    files = {"file": ("requirements.txt", io.BytesIO(oversized_bytes), "text/plain")}
    resp = client.post("/api/manifest/upload", files=files)
    assert resp.status_code == 413
    assert "exceeds maximum allowed size" in resp.json()["detail"]


def test_v1_versioned_endpoint_parity(client):
    payload = {
        "manifest_type": "requirements.txt",
        "content": "requests==2.28.0\n",
    }
    resp = client.post("/api/v1/manifest/analyze", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ecosystem"] == "python"
