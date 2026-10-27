"""Pytest fixtures and test helper data."""
import pytest
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
