"""Catalog loader and models for local package version dataset."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Dict, List, Optional


def normalize_package_name(name: str) -> str:
    """Normalize a package name according to PEP 503 rules."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class PackageInfo:
    """Represents version information for a package in the local catalog."""
    candidate_version: str
    known_versions: List[str]
    description: str


@dataclass(frozen=True)
class CatalogMetadata:
    """Provenance and description metadata for the catalog."""
    catalog_name: str
    catalog_version: str
    catalog_timestamp: str
    provenance_notice: str
    verified_live_pypi: bool = False


class Catalog:
    """In-memory representation of the local package catalog."""

    def __init__(self, metadata: CatalogMetadata, packages: Dict[str, PackageInfo]) -> None:
        self.metadata = metadata
        # Normalized key -> PackageInfo
        self._packages = {normalize_package_name(k): v for k, v in packages.items()}
        # Original name mapping for display
        self._original_names = {normalize_package_name(k): k for k in packages.keys()}

    def get_package(self, package_name: str) -> Optional[PackageInfo]:
        """Lookup package info using PEP 503 normalized name."""
        return self._packages.get(normalize_package_name(package_name))

    def get_candidate_version(self, package_name: str) -> Optional[str]:
        """Retrieve the recommended candidate version for a package if present."""
        info = self.get_package(package_name)
        return info.candidate_version if info else None

    def has_package(self, package_name: str) -> bool:
        """Check if package exists in the local catalog."""
        return normalize_package_name(package_name) in self._packages

    def list_packages(self) -> List[str]:
        """Return sorted list of package names."""
        return sorted(self._original_names.values())

    def __len__(self) -> int:
        return len(self._packages)


DEFAULT_CATALOG_PATH = Path(__file__).parent / "package_catalog.json"


def load_catalog(catalog_path: Optional[Path | str] = None) -> Catalog:
    """Load and validate the local package version catalog JSON file.
    
    Raises:
        FileNotFoundError: If the catalog file does not exist.
        ValueError: If JSON structure is invalid.
    """
    path = Path(catalog_path) if catalog_path else DEFAULT_CATALOG_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Package catalog file not found at: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as err:
        raise ValueError(f"Invalid JSON format in catalog file {path}: {err}") from err

    meta_raw = data.get("metadata", {})
    metadata = CatalogMetadata(
        catalog_name=meta_raw.get("catalog_name", "Local Package Catalog"),
        catalog_version=meta_raw.get("catalog_version", "1.0.0"),
        catalog_timestamp=meta_raw.get("catalog_timestamp", "2026-10-27"),
        provenance_notice=meta_raw.get(
            "provenance_notice",
            "Candidate versions are from a local dataset and not verified against live PyPI."
        ),
        verified_live_pypi=meta_raw.get("verified_live_pypi", False),
    )

    packages_raw = data.get("packages", {})
    packages: Dict[str, PackageInfo] = {}
    for name, pdata in packages_raw.items():
        packages[name] = PackageInfo(
            candidate_version=str(pdata.get("candidate_version", "")),
            known_versions=[str(v) for v in pdata.get("known_versions", [])],
            description=str(pdata.get("description", "")),
        )

    return Catalog(metadata=metadata, packages=packages)
