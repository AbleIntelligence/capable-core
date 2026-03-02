"""
Configuration management for CapAble core multi-agent system.

Uses Pydantic Settings for validation and environment variable loading.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GitHubConfig(BaseSettings):
    """GitHub API configuration."""

    model_config = SettingsConfigDict(env_prefix="GITHUB_")

    token: str = Field(..., description="GitHub Personal Access Token")
    default_repo: str | None = Field(None, description="Default repo (owner/repo)")
    api_url: str = Field("https://api.github.com", description="GitHub API base URL")


class GoogleAIConfig(BaseSettings):
    """
    Google AI / Vertex AI configuration.

    For Vertex AI (recommended for gemini-3.0-pro):
        - Set GOOGLE_CLOUD_PROJECT to your GCP project
        - Set GOOGLE_CLOUD_LOCATION (default: us-central1)
        - Set GOOGLE_GENAI_USE_VERTEXAI=true
        - Run: gcloud auth application-default login

    For Gemini API (simpler, but older models):
        - Set GOOGLE_API_KEY
    """

    model_config = SettingsConfigDict(env_prefix="GOOGLE_")

    # Vertex AI settings (preferred for newer models)
    # Note: ADK uses GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION
    cloud_project: str | None = Field(None, alias="GOOGLE_CLOUD_PROJECT", description="GCP Project ID for Vertex AI")
    cloud_location: str = Field("us-central1", alias="GOOGLE_CLOUD_LOCATION", description="Vertex AI region")
    genai_use_vertexai: bool = Field(False, alias="GOOGLE_GENAI_USE_VERTEXAI", description="Use Vertex AI backend")

    # Gemini API fallback
    api_key: str | None = Field(None, description="Google AI API Key (fallback)")

    @property
    def use_vertex_ai(self) -> bool:
        """Returns True if Vertex AI should be used."""
        return self.genai_use_vertexai or self.cloud_project is not None


class AgentConfig(BaseSettings):
    """Agent behavior configuration."""

    model_config = SettingsConfigDict(env_prefix="AGENT_")

    # Global model defaults (used as fallback when per-role settings are unset)
    default_model: str = Field("gemini-3-pro-preview", description="Default LLM model")
    fast_model: str = Field("gemini-2.0-flash", description="Fast model for simple tasks")
    model_name: str = Field(
        "gemini-3-pro-preview",
        description="Global fallback LLM model identifier",
    )
    provider_type: str = Field(
        "gemini",
        description="Global fallback provider: 'gemini', 'claude', 'litellm', or 'hf-local'",
    )
    thinking_budget: int = Field(
        10000,
        description="Token budget for Claude extended thinking (ignored for other providers)",
    )

    # Per-role model overrides (empty string → fall back to global model_name / provider_type)
    developer_model: str = Field(
        "",
        description="LLM model for the Developer agent (empty = use model_name)",
    )
    developer_provider: str = Field(
        "",
        description="Provider for the Developer agent (empty = use provider_type)",
    )
    qa_model: str = Field(
        "",
        description="LLM model for the QA Architect agent (empty = use model_name)",
    )
    qa_provider: str = Field(
        "",
        description="Provider for the QA Architect agent (empty = use provider_type)",
    )

    # Loop limits
    dev_max_iterations: int = Field(3, description="Max dev retry loops")
    qa_max_iterations: int = Field(2, description="Max QA verification loops")
    ci_poll_interval: int = Field(30, description="CI poll interval in seconds")
    ci_timeout: int = Field(600, description="CI timeout in seconds")

    # Quality gates
    min_coverage_percent: float = Field(80.0, description="Minimum test coverage")
    max_surviving_mutants: int = Field(5, description="Max surviving mutation tests")


class SandboxConfig(BaseSettings):
    """Docker sandbox configuration."""

    model_config = SettingsConfigDict(env_prefix="SANDBOX_")

    default_image: str = Field("python:3.11-slim", description="Default Docker image")
    timeout: int = Field(300, description="Container timeout in seconds")
    memory_limit: str = Field("512m", description="Container memory limit")
    cpu_limit: float = Field(1.0, description="Container CPU limit")


class Settings(BaseSettings):
    """Root settings aggregating all configs."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Sub-configs (loaded on access)
    @property
    def github(self) -> GitHubConfig:
        """Return GitHub configuration loaded from environment."""
        return GitHubConfig()

    @property
    def google_ai(self) -> GoogleAIConfig:
        """Return Google AI configuration loaded from environment."""
        return GoogleAIConfig()

    @property
    def agent(self) -> AgentConfig:
        """Return agent configuration loaded from environment."""
        return AgentConfig()

    @property
    def sandbox(self) -> SandboxConfig:
        """Return sandbox configuration loaded from environment."""
        return SandboxConfig()


# Global settings instance
settings = Settings()


def validate_environment() -> list[str]:
    """
    Validates required environment variables.

    Returns list of missing/invalid configs.
    """
    errors = []

    try:
        _ = settings.github.token
    except Exception:
        errors.append("GITHUB_TOKEN is required")

    if settings.agent.provider_type == "gemini" and not settings.google_ai.api_key and not settings.google_ai.cloud_project:
        errors.append("Either GOOGLE_API_KEY or GOOGLE_CLOUD_PROJECT is required for Gemini provider")

    return errors
