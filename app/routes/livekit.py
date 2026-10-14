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
    language: str = Field(
        default="english",
        description="Preferred language for the voice conversation (english, telugu, tamil, kannada, malayalam).",
    )
    tts_provider: str = Field(
        default="deepgram",
        description="TTS provider preference for English (deepgram or openrouter).",
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

    # Resolve and VALIDATE the sub-tenant before issuing a token. The header
    # (handled in get_tenant_context) wins; otherwise fall back to the body.
    # This raises 401 for an unknown slug, a slug belonging to another account,
    # or a missing slug on a multi-tenant account.
    await context.attach_sub_tenant(payload.sub_tenant_id or payload.restaurant_id)

    tenant = context.tenant
    config = context.display_config

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
        "language": payload.language,
        "ttsProvider": payload.tts_provider,
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

    # Determine the WebSocket URL for the frontend
    # In development: ws://localhost:7880
    # In production: wss://your-livekit-server.example.com
    livekit_ws_url = settings.LIVEKIT_URL

    logger.info(
        "livekit_token_generated",
        tenant_id=str(tenant.id),
        tenant_name=tenant.name,
        room=room_name,
        participant=participant_identity,
        language=payload.language,
        restaurant_id=context.restaurant_id,
        sub_tenant_id=str(context.sub_tenant_id) if context.sub_tenant_id else None,
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
