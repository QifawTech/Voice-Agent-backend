"""
Voice Agent Platform — LiveKit Routes

API endpoint to generate LiveKit room tokens for the frontend widget.

    POST /api/v1/livekit/token — Generate a room token for a participant

The frontend calls this endpoint with tenant credentials and receives a JWT
token that allows it to connect directly to the LiveKit room via WebRTC.

The LiveKit agent worker (running as a separate process) is automatically
dispatched to the room by the LiveKit server when a participant joins.
"""

import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.config import get_settings
from app.logger import get_logger
from app.middleware.tenant_context import TenantContext, get_tenant_context
from app.middleware.tenant_resolver import get_current_tenant
from app.models.tenant import Tenant
from app.redis_client import get_redis

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/livekit", tags=["LiveKit"])


# ──────────────────────────────────────────────────────────────
# Request / Response Schemas
# ──────────────────────────────────────────────────────────────


class LiveKitTokenRequest(BaseModel):
    """Request body for generating a LiveKit room token."""
    participant_name: str = Field(
        default="guest",
        description="Display name for the participant in the LiveKit room.",
        max_length=100,
    )
    language: str | None = Field(
        default=None,
        description=(
            "Preferred language for the voice conversation (english, telugu, tamil, "
            "kannada, malayalam). When omitted, the tenant's configured default "
            "language (voice_config.language) is used, falling back to english."
        ),
    )
    tts_provider: str | None = Field(
        default=None,
        description=(
            "TTS provider preference: 'deepgram'/'openrouter' for English, or "
            "'elevenlabs' to use ElevenLabs for ANY language. When omitted, the "
            "tenant's configured provider (voice_config.tts_provider) is used, "
            "falling back to 'deepgram'."
        ),
    )
    tts_model: str | None = Field(
        default=None,
        description=(
            "TTS model id. Only used for ElevenLabs (e.g. 'eleven_multilingual_v2', "
            "'eleven_flash_v2_5'). When omitted, the tenant's configured model "
            "(voice_config.elevenlabs_model) is used, else the server default."
        ),
    )
    voice_gender: str | None = Field(
        default=None,
        description=(
            "Agent voice to speak with: 'female' or 'male'. When omitted, the "
            "tenant's configured default (voice_config.voice_gender) is used, "
            "falling back to 'female'. Ignored when an explicit `voice` is set."
        ),
    )
    voice: str | None = Field(
        default=None,
        description=(
            "Explicit TTS voice id for the chosen language — a Sarvam bulbul:v3 "
            "speaker (e.g. 'priya') for Indian languages, or a Deepgram Aura "
            "model (e.g. 'aura-luna-en') for English. When omitted, the tenant's "
            "configured voice for that language (voice_config.voices[language]) "
            "is used, else the voice_gender default."
        ),
    )
    pace: float | None = Field(
        default=None,
        ge=0.5,
        le=2.0,
        description=(
            "Speaking pace (speed): 1.0 is normal, below 1.0 slower, above 1.0 "
            "faster. Range 0.5–2.0. When omitted, the tenant's configured pace "
            "(voice_config.pace) is used, else 1.0."
        ),
    )
    restaurant_id: str | None = Field(
        default=None,
        description=(
            "Sub-tenant slug for multi-tenant accounts. Legacy name — "
            "sub_tenant_id is preferred and wins when both are sent."
        ),
    )
    sub_tenant_id: str | None = Field(
        default=None,
        description="Sub-tenant slug identifying which tenant this call is for.",
    )
    room: str | None = Field(
        default=None,
        description="Specific room name to join. If not provided, a unique room is created.",
    )


class LiveKitTokenResponse(BaseModel):
    """Response containing the LiveKit room token."""
    token: str = Field(description="JWT token for connecting to the LiveKit room.")
    room: str = Field(description="The LiveKit room name.")
    livekit_url: str = Field(description="The LiveKit server WebSocket URL for the frontend to connect to.")
    participant_identity: str = Field(description="The assigned participant identity.")


# ──────────────────────────────────────────────────────────────
# Token Generation Endpoint
# ──────────────────────────────────────────────────────────────


