"""Manifest analysis service orchestrating parsing, registry queries, and upgrade classification with bounded concurrency."""
from __future__ import annotations

import concurrent.futures
import logging
from typing import List, Optional, Tuple

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version, parse as parse_version

from src.analysis.manifest_parser import parse_manifest
from src.analysis.semver_evaluator import (
    SemVer,
    extract_pinned_version,
    find_latest_compatible,
    find_latest_version,
    is_supported_semver_range,
    semver_satisfies,
)
from src.models.manifest import (
    AnalyzedDependency,
    DependencyStatus,
    DependencyType,
    Ecosystem,
    ManifestAnalysisResponse,
    ParsedDependency,
    RegistryLookupStatus,
)
from src.services.registry_client import (
    NpmRegistryClient,
    PyPIRegistryClient,
    RegistryPackageMetadata,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_WORKERS = 8


class ManifestService:
    """Coordinates manifest analysis for Python and Node.js ecosystems with bounded concurrency."""

    def __init__(
        self,
        pypi_client: Optional[PyPIRegistryClient] = None,
        npm_client: Optional[NpmRegistryClient] = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> None:
        self.pypi_client = pypi_client or PyPIRegistryClient()
        self.npm_client = npm_client or NpmRegistryClient()
        self.max_workers = max(1, max_workers)

    def analyze_manifest(
        self,
        manifest_type: str,
        content: str,
    ) -> ManifestAnalysisResponse:
        """Parse raw manifest content, query registries concurrently within bounds, and classify dependencies."""
        parsed_deps, diagnostics, ecosystem = parse_manifest(manifest_type, content)

        if not parsed_deps:
            return ManifestAnalysisResponse(
                manifest_type=manifest_type,
                ecosystem=ecosystem,
                total_dependencies=0,
                up_to_date_count=0,
                upgrade_available_count=0,
                preserved_count=0,
                dependencies=[],
                diagnostics=diagnostics,
            )

        def _evaluate_single(idx: int, dep: ParsedDependency) -> Tuple[int, AnalyzedDependency]:
            try:
                if not dep.is_supported:
                    return idx, AnalyzedDependency(
                        name=dep.name,
                        original_name=dep.original_name,
                        ecosystem=dep.ecosystem,
                        dependency_type=dep.dependency_type,
                        declared_specifier=dep.declared_specifier,
                        current_version=dep.current_version,
                        latest_version=None,
                        latest_compatible_version=None,
                        status=DependencyStatus.PRESERVED,
                        status_reason=dep.diagnostic_message or "Preserved non-standard line.",
                        registry_status=RegistryLookupStatus.SKIPPED,
                        line_number=dep.line_number,
                        raw_line=dep.raw_line,
                        diagnostic_message=dep.diagnostic_message,
                    )

                meta = self._query_registry(dep)
                analyzed = self._classify_dependency(dep, meta)
                return idx, analyzed
            except Exception as err:
                logger.error(f"Error evaluating dependency '{dep.name}': {err}", exc_info=True)
                return idx, AnalyzedDependency(
                    name=dep.name,
                    original_name=dep.original_name,
                    ecosystem=dep.ecosystem,
                    dependency_type=dep.dependency_type,
                    declared_specifier=dep.declared_specifier,
                    current_version=dep.current_version,
                    latest_version=None,
                    latest_compatible_version=None,
                    status=DependencyStatus.PRESERVED,
                    status_reason=f"Evaluation error: {err}",
                    registry_status=RegistryLookupStatus.NETWORK_ERROR,
                    line_number=dep.line_number,
                    raw_line=dep.raw_line,
                    diagnostic_message=str(err),
                )

        workers = min(self.max_workers, len(parsed_deps))
        analyzed_deps: List[Optional[AnalyzedDependency]] = [None] * len(parsed_deps)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_evaluate_single, i, dep) for i, dep in enumerate(parsed_deps)]
            for future in concurrent.futures.as_completed(futures):
                idx, analyzed = future.result()
                analyzed_deps[idx] = analyzed

        final_deps: List[AnalyzedDependency] = [d for d in analyzed_deps if d is not None]

        up_to_date = sum(1 for d in final_deps if d.status == DependencyStatus.UP_TO_DATE)
        upgrade_avail = sum(1 for d in final_deps if d.status == DependencyStatus.UPGRADE_AVAILABLE)
        preserved = sum(1 for d in final_deps if d.status == DependencyStatus.PRESERVED)

        return ManifestAnalysisResponse(
            manifest_type=manifest_type,
            ecosystem=ecosystem,
            total_dependencies=len(final_deps),
            up_to_date_count=up_to_date,
            upgrade_available_count=upgrade_avail,
            preserved_count=preserved,
            dependencies=final_deps,
            diagnostics=diagnostics,
        )

    def _query_registry(self, dep: ParsedDependency) -> RegistryPackageMetadata:
        if dep.ecosystem == Ecosystem.PYTHON:
            return self.pypi_client.get_package_metadata(dep.name)
        elif dep.ecosystem == Ecosystem.NPM:
            return self.npm_client.get_package_metadata(dep.original_name)
        return RegistryPackageMetadata(
            package_name=dep.name,
            latest_version=None,
            available_versions=[],
            status=RegistryLookupStatus.SKIPPED,
        )

    def _classify_dependency(
        self,
        dep: ParsedDependency,
        meta: RegistryPackageMetadata,
    ) -> AnalyzedDependency:
        # Check if registry lookup failed
        if meta.status != RegistryLookupStatus.SUCCESS:
            return AnalyzedDependency(
                name=dep.name,
                original_name=dep.original_name,
                ecosystem=dep.ecosystem,
                dependency_type=dep.dependency_type,
                declared_specifier=dep.declared_specifier,
                current_version=dep.current_version,
                latest_version=None,
                latest_compatible_version=None,
                status=DependencyStatus.PRESERVED,
                status_reason=f"Registry lookup {meta.status.value}: {meta.error_message or 'Package not found'}",
                registry_status=meta.status,
                line_number=dep.line_number,
                raw_line=dep.raw_line,
                diagnostic_message=meta.error_message,
            )

        if dep.ecosystem == Ecosystem.PYTHON:
            return self._classify_python(dep, meta)
        else:
            return self._classify_npm(dep, meta)

    def _classify_python(
        self,
        dep: ParsedDependency,
        meta: RegistryPackageMetadata,
    ) -> AnalyzedDependency:
        latest_ver = meta.latest_version
        sample_versions = meta.available_versions[-10:] if meta.available_versions else []

        # If dep has an exact pin (e.g. ==1.0.0)
        if dep.current_version:
            curr_ver_str = dep.current_version
            try:
                curr_v = parse_version(curr_ver_str)
                latest_v = parse_version(latest_ver) if latest_ver else None

                if latest_v and curr_v >= latest_v:
                    status = DependencyStatus.UP_TO_DATE
                    reason = f"Current version ({curr_ver_str}) is up to date with latest release."
                elif latest_v:
                    status = DependencyStatus.UPGRADE_AVAILABLE
                    reason = f"Upgrade available from {curr_ver_str} to {latest_ver}."
                else:
                    status = DependencyStatus.PRESERVED
                    reason = "No valid release versions identified on PyPI."
            except InvalidVersion:
                status = DependencyStatus.PRESERVED
                reason = f"Unparseable version format '{curr_ver_str}'."

            return AnalyzedDependency(
                name=dep.name,
                original_name=dep.original_name,
                ecosystem=dep.ecosystem,
                dependency_type=dep.dependency_type,
                declared_specifier=dep.declared_specifier,
                current_version=curr_ver_str,
                latest_version=latest_ver,
                latest_compatible_version=latest_ver,
                status=status,
                status_reason=reason,
                registry_status=meta.status,
                available_versions=sample_versions,
                yanked_or_deprecated=meta.is_deprecated_or_yanked,
                deprecation_reason=meta.deprecation_message,
                line_number=dep.line_number,
                raw_line=dep.raw_line,
                diagnostic_message=dep.diagnostic_message,
            )

        # Non-pinned Python constraint (e.g. >=2.0.0 or bare *)
        latest_compatible = None
        if dep.declared_specifier and dep.declared_specifier != "*":
            try:
                spec = SpecifierSet(dep.declared_specifier)
                compat_versions = [
                    v for v in meta.available_versions
                    if not parse_version(v).is_prerelease and parse_version(v) in spec
                ]
                if compat_versions:
                    compat_versions.sort(key=parse_version)
                    latest_compatible = compat_versions[-1]
            except (InvalidSpecifier, InvalidVersion):
                pass

        return AnalyzedDependency(
            name=dep.name,
            original_name=dep.original_name,
            ecosystem=dep.ecosystem,
            dependency_type=dep.dependency_type,
            declared_specifier=dep.declared_specifier,
            current_version=None,
            latest_version=latest_ver,
            latest_compatible_version=latest_compatible or latest_ver,
            status=DependencyStatus.PRESERVED,
            status_reason=f"Non-pinned constraint '{dep.declared_specifier}' preserved.",
            registry_status=meta.status,
            available_versions=sample_versions,
            yanked_or_deprecated=meta.is_deprecated_or_yanked,
            deprecation_reason=meta.deprecation_message,
            line_number=dep.line_number,
            raw_line=dep.raw_line,
            diagnostic_message=dep.diagnostic_message,
        )

    def _classify_npm(
        self,
        dep: ParsedDependency,
        meta: RegistryPackageMetadata,
    ) -> AnalyzedDependency:
        latest_ver = meta.latest_version
        sample_versions = meta.available_versions[-10:] if meta.available_versions else []

        # Find latest version satisfying the constraint
        latest_compatible = find_latest_compatible(meta.available_versions, dep.declared_specifier)

        # If exact pin
        if dep.current_version:
            curr_ver_str = dep.current_version
            curr_v = SemVer.parse(curr_ver_str)
            latest_v = SemVer.parse(latest_ver) if latest_ver else None

            if curr_v and latest_v and curr_v >= latest_v:
                status = DependencyStatus.UP_TO_DATE
                reason = f"Current version ({curr_ver_str}) is up to date."
            elif latest_v:
                status = DependencyStatus.UPGRADE_AVAILABLE
                reason = f"Upgrade available from {curr_ver_str} to {latest_ver}."
            else:
                status = DependencyStatus.PRESERVED
                reason = "No valid release versions identified on npm."

            return AnalyzedDependency(
                name=dep.name,
                original_name=dep.original_name,
                ecosystem=dep.ecosystem,
                dependency_type=dep.dependency_type,
                declared_specifier=dep.declared_specifier,
                current_version=curr_ver_str,
                latest_version=latest_ver,
                latest_compatible_version=latest_compatible or latest_ver,
                status=status,
                status_reason=reason,
                registry_status=meta.status,
                available_versions=sample_versions,
                yanked_or_deprecated=meta.is_deprecated_or_yanked,
                deprecation_reason=meta.deprecation_message,
                line_number=dep.line_number,
                raw_line=dep.raw_line,
                diagnostic_message=dep.diagnostic_message,
            )

        # Range specifier (e.g. ^1.2.3, ~2.0.0, >=3.0.0)
        curr_v = SemVer.parse(dep.declared_specifier.lstrip("^~>="))
        latest_v = SemVer.parse(latest_ver) if latest_ver else None
        compat_v = SemVer.parse(latest_compatible) if latest_compatible else None

        if latest_v and compat_v and compat_v >= latest_v:
            status = DependencyStatus.UP_TO_DATE
            reason = f"Constraint '{dep.declared_specifier}' already covers latest release {latest_ver}."
        elif latest_v:
            status = DependencyStatus.UPGRADE_AVAILABLE
            reason = f"Newer version available ({latest_ver}); latest compatible with '{dep.declared_specifier}' is {latest_compatible or 'none'}."
        else:
            status = DependencyStatus.PRESERVED
            reason = f"Constraint '{dep.declared_specifier}' preserved."

        return AnalyzedDependency(
            name=dep.name,
            original_name=dep.original_name,
            ecosystem=dep.ecosystem,
            dependency_type=dep.dependency_type,
            declared_specifier=dep.declared_specifier,
            current_version=str(curr_v) if curr_v else None,
            latest_version=latest_ver,
            latest_compatible_version=latest_compatible,
            status=status,
            status_reason=reason,
            registry_status=meta.status,
            available_versions=sample_versions,
            yanked_or_deprecated=meta.is_deprecated_or_yanked,
            deprecation_reason=meta.deprecation_message,
            line_number=dep.line_number,
            raw_line=dep.raw_line,
            diagnostic_message=dep.diagnostic_message,
        )
