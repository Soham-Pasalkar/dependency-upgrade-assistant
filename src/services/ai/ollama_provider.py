"""Ollama AI provider implementation for local LLM inference and dynamic model discovery."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from src.models.manifest import AnalyzedDependency, DependencyStatus, Ecosystem
from src.models.recommendation import (
    ModelMetadata,
    PackageUpgradeRecommendation,
    RecommendationCategory,
    RecommendationPriority,
    ValidationStatus,
)
from src.services.ai.base import (
    AIInferenceTimeoutError,
    AIModelNotFoundError,
    AIProviderError,
    AIProviderUnavailableError,
    AIRecommendationProvider,
    AIResponseParseError,
)
from src.services.ollama_client import (
    ChatMessage,
    OllamaClient,
    OllamaConnectionError,
    OllamaModelNotFoundError,
    OllamaParseError,
    OllamaTimeoutError,
)

logger = logging.getLogger("dua.ai.ollama")


class OllamaProvider(AIRecommendationProvider):
    """Provider implementing AIRecommendationProvider using local Ollama runtime.
    
    Adheres strictly to security and privacy guidelines:
    - Never transmits repository secrets, private source code, or ASTs.
    - Prompts include only package names, versions, constraints, and public registry options.
    - Dynamically discovers locally installed models via /api/tags (no hardcoded names).
    """

    def __init__(
        self,
        client: Optional[OllamaClient] = None,
        base_url: Optional[str] = None,
        timeout: float = 30.0,
        allow_remote: Optional[bool] = None,
    ) -> None:
        self.client = client or OllamaClient(base_url=base_url, timeout=timeout, allow_remote=allow_remote)
        self.timeout = timeout

    @property
    def provider_id(self) -> str:
        return "ollama"

    def check_availability(self) -> Tuple[bool, str]:
        """Check if local Ollama daemon is reachable and active."""
        try:
            status = self.client.check_health()
            if status.is_available:
                ver_info = f"Ollama {status.version}" if status.version else "Ollama runtime active"
                return True, ver_info
            return False, status.error_message or "Ollama service is offline or unreachable"
        except Exception as e:
            return False, f"Ollama connectivity error: {str(e)}"

    def list_models(self) -> List[ModelMetadata]:
        """Dynamically discover locally installed Ollama models."""
        try:
            models_info = self.client.list_models()
            result: List[ModelMetadata] = []
            for m in models_info:
                result.append(
                    ModelMetadata(
                        name=m.name,
                        model=m.model,
                        size_bytes=m.size_bytes,
                        modified_at=m.modified_at,
                        digest=m.digest,
                        family=m.family,
                        parameter_size=m.parameter_size,
                        display_label=m.display_label,
                    )
                )
            return result
        except OllamaConnectionError:
            logger.info("Ollama is not running. Returning empty model list.")
            return []
        except Exception as e:
            logger.warning(f"Error querying Ollama models: {e}")
            return []

    def generate_recommendations(
        self,
        dependencies: List[AnalyzedDependency],
        model: Optional[str] = None,
    ) -> List[PackageUpgradeRecommendation]:
        """Generate structured upgrade recommendations via local Ollama inference."""
        # 1. Verify availability
        is_avail, msg = self.check_availability()
        if not is_avail:
            raise AIProviderUnavailableError(f"Cannot generate AI recommendations: {msg}")

        # 2. Resolve target model dynamically
        available_models = self.list_models()
        if not available_models:
            raise AIProviderUnavailableError("No models are installed in the local Ollama runtime.")

        selected_model = model
        if not selected_model:
            # Pick first discovered model
            selected_model = available_models[0].name
        else:
            # Verify selected model exists
            model_names = {m.name for m in available_models} | {m.model for m in available_models if m.model}
            if selected_model not in model_names:
                raise AIModelNotFoundError(
                    f"Model '{selected_model}' is not installed locally in Ollama. "
                    f"Available models: {sorted(m.name for m in available_models)}"
                )

        # 3. Filter dependencies that need recommendations
        # Packages that are up-to-date or preserved do not require LLM inference
        to_query: List[AnalyzedDependency] = []
        passthrough_recs: Dict[str, PackageUpgradeRecommendation] = {}

        for dep in dependencies:
            if dep.status == DependencyStatus.UP_TO_DATE:
                passthrough_recs[dep.name.lower()] = PackageUpgradeRecommendation(
                    package_name=dep.name,
                    ecosystem=dep.ecosystem,
                    current_version=dep.current_version,
                    declared_specifier=dep.declared_specifier,
                    latest_registry_version=dep.latest_version,
                    candidate_version=dep.current_version,
                    category=RecommendationCategory.NO_UPGRADE_NEEDED,
                    priority=RecommendationPriority.LOW,
                    explanation=f"Package '{dep.name}' is up to date.",
                    confidence_score=1.0,
                    provider_used=self.provider_id,
                    model_used=selected_model,
                    is_ai_generated=False,
                    validation_status=ValidationStatus.SKIPPED,
                    raw_line=dep.raw_line,
                    line_number=dep.line_number,
                )
            elif dep.status == DependencyStatus.PRESERVED:
                passthrough_recs[dep.name.lower()] = PackageUpgradeRecommendation(
                    package_name=dep.name,
                    ecosystem=dep.ecosystem,
                    current_version=dep.current_version,
                    declared_specifier=dep.declared_specifier,
                    latest_registry_version=dep.latest_version,
                    candidate_version=None,
                    category=RecommendationCategory.PRESERVED_UNSUPPORTED,
                    priority=RecommendationPriority.LOW,
                    explanation=dep.status_reason or dep.diagnostic_message or f"Dependency '{dep.name}' preserved.",
                    confidence_score=1.0,
                    provider_used=self.provider_id,
                    model_used=selected_model,
                    is_ai_generated=False,
                    validation_status=ValidationStatus.SKIPPED,
                    raw_line=dep.raw_line,
                    line_number=dep.line_number,
                )
            else:
                to_query.append(dep)

        if not to_query:
            # All packages are up to date or preserved
            return [passthrough_recs.get(d.name.lower()) for d in dependencies if d.name.lower() in passthrough_recs]

        # 4. Construct prompt and execute inference
        prompt = self._build_prompt(to_query)
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "You are an expert dependency upgrade assistant. Analyze the given package dependencies "
                    "and select the best candidate upgrade version from the provided registry versions. "
                    "Assess risk, potential breaking changes, and priority. Return ONLY a valid JSON object "
                    "with a key 'recommendations' containing an array of recommendation objects."
                ),
            ),
            ChatMessage(role="user", content=prompt),
        ]

        try:
            parsed_json, _ = self.client.chat_json(
                model=selected_model,
                messages=messages,
                timeout=self.timeout,
            )
        except OllamaConnectionError as e:
            raise AIProviderUnavailableError(str(e)) from e
        except OllamaModelNotFoundError as e:
            raise AIModelNotFoundError(str(e)) from e
        except OllamaTimeoutError as e:
            raise AIInferenceTimeoutError(str(e)) from e
        except OllamaParseError as e:
            raise AIResponseParseError(str(e)) from e
        except Exception as e:
            raise AIProviderError(f"Ollama inference error: {e}") from e

        # 5. Extract and validate recommendations from JSON response
        ai_recs = self._parse_model_output(parsed_json, to_query, selected_model)

        # 6. Recombine in original dependency order
        final_list: List[PackageUpgradeRecommendation] = []
        for dep in dependencies:
            dep_key = dep.name.lower()
            if dep_key in passthrough_recs:
                final_list.append(passthrough_recs[dep_key])
            elif dep_key in ai_recs:
                final_list.append(ai_recs[dep_key])
            else:
                # If LLM omitted a package, create default recommendation
                candidate = dep.latest_compatible_version or dep.latest_version or dep.current_version
                final_list.append(
                    PackageUpgradeRecommendation(
                        package_name=dep.name,
                        ecosystem=dep.ecosystem,
                        current_version=dep.current_version,
                        declared_specifier=dep.declared_specifier,
                        latest_registry_version=dep.latest_version,
                        candidate_version=candidate,
                        category=RecommendationCategory.RECOMMENDED,
                        priority=RecommendationPriority.MEDIUM,
                        explanation=f"Upgrade '{dep.name}' to version {candidate}.",
                        confidence_score=0.8,
                        provider_used=self.provider_id,
                        model_used=selected_model,
                        is_ai_generated=False,
                        validation_status=ValidationStatus.FALLBACK_DETERMINISTIC,
                        raw_line=dep.raw_line,
                        line_number=dep.line_number,
                    )
                )

        return final_list

    def _build_prompt(self, dependencies: List[AnalyzedDependency]) -> str:
        """Build structured prompt containing only public metadata."""
        items = []
        for d in dependencies:
            items.append({
                "package_name": d.name,
                "ecosystem": d.ecosystem.value,
                "current_version": d.current_version,
                "declared_specifier": d.declared_specifier,
                "latest_registry_version": d.latest_version,
                "latest_compatible_version": d.latest_compatible_version,
                "available_versions_sample": d.available_versions[-10:] if d.available_versions else [],
                "is_yanked_or_deprecated": d.yanked_or_deprecated,
            })

        return (
            "Analyze these outdated dependencies and propose an upgrade recommendation for each:\n"
            f"{json.dumps(items, indent=2)}\n\n"
            "Format your response as a JSON object with a single 'recommendations' array. Each item must have:\n"
            "- 'package_name': string\n"
            "- 'candidate_version': string (must be chosen from available_versions_sample or latest_registry_version)\n"
            "- 'category': 'RECOMMENDED' or 'CAUTION'\n"
            "- 'priority': 'CRITICAL', 'HIGH', 'MEDIUM', or 'LOW'\n"
            "- 'explanation': string explaining why this version was chosen\n"
            "- 'breaking_changes_summary': string or null detailing any breaking changes"
        )

    def _parse_model_output(
        self,
        data: Dict[str, Any],
        dependencies: List[AnalyzedDependency],
        model_name: str,
    ) -> Dict[str, PackageUpgradeRecommendation]:
        """Parse structured model output into PackageUpgradeRecommendation items.
        
        Security and Integrity Guards:
        - Unknown package names not in analyzed dependencies are rejected.
        - Duplicate package recommendations are safely ignored (first occurrence preserved).
        - Candidate versions are validated against string length and illegal characters.
        - Explanations and risk summaries are strictly bounded (<= 500 chars).
        - Unknown enum categories/priorities fall back to safe defaults.
        """
        dep_map = {d.name.lower(): d for d in dependencies}
        recs_data = data.get("recommendations", [])
        if isinstance(data, list):
            recs_data = data
        elif not isinstance(recs_data, list):
            recs_data = [recs_data] if isinstance(recs_data, dict) else []

        result: Dict[str, PackageUpgradeRecommendation] = {}
        seen_packages: Set[str] = set()

        for item in recs_data:
            if not isinstance(item, dict):
                continue

            pkg_name = item.get("package_name")
            if not pkg_name or not isinstance(pkg_name, str):
                continue

            dep_key = pkg_name.strip().lower()
            # Ignore unknown packages not in analyzed manifest
            dep = dep_map.get(dep_key)
            if not dep:
                continue

            # Prevent duplicate packages from overwriting earlier entries
            if dep_key in seen_packages:
                continue
            seen_packages.add(dep_key)

            cand = item.get("candidate_version")
            cand_str = str(cand).strip() if cand is not None else None
            # Validate candidate string bounds
            if cand_str:
                if len(cand_str) > 64 or any(c.isspace() or c in ("\x00", "\r", "\n", ";", "&", "|") for c in cand_str):
                    cand_str = None

            # Category parsing with safe fallback
            cat_raw = str(item.get("category", "RECOMMENDED")).upper()
            try:
                cat = RecommendationCategory(cat_raw)
            except ValueError:
                cat = RecommendationCategory.RECOMMENDED

            # Priority parsing with safe fallback
            prio_raw = str(item.get("priority", "MEDIUM")).upper()
            try:
                prio = RecommendationPriority(prio_raw)
            except ValueError:
                prio = RecommendationPriority.MEDIUM

            # Bounded text fields
            explanation = str(item.get("explanation", f"AI recommendation for {dep.name}"))[:500]
            breaking = item.get("breaking_changes_summary")
            breaking_str = str(breaking)[:500] if breaking else None

            result[dep_key] = PackageUpgradeRecommendation(
                package_name=dep.name,
                ecosystem=dep.ecosystem,
                current_version=dep.current_version,
                declared_specifier=dep.declared_specifier,
                latest_registry_version=dep.latest_version,
                candidate_version=cand_str,
                category=cat,
                priority=prio,
                explanation=explanation,
                breaking_changes_summary=breaking_str,
                confidence_score=0.9,
                provider_used=self.provider_id,
                model_used=model_name,
                is_ai_generated=True,
                validation_status=ValidationStatus.VALIDATED,  # Will be validated by GroundTruthValidator
                raw_line=dep.raw_line,
                line_number=dep.line_number,
            )

        return result