@router.post(
    "/token",
    response_model=LiveKitTokenResponse,
    summary="Generate LiveKit Room Token",
    description=(
        "Generate a JWT token that allows the frontend widget to connect to a "
        "LiveKit room for real-time voice conversation. The token includes "
        "participant metadata (tenant, language, etc.) that the LiveKit agent "
        "worker reads to configure STT/TTS and resolve the tenant."
    ),
)
async def generate_livekit_token(
    payload: LiveKitTokenRequest,
    context: TenantContext = Depends(get_tenant_context),
    redis: Redis = Depends(get_redis),
):
    """
    Generate a LiveKit room token for the frontend.

    Flow:
        1. Validate tenant credentials (via get_current_tenant dependency)
        2. Generate a unique room name (or use the provided one)
        3. Create a JWT token with:
           - Participant identity (name + random suffix)
           - Room join grant
           - Metadata (tenant_id, api_key, language, restaurant_id, etc.)
        4. Return the token + room name + LiveKit URL
    """
    settings = get_settings()
    handler_t0 = time.perf_counter()

    # Resolve and VALIDATE the sub-tenant before issuing a token. The header
    # (handled in get_tenant_context) wins; otherwise fall back to the body.
    # This raises 401 for an unknown slug, a slug belonging to another account,
    # or a missing slug on a multi-tenant account.
    subtenant_t0 = time.perf_counter()
    await context.attach_sub_tenant(payload.sub_tenant_id or payload.restaurant_id)
    subtenant_ms = (time.perf_counter() - subtenant_t0) * 1000

    tenant = context.tenant
    config = context.display_config

    # Resolve the language to speak. An explicit value from the caller (e.g. the
    # widget's data-language or the in-call switcher) wins; otherwise fall back
    # to the tenant's configured default (set in the dashboard Voice Config tab),
    # and finally to english.
    tenant_voice_config = config.get("voice_config") or {}
    tenant_default_language = tenant_voice_config.get("language")
    effective_language = (payload.language or tenant_default_language or "english").lower()

    # Same precedence for the voice: explicit request wins, else the tenant's
    # configured default, else female.
    tenant_default_voice = tenant_voice_config.get("voice_gender")
    effective_voice_gender = (
        "male"
        if (payload.voice_gender or tenant_default_voice or "female").lower() == "male"
        else "female"
    )

    # TTS provider: explicit request wins, else the tenant's configured provider
    # (set in the dashboard Voice Config tab), else deepgram. "elevenlabs" makes
    # the agent use ElevenLabs for EVERY language, bypassing Sarvam/Deepgram.
    effective_tts_provider = (
        payload.tts_provider or tenant_voice_config.get("tts_provider") or "deepgram"
    ).lower()

    # Explicit named voice for this language: caller's choice wins, else the
    # tenant's configured per-language voice (voice_config.voices[language]).
    tenant_voices = tenant_voice_config.get("voices") or {}
    effective_voice = payload.voice or tenant_voices.get(effective_language)

    # ElevenLabs is multilingual, so its voice/model are stored once (not per
    # language): voice_config.elevenlabs_voice_id / elevenlabs_model. When the
    # provider is ElevenLabs these win so one picked voice serves all languages.
    effective_tts_model: str | None = None
    if effective_tts_provider == "elevenlabs":
        effective_voice = (
            payload.voice
            or tenant_voice_config.get("elevenlabs_voice_id")
            or effective_voice
        )
        effective_tts_model = (
            payload.tts_model or tenant_voice_config.get("elevenlabs_model") or None
        )

    # Speaking pace: explicit request wins, else the tenant's configured pace,
    # else 1.0 (normal). Clamped to the range bulbul:v3 accepts.
    try:
        raw_pace = payload.pace if payload.pace is not None else tenant_voice_config.get("pace", 1.0)
        effective_pace = max(0.5, min(2.0, float(raw_pace)))
    except (TypeError, ValueError):
        effective_pace = 1.0

    # Generate room name
    room_name = payload.room or f"voice-{tenant.id}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    # Generate participant identity
    participant_identity = f"{payload.participant_name}-{uuid.uuid4().hex[:6]}"

    # Build participant metadata — the LiveKit agent reads this
    # to configure STT/TTS, resolve tenant, and set up tools
    metadata = json.dumps({
        "tenant_id": str(tenant.id),
        "api_key": _get_tenant_widget_api_key(tenant),
        "tenant_name": tenant.name,
        # Resolved and validated — never the raw value from the widget.
        "restaurant_id": context.restaurant_id,
        "sub_tenant_id": str(context.sub_tenant_id) if context.sub_tenant_id else None,
        # The display config the agent should speak with, already merged by
        # resolve_display_config. Credentials are deliberately excluded — this
        # travels inside the participant token.
        "display_name": config.get("name"),
        "voice_config": config.get("voice_config") or {},
        "upi_id": config.get("upi_id"),
        "language": effective_language,
        "ttsProvider": effective_tts_provider,
        "ttsModel": effective_tts_model,
        "voiceGender": effective_voice_gender,
        "voice": effective_voice,
        "pace": effective_pace,
    })

    try:
        from livekit import api

        token = (
            api.AccessToken(
                api_key=settings.LIVEKIT_API_KEY,
                api_secret=settings.LIVEKIT_API_SECRET,
            )
            .with_identity(participant_identity)
            .with_name(payload.participant_name)
            .with_metadata(metadata)
            .with_grants(api.VideoGrants(
                room_join=True,
                room=room_name,
            ))
            .to_jwt()
        )

        jwt_token = token

    except ImportError:
        logger.error("livekit-api package not installed — cannot generate tokens")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LiveKit token generation is not available. Install livekit-api.",
        )
    except Exception as e:
        logger.error(
            "livekit_token_generation_error",
            tenant_id=str(tenant.id),
            error_type=type(e).__name__,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate LiveKit token.",
        )

    # Determine the WebSocket URL for the frontend (the BROWSER).
    # The browser can't reach the worker's internal/private LIVEKIT_URL, so prefer
    # LIVEKIT_PUBLIC_URL (the instance's public IP) and fall back to LIVEKIT_URL for dev.
    #   dev:  ws://localhost:7880
    #   prod: ws://<public-ip>:7880  (or wss://<domain> behind TLS)
    livekit_ws_url = settings.LIVEKIT_PUBLIC_URL or settings.LIVEKIT_URL

    logger.info(
        "livekit_token_generated",
        tenant_id=str(tenant.id),
        tenant_name=tenant.name,
        room=room_name,
        participant=participant_identity,
        language=effective_language,
        restaurant_id=context.restaurant_id,
        sub_tenant_id=str(context.sub_tenant_id) if context.sub_tenant_id else None,
    )

    # The customer's very first wait: the widget blocks on this POST before it
    # can even join the room. attach_sub_tenant hits the DB/redis, so it is
    # called out separately from the total.
    logger.info(
        "LATENCY token_mint total=%.0fms (subtenant_resolve=%.0fms)",
        (time.perf_counter() - handler_t0) * 1000,
        subtenant_ms,
    )

    return LiveKitTokenResponse(
        token=jwt_token,
        room=room_name,
        livekit_url=livekit_ws_url,
        participant_identity=participant_identity,
    )


