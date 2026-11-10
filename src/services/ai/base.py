"""Provider-independent abstract interface and base classes for AI recommendation services."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from src.models.manifest import AnalyzedDependency
from src.models.recommendation import ModelMetadata, PackageUpgradeRecommendation


# ---------------------------------------------------------------------------
# Provider Exceptions
# ---------------------------------------------------------------------------

class AIProviderError(Exception):
    """Base exception for all AI provider errors."""


class AIProviderUnavailableError(AIProviderError):
    """Raised when the AI provider service is offline or unreachable."""


class AIModelNotFoundError(AIProviderError):
    """Raised when the requested model is not found on the provider."""


class AIInferenceTimeoutError(AIProviderError):
    """Raised when inference with the provider times out."""


class AIResponseParseError(AIProviderError):
    """Raised when the provider output cannot be parsed into structured recommendation models."""


# ---------------------------------------------------------------------------
# Provider Interface
# ---------------------------------------------------------------------------

class AIRecommendationProvider(ABC):
    """Abstract provider interface for generating dependency upgrade recommendations.
    
    Implementations must be provider-independent, ensuring that callers can swap
    between local Ollama, mock engines, or deterministic fallbacks without changing
    business logic or API contracts.
    """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique identifier for this provider (e.g., 'ollama', 'deterministic')."""
        ...

    @abstractmethod
    def check_availability(self) -> Tuple[bool, str]:
        """Check if the provider runtime is active and responsive.
        
        Returns:
            Tuple of (is_available, message_or_version).
        """
        ...

    @abstractmethod
    def list_models(self) -> List[ModelMetadata]:
        """Dynamically discover available models supported by this provider.
        
        Must query runtime capabilities dynamically without hardcoding model names.
        """
        ...

    @abstractmethod
    def generate_recommendations(
        self,
        dependencies: List[AnalyzedDependency],
        model: Optional[str] = None,
    ) -> List[PackageUpgradeRecommendation]:
        """Generate structured upgrade recommendations for a batch of analyzed dependencies.
        
        Args:
            dependencies: List of analyzed dependencies enriched with registry metadata from M2.
            model: Optional model identifier to use for inference.
            
        Returns:
            List of structured PackageUpgradeRecommendation items.
            
        Raises:
            AIProviderUnavailableError: If provider is unreachable.
            AIModelNotFoundError: If requested model is not installed.
            AIInferenceTimeoutError: If inference exceeds timeout limit.
            AIResponseParseError: If model response cannot be parsed into schema.
        """
        ...
