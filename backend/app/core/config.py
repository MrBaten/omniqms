"""Application settings (frozen contract). All values come from environment variables or .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "dev"  # dev | test | prod
    database_url: str = "sqlite:///./omniqms.db"
    secret_key: str = "dev-insecure-secret-change-me"  # JWT signing; MUST be set in prod
    token_encryption_key: str = ""  # Fernet key (urlsafe base64, 32 bytes); MUST be set in prod
    public_base_url: str = "http://localhost:8000"  # used to build OAuth redirect URIs
    cookie_secure: bool = False  # True in prod (HTTPS)
    session_hours: int = 12
    cors_origins: str = ""  # comma-separated; empty = closed

    # Meta (Facebook Pages + Messenger) — Facebook Login for Business
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_webhook_verify_token: str = ""
    meta_graph_version: str = "v26.0"
    # Facebook Login for Business (Business-type apps): configuration ID that replaces the scope list
    meta_login_config_id: str = ""
    meta_ig_login_config_id: str = ""  # Instagram via Facebook Login (newer apps); enables that connector

    # Instagram API with Instagram Login (Business Login for Instagram)
    ig_app_id: str = ""
    ig_app_secret: str = ""
    ig_webhook_verify_token: str = ""

    # Google / YouTube Data API v3
    google_client_id: str = ""
    google_client_secret: str = ""
    youtube_poll_seconds: int = 120

    # AI
    llm_provider: str = "mock"  # mock | openai | anthropic | openrouter
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    openrouter_api_key: str = ""
    openrouter_model: str = "openai/gpt-4o-mini"
    embeddings: str = "none"  # none | openai
    openai_embedding_model: str = "text-embedding-3-small"

    # Worker
    worker_enabled: bool = True  # run the in-process worker loop on startup
    worker_interval_seconds: float = 1.0

    operator_name: str = ""  # shown on /privacy, /terms, /data-deletion
    contact_email: str = ""

    sandbox_enabled: bool = True  # sandbox platform for demos/tests; disable in prod if unwanted

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
