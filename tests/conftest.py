"""Pytest fixtures and test helper data."""
import os

# The API only answers to loopback Host headers; Starlette's TestClient uses "testserver".
os.environ.setdefault("ALLOWED_HOSTS", '["localhost", "127.0.0.1", "::1", "testserver"]')

from starlette.testclient import TestClient as _StarletteTestClient  # noqa: E402

# Requests from the real web client carry these headers; give TestClient the same defaults.
# Tests that need to omit them pass explicit headers (see tests/test_request_protection.py).
_DEFAULT_CLIENT_HEADERS = {"Origin": "http://localhost:5173", "X-DUA-Client": "web"}
_original_init = _StarletteTestClient.__init__


def _init_with_client_headers(self, *args, headers=None, **kwargs):
    _original_init(self, *args, headers=_DEFAULT_CLIENT_HEADERS if headers is None else headers, **kwargs)


_StarletteTestClient.__init__ = _init_with_client_headers

import pytest

import src.services.run_store as _run_store_module
from src.data.catalog_loader import Catalog, CatalogMetadata, PackageInfo, load_catalog


@pytest.fixture
def mock_catalog() -> Catalog:
    """Fixture returning a controlled mock catalog for deterministic testing."""
    metadata = CatalogMetadata(
        catalog_name="Test Catalog",
        catalog_version="1.0.0-test",
        catalog_timestamp="2026-10-27",
        provenance_notice="Test catalog fixture.",
        verified_live_pypi=False,
    )
    packages = {
        "requests": PackageInfo(
            candidate_version="2.32.3",
            known_versions=["2.25.1", "2.28.0", "2.31.0", "2.32.3"],
            description="Python HTTP library.",
        ),
        "flask": PackageInfo(
            candidate_version="3.1.0",
            known_versions=["2.0.1", "2.2.5", "3.0.0", "3.1.0"],
            description="WSGI web application framework.",
        ),
        "urllib3": PackageInfo(
            candidate_version="2.2.3",
            known_versions=["1.26.5", "2.0.0", "2.2.3"],
            description="HTTP library with connection pooling.",
        ),
        "demo-semver": PackageInfo(
            candidate_version="2.10.0",
            known_versions=["2.8.0", "2.9.0", "2.10.0"],
            description="Package specifically demonstrating semantic ordering vs alphabetical.",
        ),
    }
    return Catalog(metadata=metadata, packages=packages)


@pytest.fixture
def default_catalog() -> Catalog:
    """Fixture returning the real production local catalog."""
    return load_catalog()


@pytest.fixture(autouse=True)
def isolated_run_store(tmp_path):
    """Give every test its own SQLite run store instead of the developer database."""
    previous = _run_store_module._store
    _run_store_module._store = _run_store_module.RunStore(db_path=str(tmp_path / "runs.db"))
    yield _run_store_module._store
    _run_store_module._store = previous


@pytest.fixture(autouse=True)
def no_live_dependency_staging(request, monkeypatch):
    """Unit tests must not download packages. Live staging runs only in tests marked `live_staging`."""
    if request.node.get_closest_marker("live_staging"):
        return
    import src.sandbox.dependency_staging as staging

    def _blocked(*_args, **_kwargs):
        raise RuntimeError("Live dependency staging is disabled in unit tests.")

    monkeypatch.setattr(staging, "run_bounded", _blocked)
