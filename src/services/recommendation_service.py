"""Service coordinator for dependency upgrade recommendations, AI inference, and validation."""
from __future__ import annotations

import logging
import urllib.parse
from typing import Optional

from src.api.config import settings
from src.models.recommendation import (
    AIProviderStatusResponse,
    ModelMetadata,
    RecommendationBatchRequest,
    RecommendationBatchResponse,
    RecommendationCategory,
    ValidationStatus,
)
from src.services.ai.base import AIProviderError, AIRecommendationProvider
from src.services.ai.deterministic_engine import DeterministicRecommendationEngine
from src.services.ai.ollama_provider import OllamaProvider
from src.services.ai.validator import RegistryGroundTruthValidator

logger = logging.getLogger("dua.services.recommendation")


class RecommendationService:
    """Coordinates upgrade recommendation generation, dynamic model discovery,
    registry ground-truth validation, and deterministic fallbacks.
    """

    def __init__(
        self,
        ai_provider: Optional[AIRecommendationProvider] = None,
        deterministic_engine: Optional[DeterministicRecommendationEngine] = None,
        validator: Optional[RegistryGroundTruthValidator] = None,
    ) -> None:
        self.ai_provider = ai_provider or OllamaProvider(
            base_url=settings.OLLAMA_BASE_URL,
            timeout=settings.OLLAMA_TIMEOUT_SECONDS,
            allow_remote=settings.ALLOW_REMOTE_OLLAMA,
        )
        self.deterministic_engine = deterministic_engine or DeterministicRecommendationEngine()
        self.validator = validator or RegistryGroundTruthValidator()

    def get_ai_status(self) -> AIProviderStatusResponse:
        """Check AI provider connectivity and dynamically discover installed models."""
        try:
            is_avail, message = self.ai_provider.check_availability()
            discovered = self.ai_provider.list_models() if is_avail else []
            default_mod = discovered[0].name if discovered else None

            # Sanitize endpoint display for security
            endpoint_display = "http://127.0.0.1:11434"
            if isinstance(self.ai_provider, OllamaProvider) and hasattr(self.ai_provider, "client"):
                raw_endpoint = self.ai_provider.client.base_url
                try:
                    parsed = urllib.parse.urlsplit(raw_endpoint)
                    endpoint_display = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
                except Exception:
                    endpoint_display = "http://127.0.0.1:11434"

            return AIProviderStatusResponse(
                is_available=is_avail,
                provider_name=self.ai_provider.provider_id,
                endpoint=endpoint_display,
                discovered_models=discovered,
                default_model=default_mod,
                message=message,
            )
        except Exception as e:
            logger.warning(f"Error checking AI provider status: {e}")
            return AIProviderStatusResponse(
                is_available=False,
                provider_name="ollama",
                endpoint="http://127.0.0.1:11434",
                discovered_models=[],
                default_model=None,
                message=f"AI service unreachable: {str(e)}",
            )

    def generate_recommendations(
        self,
        request: RecommendationBatchRequest,
    ) -> RecommendationBatchResponse:
        """Generate structured upgrade recommendations with automatic fallback and validation."""
        dependencies = request.manifest_analysis.dependencies
        manifest_type = request.manifest_analysis.manifest_type
        ecosystem = request.manifest_analysis.ecosystem

        provider_used = "deterministic"
        model_used: Optional[str] = None
        is_offline_fallback = False

        raw_recs = []

        # Determine whether to use AI or deterministic engine
        if request.force_deterministic:
            logger.info("Deterministic execution forced by request.")
            raw_recs = self.deterministic_engine.generate_recommendations(dependencies)
            provider_used = "deterministic"
            model_used = "deterministic-semver"
        else:
            # Check if AI provider is available
            is_avail, avail_msg = self.ai_provider.check_availability()
            available_models = self.ai_provider.list_models() if is_avail else []

            if is_avail and available_models:
                try:
                    logger.info(
                        f"Attempting recommendation generation with AI provider '{self.ai_provider.provider_id}' "
                        f"(requested model: {request.preferred_model})."
                    )
                    raw_recs = self.ai_provider.generate_recommendations(
                        dependencies=dependencies,
                        model=request.preferred_model,
                    )
                    provider_used = self.ai_provider.provider_id
                    model_used = request.preferred_model or available_models[0].name
                except AIProviderError as e:
                    logger.warning(
                        f"AI provider failed ({e}). Gracefully falling back to deterministic engine."
                    )
                    raw_recs = self.deterministic_engine.generate_recommendations(dependencies)
                    provider_used = "deterministic"
                    model_used = "deterministic-semver"
                    is_offline_fallback = True
                except Exception as e:
                    logger.error(
                        f"Unexpected error in AI provider ({e}). Falling back to deterministic engine."
                    )
                    raw_recs = self.deterministic_engine.generate_recommendations(dependencies)
                    provider_used = "deterministic"
                    model_used = "deterministic-semver"
                    is_offline_fallback = True
            else:
                logger.info(
                    f"AI provider offline or has no models ({avail_msg}). Using deterministic fallback engine."
                )
                raw_recs = self.deterministic_engine.generate_recommendations(dependencies)
                provider_used = "deterministic"
                model_used = "deterministic-semver"
                is_offline_fallback = True

        # Validate candidate versions against registry ground truth
        validated_recs = self.validator.validate_batch(raw_recs, dependencies)

        # Calculate metrics
        total_upgrades = sum(
            1
            for r in validated_recs
            if r.category in (RecommendationCategory.RECOMMENDED, RecommendationCategory.CAUTION)
            and r.candidate_version
            and r.candidate_version != r.current_version
        )
        total_caution = sum(1 for r in validated_recs if r.category == RecommendationCategory.CAUTION)

        # Build summary
        summary = (
            f"Generated {len(validated_recs)} recommendations ({total_upgrades} upgrades available, "
            f"{total_caution} caution). Engine: {provider_used}"
            + (f" ({model_used})" if model_used else "")
            + (" [Offline Fallback Active]" if is_offline_fallback else "")
            + "."
        )

        return RecommendationBatchResponse(
            manifest_type=manifest_type,
            ecosystem=ecosystem,
            recommendations=validated_recs,
            provider_used=provider_used,
            model_used=model_used,
            is_offline_fallback=is_offline_fallback,
            total_upgrades_recommended=total_upgrades,
            total_caution=total_caution,
            summary=summary,
        )
