"""Deterministic dependency analysis engine with PyPI metadata and Ollama advisory risk assessment.

Implements a multi-tier evaluation system:
1. Candidate Version Availability: Reliable metadata lookup via PyPI JSON API
   (with fallback to local catalog for offline/private packages).
2. Declared Constraint Satisfaction: Evaluation using packaging.specifiers.
3. Cross-Manifest Conflict Detection: Identifies conflicting constraints across manifests.
4. AI Compatibility Risk Assessment: Structured qualitative advisory explanations from Ollama.

Enforces strict safety boundaries:
- Never treats AI assessments as proof of compatibility.
- Filters out prereleases, yanked releases, and releases incompatible with Python 3.10–3.14.
- Caches PyPI queries in memory to prevent redundant network calls.
- Never executes build backends or repository code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from packaging.requirements import Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version, parse as parse_version

from src.analysis.manifest_scanner import DiscoveredManifest, ManifestType
from src.analysis.pyproject_parser import ParsedPyProjectDependency, parse_pyproject_toml
from src.core.comparator import compare_requirements
from src.core.models import EvaluationResult, LineType, RequirementLine, UpgradeStatus
from src.core.parser import parse_requirements_content
from src.data.catalog_loader import Catalog, load_catalog, normalize_package_name
from src.services.ollama_client import OllamaClient, OllamaError

logger = logging.getLogger(__name__)

DEFAULT_PYPI_TIMEOUT = 5.0
TARGET_PYTHON_VERSION = "3.14.3"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AIRiskAssessment:
    """Structured qualitative risk evaluation from Ollama."""
    package_name: str
    current_version: str
    candidate_version: str
    risk_level: str                         # "LOW", "MEDIUM", "HIGH", or "UNKNOWN"
    breaking_changes_summary: str
    deprecated_features: List[str]
    migration_notes: str
    confidence: str                         # "HIGH", "MEDIUM", "LOW"
    model_used: Optional[str] = None
    is_advisory: bool = True
    error_message: Optional[str] = None

    @property
    def has_error(self) -> bool:
        return self.error_message is not None


@dataclass
class AnalyzedDependency:
    """Comprehensive multi-tier analysis for a single dependency declaration."""
    package_name: str
    source_file: str                        # Relative manifest path
    raw_declaration: str
    current_specifier: str
    current_version: Optional[str]          # Pinned version if exact pin
    candidate_version: Optional[str]        # Proposed candidate version
    has_candidate_upgrade: bool
    satisfies_current_constraint: bool      # Tier 2: Does candidate satisfy current spec?
    requires_python_compatibility: bool    # Compatible with target Python?
    status_message: str
    provenance: str                         # "PyPI JSON API" or "Local Catalog"
    ai_risk_assessment: Optional[AIRiskAssessment] = None
    is_exact_pin: bool = False
    extras: Optional[str] = None
    environment_marker: Optional[str] = None


@dataclass
class ManifestDependencyAnalysis:
    """Analysis results for a single manifest file."""
    manifest: DiscoveredManifest
    dependencies: List[AnalyzedDependency]
    outdated_count: int = 0
    up_to_date_count: int = 0
    preserved_count: int = 0


@dataclass
class RepositoryDependencyReport:
    """Aggregated repository-wide dependency analysis report."""
    manifest_analyses: List[ManifestDependencyAnalysis]
    conflicts: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)  # pkg -> [(manifest, spec)]
    total_dependencies: int = 0
    total_outdated: int = 0
    total_conflicts: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def has_outdated(self) -> bool:
        return self.total_outdated > 0

    @property
    def has_conflicts(self) -> bool:
        return self.total_conflicts > 0


# ---------------------------------------------------------------------------
# PyPI Metadata Retrieval & Caching
# ---------------------------------------------------------------------------

class PyPIMetadataClient:
    """Lightweight in-memory cached client for the PyPI JSON API."""

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        timeout: float = DEFAULT_PYPI_TIMEOUT,
        catalog: Optional[Catalog] = None,
    ) -> None:
        self._session = session or requests.Session()
        self.timeout = timeout
        self.catalog = catalog or load_catalog()
        self._cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def get_package_data(self, package_name: str) -> Optional[Dict[str, Any]]:
        """Retrieve package metadata from PyPI JSON API, with in-memory caching."""
        norm_name = normalize_package_name(package_name)
        if norm_name in self._cache:
            return self._cache[norm_name]

        url = f"https://pypi.org/pypi/{norm_name}/json"
        try:
            resp = self._session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                self._cache[norm_name] = data
                return data
            elif resp.status_code == 404:
                self._cache[norm_name] = None
                return None
            else:
                logger.warning(f"PyPI returned HTTP {resp.status_code} for {package_name}")
                self._cache[norm_name] = None
                return None
        except Exception as err:
            logger.debug(f"Network error querying PyPI for {package_name}: {err}")
            # Cache failure temporarily to prevent repeated slow timeouts
            self._cache[norm_name] = None
            return None

    def find_candidate_version(
        self,
        package_name: str,
        current_version_str: Optional[str] = None,
        allow_prereleases: bool = False,
        target_python: str = TARGET_PYTHON_VERSION,
    ) -> Tuple[Optional[str], str, bool]:
        """Find the latest compatible stable candidate version for a package.
        
        Returns:
            Tuple of (candidate_version, provenance, python_compatible)
        """
        norm_name = normalize_package_name(package_name)
        pypi_data = self.get_package_data(norm_name)

        current_ver: Optional[Version] = None
        if current_version_str:
            try:
                current_ver = parse_version(current_version_str)
            except InvalidVersion:
                pass

        # 1. Evaluate via PyPI data if available
        if pypi_data and isinstance(pypi_data, dict):
            releases = pypi_data.get("releases", {})
            valid_candidates: List[Tuple[Version, bool]] = []

            target_py_v = parse_version(target_python)

            for ver_str, files in releases.items():
                try:
                    ver_obj = parse_version(ver_str)
                except InvalidVersion:
                    continue

                # Filter out prereleases unless current is prerelease or allowed
                if ver_obj.is_prerelease and not allow_prereleases:
                    if not (current_ver and current_ver.is_prerelease):
                        continue

                # Check if release is yanked
                if isinstance(files, list) and files:
                    all_yanked = all(f.get("yanked", False) for f in files if isinstance(f, dict))
                    if all_yanked:
                        continue

                # Check Python version requirement
                py_compat = True
                requires_python = None
                if isinstance(files, list):
                    for f in files:
                        if isinstance(f, dict) and f.get("requires_python"):
                            requires_python = f.get("requires_python")
                            break
                if not requires_python and isinstance(pypi_data.get("info"), dict):
                    requires_python = pypi_data["info"].get("requires_python")

                if requires_python:
                    try:
                        spec = SpecifierSet(requires_python)
                        if target_py_v not in spec:
                            py_compat = False
                    except InvalidSpecifier:
                        pass

                # If strictly greater than current version
                if current_ver is None or ver_obj > current_ver:
                    valid_candidates.append((ver_obj, py_compat))

            # Prefer python-compatible candidate if available
            compat_candidates = [v for v, c in valid_candidates if c]
            if compat_candidates:
                best = max(compat_candidates)
                return str(best), "PyPI JSON API", True
            elif valid_candidates:
                best = max([v for v, _ in valid_candidates])
                return str(best), "PyPI JSON API", False

        # 2. Fallback to deterministic local catalog
        if self.catalog.has_package(norm_name):
            cat_cand = self.catalog.get_candidate_version(norm_name)
            if cat_cand:
                try:
                    cand_v = parse_version(cat_cand)
                    if current_ver is None or cand_v > current_ver:
                        return cat_cand, "Local Catalog (Fallback)", True
                except InvalidVersion:
                    pass

        return None, "None", True


# ---------------------------------------------------------------------------
# AI Risk Assessment via Ollama
# ---------------------------------------------------------------------------

RISK_ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "risk_level": {
            "type": "string",
            "enum": ["LOW", "MEDIUM", "HIGH"],
        },
        "breaking_changes_summary": {"type": "string"},
        "deprecated_features": {
            "type": "array",
            "items": {"type": "string"},
        },
        "migration_notes": {"type": "string"},
        "confidence": {
            "type": "string",
            "enum": ["HIGH", "MEDIUM", "LOW"],
        },
    },
    "required": ["risk_level", "breaking_changes_summary", "deprecated_features", "migration_notes"],
}


def assess_upgrade_risk_with_ollama(
    client: OllamaClient,
    model: str,
    package_name: str,
    current_version: str,
    candidate_version: str,
    package_summary: Optional[str] = None,
    timeout: float = 30.0,
) -> AIRiskAssessment:
    """Query local Ollama runtime for qualitative advisory risk analysis on a candidate upgrade.
    
    Advisory Notice:
        The output is strictly qualitative advice based on training data.
        It is never treated as verified proof of compatibility.
    """
    summary_text = f" ({package_summary})" if package_summary else ""
    prompt = (
        f"You are a Python software architect evaluating a dependency upgrade.\n"
        f"Package: {package_name}{summary_text}\n"
        f"Current Version: {current_version}\n"
        f"Proposed Candidate Version: {candidate_version}\n\n"
        "Assess potential compatibility risks, breaking changes, API deprecations, "
        "and necessary migration steps between these versions.\n"
        "Return your assessment strictly in the requested JSON format."
    )

    messages = [
        {"role": "system", "content": "You are a specialized Python dependency compatibility auditor. Answer strictly in JSON."},
        {"role": "user", "content": prompt},
    ]

    try:
        parsed_json, _ = client.chat_json(
            model=model,
            messages=messages,
            schema=RISK_ASSESSMENT_SCHEMA,
            options={"temperature": 0.1},
            timeout=timeout,
        )

        risk_level = str(parsed_json.get("risk_level", "UNKNOWN")).upper()
        if risk_level not in ("LOW", "MEDIUM", "HIGH"):
            risk_level = "UNKNOWN"

        breaking = str(parsed_json.get("breaking_changes_summary", "None reported."))
        deprecated = parsed_json.get("deprecated_features", [])
        if not isinstance(deprecated, list):
            deprecated = [str(deprecated)]

        migration = str(parsed_json.get("migration_notes", "Standard testing recommended."))
        confidence = str(parsed_json.get("confidence", "MEDIUM")).upper()

        return AIRiskAssessment(
            package_name=package_name,
            current_version=current_version,
            candidate_version=candidate_version,
            risk_level=risk_level,
            breaking_changes_summary=breaking,
            deprecated_features=[str(d) for d in deprecated],
            migration_notes=migration,
            confidence=confidence,
            model_used=model,
            is_advisory=True,
        )

    except OllamaError as err:
        return AIRiskAssessment(
            package_name=package_name,
            current_version=current_version,
            candidate_version=candidate_version,
            risk_level="UNKNOWN",
            breaking_changes_summary="AI risk analysis unavailable.",
            deprecated_features=[],
            migration_notes="Verify test suite in isolated sandbox.",
            confidence="LOW",
            model_used=model,
            is_advisory=True,
            error_message=f"Ollama error: {err}",
        )
    except Exception as exc:
        return AIRiskAssessment(
            package_name=package_name,
            current_version=current_version,
            candidate_version=candidate_version,
            risk_level="UNKNOWN",
            breaking_changes_summary="AI risk assessment failed.",
            deprecated_features=[],
            migration_notes="Verify test suite in isolated sandbox.",
            confidence="LOW",
            model_used=model,
            is_advisory=True,
            error_message=f"Unexpected error: {exc}",
        )


# ---------------------------------------------------------------------------
# Dependency Analysis Coordinator
# ---------------------------------------------------------------------------

class DependencyAnalyzer:
    """Orchestrates deterministic version evaluation and advisory AI risk querying."""

    def __init__(
        self,
        metadata_client: Optional[PyPIMetadataClient] = None,
        ollama_client: Optional[OllamaClient] = None,
        ollama_model: Optional[str] = None,
    ) -> None:
        self.metadata_client = metadata_client or PyPIMetadataClient()
        self.ollama_client = ollama_client
        self.ollama_model = ollama_model

    def analyze_requirement_line(
        self,
        line: RequirementLine,
        source_manifest: str,
        enable_ai_risk: bool = False,
    ) -> AnalyzedDependency:
        """Analyze a single parsed RequirementLine against metadata and optional AI risk."""
        pkg_name = line.package_name or "Unknown"
        raw_decl = line.raw_text.strip()
        spec_str = line.operator or ""
        curr_ver = line.version if line.line_type == LineType.EXACT_PIN else None
        is_exact = line.line_type == LineType.EXACT_PIN

        # Query candidate version
        cand_ver, provenance, py_compat = self.metadata_client.find_candidate_version(
            package_name=pkg_name,
            current_version_str=curr_ver,
        )

        has_upgrade = bool(cand_ver is not None and (curr_ver is None or cand_ver != curr_ver))

        # Tier 2: Check if candidate satisfies current constraint
        satisfies_current = False
        if cand_ver and spec_str:
            try:
                # If exact pin, candidate is newer, so it does not satisfy the exact pin
                if is_exact:
                    satisfies_current = (cand_ver == curr_ver)
                else:
                    spec_set = SpecifierSet(spec_str)
                    satisfies_current = parse_version(cand_ver) in spec_set
            except (InvalidSpecifier, InvalidVersion):
                satisfies_current = False

        status_msg = ""
        if has_upgrade:
            status_msg = f"Candidate upgrade available ({cand_ver}) via {provenance}."
            if not py_compat:
                status_msg += " (Warning: Potential Python version incompatibility)."
        elif curr_ver:
            status_msg = f"Already up-to-date with latest metadata ({curr_ver})."
        else:
            status_msg = "Non-pinned requirement preserved per project policy."

        # Optional Tier 4: AI Risk assessment
        ai_assessment: Optional[AIRiskAssessment] = None
        if enable_ai_risk and has_upgrade and curr_ver and cand_ver and self.ollama_client and self.ollama_model:
            ai_assessment = assess_upgrade_risk_with_ollama(
                client=self.ollama_client,
                model=self.ollama_model,
                package_name=pkg_name,
                current_version=curr_ver,
                candidate_version=cand_ver,
            )

        return AnalyzedDependency(
            package_name=pkg_name,
            source_file=source_manifest,
            raw_declaration=raw_decl,
            current_specifier=spec_str,
            current_version=curr_ver,
            candidate_version=cand_ver,
            has_candidate_upgrade=has_upgrade,
            satisfies_current_constraint=satisfies_current,
            requires_python_compatibility=py_compat,
            status_message=status_msg,
            provenance=provenance,
            ai_risk_assessment=ai_assessment,
            is_exact_pin=is_exact,
            extras=line.extras,
            environment_marker=line.environment_marker,
        )

    def analyze_manifest(
        self,
        manifest: DiscoveredManifest,
        enable_ai_risk: bool = False,
    ) -> ManifestDependencyAnalysis:
        """Analyze all dependencies declared in a given manifest."""
        analyzed_deps: List[AnalyzedDependency] = []

        if manifest.manifest_type in (ManifestType.REQUIREMENTS_TXT, ManifestType.CONSTRAINTS_TXT):
            content = manifest.path.read_text(encoding="utf-8", errors="replace")
            req_lines = parse_requirements_content(content)
            for rline in req_lines:
                if rline.package_name:
                    dep = self.analyze_requirement_line(
                        rline,
                        source_manifest=manifest.relative_path,
                        enable_ai_risk=enable_ai_risk,
                    )
                    analyzed_deps.append(dep)

        elif manifest.manifest_type == ManifestType.PYPROJECT_TOML:
            parse_res = parse_pyproject_toml(manifest.path)
            for pdep in parse_res.dependencies:
                rline = pdep.to_requirement_line()
                dep = self.analyze_requirement_line(
                    rline,
                    source_manifest=manifest.relative_path,
                    enable_ai_risk=enable_ai_risk,
                )
                analyzed_deps.append(dep)

        outdated = sum(1 for d in analyzed_deps if d.has_candidate_upgrade and d.is_exact_pin)
        up_to_date = sum(1 for d in analyzed_deps if not d.has_candidate_upgrade and d.current_version)
        preserved = len(analyzed_deps) - outdated - up_to_date

        return ManifestDependencyAnalysis(
            manifest=manifest,
            dependencies=analyzed_deps,
            outdated_count=outdated,
            up_to_date_count=up_to_date,
            preserved_count=preserved,
        )

    def analyze_repository(
        self,
        manifests: List[DiscoveredManifest],
        enable_ai_risk: bool = False,
    ) -> RepositoryDependencyReport:
        """Perform repository-wide dependency analysis, cross-manifest conflict detection, and aggregation."""
        manifest_results: List[ManifestDependencyAnalysis] = []
        specs_by_pkg: Dict[str, List[Tuple[str, str]]] = {}
        warnings: List[str] = []

        for manifest in manifests:
            try:
                m_res = self.analyze_manifest(manifest, enable_ai_risk=enable_ai_risk)
                manifest_results.append(m_res)

                # Collect specifiers for conflict checking
                for dep in m_res.dependencies:
                    norm_k = normalize_package_name(dep.package_name)
                    if norm_k not in specs_by_pkg:
                        specs_by_pkg[norm_k] = []
                    specs_by_pkg[norm_k].append((manifest.relative_path, dep.current_specifier))
            except Exception as err:
                warnings.append(f"Failed to analyze manifest '{manifest.relative_path}': {err}")

        # Tier 3: Detect cross-manifest conflicts
        conflicts: Dict[str, List[Tuple[str, str]]] = {}
        for pkg, occurrences in specs_by_pkg.items():
            if len(occurrences) > 1:
                # Check if distinct specifiers exist for the same package
                unique_specs = {spec for _, spec in occurrences if spec}
                if len(unique_specs) > 1:
                    conflicts[pkg] = occurrences

        total_deps = sum(len(m.dependencies) for m in manifest_results)
        total_outdated = sum(m.outdated_count for m in manifest_results)

        return RepositoryDependencyReport(
            manifest_analyses=manifest_results,
            conflicts=conflicts,
            total_dependencies=total_deps,
            total_outdated=total_outdated,
            total_conflicts=len(conflicts),
            warnings=warnings,
        )

