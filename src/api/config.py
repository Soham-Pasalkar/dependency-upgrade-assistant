"""Centralized Configuration Management for Dependency Upgrade Assistant 2.0."""

from pathlib import Path
from typing import List, Union
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import json


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core Application Settings
    APP_NAME: str = Field(default="Dependency Upgrade Assistant", description="Application display name")
    APP_VERSION: str = Field(default="0.1.0", description="Application semantic version")
    ENVIRONMENT: str = Field(default="development", description="Runtime environment (development, test, production)")
    HOST: str = Field(default="127.0.0.1", description="Host address to bind the API server")
    PORT: int = Field(default=8000, description="Port to listen on")
    DEBUG: bool = Field(default=False, description="Enable debug mode")

    # Persistence Settings
    DATABASE_PATH: str = Field(default="dua_local.db", description="Path to the SQLite database file")

    # Workspace Settings
    WORKSPACE_ROOT: str = Field(
        default=str(Path.home() / ".dua" / "workspaces"),
        description="Root directory for isolated mutable repository workspaces",
    )

    # Security & CORS Settings
    CORS_ORIGINS: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        description="Allowed origins for Cross-Origin Resource Sharing",
    )

    ALLOWED_HOSTS: List[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "::1"],
        description="Host header values the API answers to (DNS-rebinding protection)",
    )

    # Ollama Local AI Settings
    OLLAMA_BASE_URL: str = Field(default="http://127.0.0.1:11434", description="Base URL of the Ollama server")
    OLLAMA_DEFAULT_MODEL: str = Field(default="llama3:latest", description="Default model tag for Ollama")
    OLLAMA_TIMEOUT_SECONDS: float = Field(default=30.0, description="Ollama API query timeout in seconds")
    ALLOW_REMOTE_OLLAMA: bool = Field(default=False, description="Explicit authorization to connect to non-loopback remote Ollama endpoints")

    # Docker Sandbox Settings
    DOCKER_TIMEOUT_SECONDS: int = Field(default=60, description="Container execution timeout in seconds")
    DOCKER_MAX_MEMORY_MB: int = Field(default=512, description="Maximum RAM cap per container")
    DOCKER_MAX_CPUS: float = Field(default=1.0, description="Maximum CPU limit per container")

    @field_validator("CORS_ORIGINS", "ALLOWED_HOSTS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Union[str, List[str]]) -> List[str]:
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                try:
                    return json.loads(value)
                except Exception:
                    pass
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"

    @property
    def resolved_database_path(self) -> Path:
        return Path(self.DATABASE_PATH).resolve()

    @property
    def resolved_workspace_root(self) -> Path:
        p = Path(self.WORKSPACE_ROOT).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p


# Singleton settings instance
settings = Settings()
