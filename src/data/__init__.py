"""Data package for Dependency Upgrade Assistant."""
from src.data.catalog_loader import Catalog, CatalogMetadata, PackageInfo, load_catalog

__all__ = ["Catalog", "CatalogMetadata", "PackageInfo", "load_catalog"]
