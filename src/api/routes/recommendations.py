"""FastAPI endpoints for AI provider health, dynamic model discovery, and upgrade recommendations."""
from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.concurrency import run_in_threadpool

from src.models.recommendation import (
    AIProviderStatusResponse,
    RecommendationBatchRequest,
    RecommendationBatchResponse,
)
from src.services.recommendation_service import RecommendationService

logger = logging.getLogger("dua.api.recommendations")

router = APIRouter(tags=["Recommendations"])

# Dependency injection for RecommendationService
_service_instance: Optional[RecommendationService] = None


def get_recommendation_service() -> RecommendationService:
    global _service_instance
    if _service_instance is None:
        _service_instance = RecommendationService()
    return _service_instance


@router.get(
    "/ai/status",
    response_model=AIProviderStatusResponse,
    summary="Check AI Provider Health and Model Discovery",
    description="Probes local Ollama runtime and dynamically discovers installed models.",
)
@router.get(
    "/recommendations/status",
    response_model=AIProviderStatusResponse,
    include_in_schema=False,
)
async def check_ai_status(
    service: Annotated[RecommendationService, Depends(get_recommendation_service)],
) -> AIProviderStatusResponse:
    """Check AI provider status and return dynamically discovered models."""
    return await run_in_threadpool(service.get_ai_status)


@router.post(
    "/recommendations/generate",
    response_model=RecommendationBatchResponse,
    summary="Generate Upgrade Recommendations",
    description=(
        "Analyzes dependency upgrade options using local Ollama LLM inference (with dynamic model selection) "
        "or automatic deterministic SemVer fallback if the daemon is offline. All candidate versions "
        "are strictly validated against registry ground truth."
    ),
)
async def generate_recommendations(
    request: RecommendationBatchRequest,
    service: Annotated[RecommendationService, Depends(get_recommendation_service)],
) -> RecommendationBatchResponse:
    """Generate structured upgrade recommendations for analyzed dependencies."""
    try:
        response = await run_in_threadpool(service.generate_recommendations, request)
        return response
    except Exception as e:
        logger.exception(f"Error generating recommendations: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate recommendations: {str(e)}",
        )
