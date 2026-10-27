"""Unit tests for the catalog loader and dataset integrity."""
import json
import pytest
from src.data.catalog_loader import (
    Catalog,
    CatalogMetadata,
    PackageInfo,
    load_catalog,
    normalize_package_name,
)


def test_load_default_catalog(default_catalog: Catalog):
    """Verify that the default 20-package catalog loads with expected metadata."""
    assert default_catalog is not None
    assert len(default_catalog) == 20
    assert default_catalog.metadata.verified_live_pypi is False
    assert "predefined local dataset" in default_catalog.metadata.provenance_notice.lower()


def test_package_normalization():
    """Verify PEP 503 normalization (hyphens, underscores, case)."""
    assert normalize_package_name("Scikit-Learn") == "scikit-learn"
    assert normalize_package_name("scikit_learn") == "scikit-learn"
    assert normalize_package_name("PyYAML") == "pyyaml"
    assert normalize_package_name("Demo.Package") == "demo-package"


def test_catalog_package_retrieval(mock_catalog: Catalog):
    """Test retrieving packages and candidate versions with different casings."""
    assert mock_catalog.has_package("requests") is True
    assert mock_catalog.has_package("REQUESTS") is True
    assert mock_catalog.has_package("non-existent-pkg") is False

    cand = mock_catalog.get_candidate_version("requests")
    assert cand == "2.32.3"

    cand_upper = mock_catalog.get_candidate_version("Requests")
    assert cand_upper == "2.32.3"


def test_catalog_missing_file_raises_error(tmp_path):
    """Test that attempting to load a non-existent catalog file raises FileNotFoundError."""
    missing_path = tmp_path / "non_existent.json"
    with pytest.raises(FileNotFoundError):
        load_catalog(missing_path)


def test_catalog_invalid_json_raises_error(tmp_path):
    """Test that corrupted JSON raises ValueError."""
    corrupted_file = tmp_path / "bad.json"
    corrupted_file.write_text("NOT_JSON{", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON format"):
        load_catalog(corrupted_file)
