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

    # ---- ElevenLabs (multilingual TTS — a selectable provider) ----
    # Switchable per account from the dashboard (voice_config.tts_provider =
    # "elevenlabs"). One voice id works across every language because the
    # ElevenLabs models are multilingual, so there is no per-language speaker
    # map the way Sarvam needs. Defaults below are only used when the account
    # picked ElevenLabs but set no explicit voice/model.
    ELEVENLABS_API_KEY: str = "placeholder"
    ELEVENLABS_MODEL: str = "eleven_multilingual_v2"
    ELEVENLABS_VOICE_ID: str = "21m00Tcm4TlvDq8ikWAM"

    # ---- LiveKit (Real-time Voice Agent) ----
    # LIVEKIT_URL: the address the WORKER uses to connect (internal/private on EC2).
    # LIVEKIT_PUBLIC_URL: the address handed to BROWSERS in the token response.
    #   On a single EC2 box the worker uses the private IP but the browser needs the
    #   public IP, so these differ. Empty public URL falls back to LIVEKIT_URL.
    LIVEKIT_URL: str = "ws://localhost:7880"
    LIVEKIT_PUBLIC_URL: str = ""
    LIVEKIT_API_KEY: str = "devkey"
    LIVEKIT_API_SECRET: str = "secret"

    # ---- Deepgram (Direct API — used by LiveKit agent) ----
    DEEPGRAM_API_KEY: str = "placeholder"

    # ---- Encryption ----
    ENCRYPTION_KEY: str = "dev-encryption-key-32bytes-long!!"

    # ---- SMTP (the reminder engine's first real channel) ----
    # Empty SMTP_HOST means "not configured": the email adapter then reports
    # itself unavailable and the engine records a skipped delivery instead of
    # pretending to have sent one. Tests never need real credentials.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_FROM_NAME: str = "Reminders"
    SMTP_USE_TLS: bool = True  # STARTTLS on 587; set false only for port 25
    SMTP_TIMEOUT_SECONDS: int = 20

    # ---- Reminder engine ----
    # PUBLIC_BASE_URL is what the "mark it done" link in an email points at.
    # It must be reachable by the recipient, so localhost works only while you
    # are the recipient.
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # How far ahead occurrences are generated. Generating ahead is what makes
    # the "next 7 days" screen a plain query and stops an outage breaking the
    # chain — the nightly top-up is idempotent, so re-running it is free.
    REMINDER_HORIZON_DAYS: int = 60

    # Rows claimed per tick. At 500 due in the same minute this is the knob
    # that decides whether the backlog drains or grows.
    REMINDER_TICK_BATCH: int = 100

    # Sends in flight at once. Sequential sending cannot clear a 500-reminder
    # minute: at 200ms per email that is 100 seconds, which is longer than the
    # tick interval it has to finish inside.
    REMINDER_SEND_CONCURRENCY: int = 10

    # A row claimed but never finished — the process died mid-send — is handed
    # back after this long. Without this, a crash strands reminders in
    # 'claimed' and nothing ever looks at them again.
    REMINDER_CLAIM_TIMEOUT_MINUTES: int = 10

    # Hour (UTC) of the nightly top-up. 18:00 UTC is 23:30 IST — after the
    # evening medicine reminders have gone out, before midnight.
    REMINDER_TOPUP_HOUR_UTC: int = 18

    @property
    def smtp_configured(self) -> bool:
        """Whether the email channel has enough settings to send anything."""
        return bool(self.SMTP_HOST and self.SMTP_FROM)

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
