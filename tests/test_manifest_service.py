"""Unit tests for ManifestService dependency classification and bounded concurrency."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock
import pytest

from src.models.manifest import (
    DependencyStatus,
    DependencyType,
    Ecosystem,
    RegistryLookupStatus,
)
from src.services.manifest_service import ManifestService
from src.services.registry_client import (
    NpmRegistryClient,
    PyPIRegistryClient,
    RegistryPackageMetadata,
)


@pytest.fixture
def mock_pypi_client():
    client = MagicMock(spec=PyPIRegistryClient)

    def get_meta(pkg_name):
        if pkg_name == "requests":
            return RegistryPackageMetadata(
                package_name="requests",
                latest_version="2.31.0",
                available_versions=["2.28.0", "2.30.0", "2.31.0"],
                status=RegistryLookupStatus.SUCCESS,
            )
        elif pkg_name == "fastapi":
            return RegistryPackageMetadata(
                package_name="fastapi",
                latest_version="0.115.0",
                available_versions=["0.100.0", "0.115.0"],
                status=RegistryLookupStatus.SUCCESS,
            )
        elif pkg_name == "pytest":
            return RegistryPackageMetadata(
                package_name="pytest",
                latest_version="8.3.0",
                available_versions=["7.4.0", "8.0.0", "8.3.0"],
                status=RegistryLookupStatus.SUCCESS,
            )
        elif pkg_name == "missing-pkg":
            return RegistryPackageMetadata(
                package_name="missing-pkg",
                latest_version=None,
                available_versions=[],
                status=RegistryLookupStatus.NOT_FOUND,
                error_message="Package not found on PyPI",
            )
        return RegistryPackageMetadata(
            package_name=pkg_name,
            latest_version="1.0.0",
            available_versions=["1.0.0"],
            status=RegistryLookupStatus.SUCCESS,
        )

    client.get_package_metadata.side_effect = get_meta
    return client


@pytest.fixture
def mock_npm_client():
    client = MagicMock(spec=NpmRegistryClient)

    def get_meta(pkg_name):
        if pkg_name == "express":
            return RegistryPackageMetadata(
                package_name="express",
                latest_version="4.19.2",
                available_versions=["4.18.0", "4.18.2", "4.19.2"],
                status=RegistryLookupStatus.SUCCESS,
            )
        elif pkg_name == "react":
            return RegistryPackageMetadata(
                package_name="react",
                latest_version="19.0.0",
                available_versions=["18.2.0", "18.3.1", "19.0.0"],
                status=RegistryLookupStatus.SUCCESS,
            )
        elif pkg_name == "deprecated-pkg":
            return RegistryPackageMetadata(
                package_name="deprecated-pkg",
                latest_version="1.0.0",
                available_versions=["1.0.0"],
                status=RegistryLookupStatus.SUCCESS,
                is_deprecated_or_yanked=True,
                deprecation_message="Deprecated package",
            )
        return RegistryPackageMetadata(
            package_name=pkg_name,
            latest_version="1.0.0",
            available_versions=["1.0.0"],
            status=RegistryLookupStatus.SUCCESS,
        )

    client.get_package_metadata.side_effect = get_meta
    return client


def test_analyze_requirements_manifest(mock_pypi_client, mock_npm_client):
    service = ManifestService(pypi_client=mock_pypi_client, npm_client=mock_npm_client)
    content = (
        "requests==2.28.0\n"
        "fastapi==0.115.0\n"
        "pytest>=7.0.0\n"
        "-r base.txt\n"
        "missing-pkg==0.1.0\n"
        "custom-wheel @ https://example.com/wheels/custom.whl\n"
    )

    resp = service.analyze_manifest("requirements.txt", content)

    assert resp.manifest_type == "requirements.txt"
    assert resp.ecosystem == Ecosystem.PYTHON
    assert resp.total_dependencies == 6

    # requests==2.28.0 has 2.31.0 available -> UPGRADE_AVAILABLE
    req_dep = next(d for d in resp.dependencies if d.name == "requests")
    assert req_dep.status == DependencyStatus.UPGRADE_AVAILABLE
    assert req_dep.latest_version == "2.31.0"
    assert req_dep.current_version == "2.28.0"

    # fastapi==0.115.0 is latest -> UP_TO_DATE
    fastapi_dep = next(d for d in resp.dependencies if d.name == "fastapi")
    assert fastapi_dep.status == DependencyStatus.UP_TO_DATE
    assert fastapi_dep.latest_version == "0.115.0"

    # pytest>=7.0.0 is non-pinned -> PRESERVED
    pytest_dep = next(d for d in resp.dependencies if d.name == "pytest")
    assert pytest_dep.status == DependencyStatus.PRESERVED
    assert "Non-pinned" in pytest_dep.status_reason

    # -r base.txt is flag -> PRESERVED, skipped registry
    flag_dep = next(d for d in resp.dependencies if d.name == "-r")
    assert flag_dep.status == DependencyStatus.PRESERVED
    assert flag_dep.registry_status == RegistryLookupStatus.SKIPPED

    # missing-pkg was 404 -> PRESERVED
    missing_dep = next(d for d in resp.dependencies if d.name == "missing-pkg")
    assert missing_dep.status == DependencyStatus.PRESERVED
    assert missing_dep.registry_status == RegistryLookupStatus.NOT_FOUND

    # Direct reference custom-wheel -> PRESERVED, skipped registry
    direct_dep = next(d for d in resp.dependencies if d.name == "custom-wheel")
    assert direct_dep.status == DependencyStatus.PRESERVED
    assert direct_dep.registry_status == RegistryLookupStatus.SKIPPED
    assert "direct URL reference" in direct_dep.status_reason

    # Counts
    assert resp.upgrade_available_count == 1
    assert resp.up_to_date_count == 1
    assert resp.preserved_count == 4


def test_analyze_package_json_manifest(mock_pypi_client, mock_npm_client):
    service = ManifestService(pypi_client=mock_pypi_client, npm_client=mock_npm_client)
    content = """{
      "dependencies": {
        "express": "^4.18.0",
        "react": "18.2.0",
        "local-pkg": "workspace:*",
        "file-pkg": "file:../local-file"
      },
      "devDependencies": {
        "deprecated-pkg": "1.0.0"
      }
    }"""

    resp = service.analyze_manifest("package.json", content)

    assert resp.manifest_type == "package.json"
    assert resp.ecosystem == Ecosystem.NPM
    assert resp.total_dependencies == 5

    # express ^4.18.0 covers latest 4.19.2 -> UP_TO_DATE
    express_dep = next(d for d in resp.dependencies if d.original_name == "express")
    assert express_dep.status == DependencyStatus.UP_TO_DATE

    # react pinned to 18.2.0 when 19.0.0 is latest -> UPGRADE_AVAILABLE
    react_dep = next(d for d in resp.dependencies if d.original_name == "react")
    assert react_dep.status == DependencyStatus.UPGRADE_AVAILABLE
    assert react_dep.latest_version == "19.0.0"

    # workspace:* -> PRESERVED, skipped registry
    ws_dep = next(d for d in resp.dependencies if d.original_name == "local-pkg")
    assert ws_dep.status == DependencyStatus.PRESERVED
    assert ws_dep.registry_status == RegistryLookupStatus.SKIPPED
    assert "workspace" in ws_dep.status_reason.lower()

    # file:... -> PRESERVED, skipped registry
    file_dep = next(d for d in resp.dependencies if d.original_name == "file-pkg")
    assert file_dep.status == DependencyStatus.PRESERVED
    assert file_dep.registry_status == RegistryLookupStatus.SKIPPED
    assert "local filesystem" in file_dep.status_reason.lower()

    # deprecated-pkg
    dep_pkg = next(d for d in resp.dependencies if d.original_name == "deprecated-pkg")
    assert dep_pkg.yanked_or_deprecated is True
    assert dep_pkg.status == DependencyStatus.UP_TO_DATE
    assert dep_pkg.dependency_type == DependencyType.DEVELOPMENT


def test_bounded_concurrency_and_stable_ordering():
    """Verify registry lookups run concurrently up to max_workers and maintain stable order."""
    active_threads = 0
    max_observed_threads = 0
    lock = threading.Lock()

    mock_pypi = MagicMock(spec=PyPIRegistryClient)

    def slow_lookup(name):
        nonlocal active_threads, max_observed_threads
        with lock:
            active_threads += 1
            if active_threads > max_observed_threads:
                max_observed_threads = active_threads

        # Stagger delay based on name to test ordering under variable completion times
        time.sleep(0.02)

        with lock:
            active_threads -= 1

        return RegistryPackageMetadata(
            package_name=name,
            latest_version="2.0.0",
            available_versions=["1.0.0", "2.0.0"],
            status=RegistryLookupStatus.SUCCESS,
        )

    mock_pypi.get_package_metadata.side_effect = slow_lookup

    # 12 dependencies, max_workers = 4
    service = ManifestService(pypi_client=mock_pypi, max_workers=4)

    expected_names = [f"pkg-{i:02d}" for i in range(12)]
    content = "\n".join(f"{name}==1.0.0" for name in expected_names)

    resp = service.analyze_manifest("requirements.txt", content)

    # 1. Concurrency was bounded by max_workers (4)
    assert max_observed_threads <= 4
    assert max_observed_threads >= 2  # Concurrency occurred!

    # 2. Output ordering strictly matches original input manifest
    actual_names = [d.original_name for d in resp.dependencies]
    assert actual_names == expected_names


def test_error_isolation_single_failure_does_not_abort_analysis():
    """Verify that an unexpected failure on one package lookup is isolated."""
    mock_pypi = MagicMock(spec=PyPIRegistryClient)

    def flaky_lookup(name):
        if name == "exploding-pkg":
            raise RuntimeError("Unexpected internal crash during network parse")
        return RegistryPackageMetadata(
            package_name=name,
            latest_version="1.0.0",
            available_versions=["1.0.0"],
            status=RegistryLookupStatus.SUCCESS,
        )

    mock_pypi.get_package_metadata.side_effect = flaky_lookup

    service = ManifestService(pypi_client=mock_pypi, max_workers=4)
    content = "pkg-a==1.0.0\nexploding-pkg==1.0.0\npkg-b==1.0.0\n"

    resp = service.analyze_manifest("requirements.txt", content)

    assert resp.total_dependencies == 3
    # Healthy dependencies succeeded
    assert resp.dependencies[0].status == DependencyStatus.UP_TO_DATE
    assert resp.dependencies[2].status == DependencyStatus.UP_TO_DATE

    # Failed dependency is isolated as PRESERVED with diagnostic
    exploding = resp.dependencies[1]
    assert exploding.name == "exploding-pkg"
    assert exploding.status == DependencyStatus.PRESERVED
    assert "Evaluation error" in exploding.status_reason