# ──────────────────────────────────────────────────────────────
# Health Check
# ──────────────────────────────────────────────────────────────


@router.get(
    "/health",
    summary="LiveKit Health Check",
    description="Check if the LiveKit configuration is valid and the agent can generate tokens.",
)
async def livekit_health():
    """Quick check that LiveKit settings are configured."""
    settings = get_settings()

    has_url = bool(settings.LIVEKIT_URL and settings.LIVEKIT_URL != "ws://localhost:7880")
    has_keys = bool(
        settings.LIVEKIT_API_KEY != "devkey"
        and settings.LIVEKIT_API_SECRET != "secret"
    )

    return {
        "status": "ok" if has_url and has_keys else "development",
        "livekit_url": settings.LIVEKIT_URL,
        "api_key_configured": settings.LIVEKIT_API_KEY != "devkey",
        "deepgram_configured": settings.DEEPGRAM_API_KEY != "placeholder",
    }


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────


def _get_tenant_widget_api_key(tenant: Tenant) -> str:
    """
    Extract the widget API key from the tenant model.

    The widget API key is used by the LiveKit agent to verify the tenant
    when it resolves the participant metadata.
    """
    if hasattr(tenant, "api_key"):
        return str(tenant.api_key)
    if hasattr(tenant, "widget_api_key"):
        return str(tenant.widget_api_key)
    return ""
