"""Centralized Configuration Management for Dependency Upgrade Assistant 2.0."""

from pathlib import Path
from typing import List, Union
from urllib.parse import urlsplit
from pydantic import Field, field_validator, model_validator
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
    # Origins allowed to make state-changing requests (also used for CORS). Explicit list; never "*".
    # Production deployments must set this. Example: CORS_ORIGINS=["https://dua.example.internal"]
    CORS_ORIGINS: List[str] = Field(
        default_factory=list,
        description="Explicitly allowed browser origins (scheme://host[:port])",
    )
    # In development only, loopback frontends on these ports are allowed automatically
    # (http://localhost, http://127.0.0.1 and http://[::1]). Vite falls back to the next free port,
    # so 5173 and 5174 are both expected; 4173 is `vite preview`.
    DEV_FRONTEND_PORTS: List[int] = Field(
        default_factory=lambda: [5173, 5174, 4173, 3000],
        description="Loopback frontend ports trusted automatically when ENVIRONMENT=development",
    )
    ALLOWED_HOSTS: List[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "::1"],
        description="Host header values the API answers to (DNS-rebinding protection)",
    )

    PLAN_RETENTION_HOURS: float = Field(
        default=24.0, gt=0, le=24 * 30,
        description="How long a built upgrade plan (and its approval) stays usable, across restarts",
    )

    EXPORT_RETENTION_HOURS: float = Field(
        default=24.0, gt=0, le=24 * 365,
        description="Exports older than this are deleted unless their plan is still valid",
    )
    RUN_RETENTION_HOURS: float = Field(
        default=24.0 * 7, gt=0, le=24 * 365,
        description="Analysis runs older than this are deleted once none of their plans is still valid",
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

    @field_validator("CORS_ORIGINS", "ALLOWED_HOSTS", "DEV_FRONTEND_PORTS", mode="before")
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

    @field_validator("CORS_ORIGINS", mode="after")
    @classmethod
    def validate_origins(cls, value: List[str]) -> List[str]:
        return [normalize_origin(origin) for origin in value]

    @field_validator("DEV_FRONTEND_PORTS", mode="after")
    @classmethod
    def validate_ports(cls, value: List[int]) -> List[int]:
        for port in value:
            if not 1 <= int(port) <= 65535:
                raise ValueError(f"DEV_FRONTEND_PORTS contains an invalid port: {port}")
        return [int(p) for p in value]

    @model_validator(mode="after")
    def validate_environment(self) -> "Settings":
        env = self.ENVIRONMENT.strip().lower()
        if env not in {"development", "test", "production"}:
            raise ValueError("ENVIRONMENT must be one of: development, test, production")
        if env == "production" and not self.CORS_ORIGINS:
            raise ValueError("CORS_ORIGINS must list the frontend origin(s) when ENVIRONMENT=production")
        return self

    @property
    def allowed_origins(self) -> List[str]:
        """Single source of truth for origin checks and CORS."""
        origins = list(self.CORS_ORIGINS)
        if self.is_development:
            for port in self.DEV_FRONTEND_PORTS:
                for host in ("localhost", "127.0.0.1", "[::1]"):
                    origin = f"http://{host}:{port}"
                    if origin not in origins:
                        origins.append(origin)
        return origins

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


def normalize_origin(origin: str) -> str:
    """Validate one origin (scheme://host[:port]) and return its canonical form."""
    raw = str(origin).strip()
    if raw in ("*", "null") or "*" in raw:
        raise ValueError(f"Wildcard or null origins are not allowed: {raw!r}")
    parts = urlsplit(raw.rstrip("/"))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Origin must be http(s)://host[:port]: {raw!r}")
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError(f"Origin must not contain a path, query, fragment or credentials: {raw!r}")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


# Singleton settings instance
settings = Settings()
