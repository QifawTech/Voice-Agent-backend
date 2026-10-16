"""
Voice Agent Platform — Application Configuration

Loads all settings from environment variables / .env file using pydantic-settings.
Validates required fields at startup and provides typed access throughout the app.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    ## test 
    # ---- Application ----
    APP_NAME: str = "Voice Agent Platform"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "development"

    # ---- Server ----
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ---- PostgreSQL ----
    DATABASE_URL: str

    # ---- Redis ----
    REDIS_URL: str = "redis://localhost:6379/0"

    # ---- Security ----
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 1440  # 24 hours

    # ---- CORS ----
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # ---- DeepSeek AI ----
    DEEPSEEK_API_KEY: str = "placeholder"
    DEEPSEEK_MODEL: str = "deepseek-chat"
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

    # ---- Deepgram via OpenRouter (English STT + TTS) ----
    OPENROUTER_API_KEY: str = "placeholder"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    DEEPGRAM_STT_MODEL: str = "deepgram/nova-3"
    DEEPGRAM_TTS_MODEL: str = "deepgram/flux-tts:free"
    DEEPGRAM_TTS_VOICE: str = "flux-meena-en"

    # ---- Sarvam AI (South Indian Languages STT/TTS) ----
    SARVAM_API_KEY: str = "placeholder"
    SARVAM_STT_MODEL: str = "saaras:v4"
    SARVAM_TTS_SPEAKER: str = "shreya"

    # ---- LiveKit (Real-time Voice Agent) ----
    LIVEKIT_URL: str = "ws://localhost:7880"
    LIVEKIT_API_KEY: str = "devkey"
    LIVEKIT_API_SECRET: str = "secret"

    # ---- Deepgram (Direct API — used by LiveKit agent) ----
    DEEPGRAM_API_KEY: str = "placeholder"

    # ---- Encryption ----
    ENCRYPTION_KEY: str = "dev-encryption-key-32bytes-long!!"

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse comma-separated CORS origins into a list."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]

    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.ENVIRONMENT == "development"

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    """
    Get cached application settings.

    Uses lru_cache so the .env file is only read once,
    and the same Settings instance is reused across the app.
    """
    return Settings()
