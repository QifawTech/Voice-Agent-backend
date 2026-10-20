"""
Voice Agent Platform — Voice Schemas

Pydantic models for voice conversation API requests and responses.

Supports two modes:
    1. Text mode  — Widget sends text, receives text
    2. Audio mode — Widget sends audio bytes, receives audio bytes + text
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# ── Supported Languages ─────────────────────────────────────

SUPPORTED_LANGUAGES = ["en", "ta", "te", "kn", "ml"]


# ── Request Schemas ──────────────────────────────────────────


class VoiceMessageRequest(BaseModel):
    """Incoming voice message from the widget (text mode)."""

    text: str = Field(
        ..., min_length=1, max_length=2000, examples=["What's on the menu?"]
    )
    session_id: str | None = Field(
        default=None,
        description="Existing session ID. If None, a new session is created.",
        examples=["sess_abc123"],
    )
    restaurant_id: str | None = Field(
        default=None,
        max_length=255,
        description="Restaurant identifier within the tenant platform. Required for multi-restaurant tenants like SOPZY.",
        examples=["spice-garden"],
    )


class VoiceAudioRequest(BaseModel):
    """Incoming voice message from the widget (audio mode).

    The widget captures audio from the microphone, base64-encodes it,
    and sends it here. The backend handles STT → AI → TTS.
    """

    audio: str = Field(
        ...,
        description="Base64-encoded audio data from the microphone.",
        examples=["UklGRiQAAABXQVZFZm10IBAAAA..."],
    )
    audio_format: str = Field(
        default="webm",
        description="Audio format of the recorded data.",
        examples=["webm", "wav", "mp3", "ogg"],
    )
    language: str = Field(
        default="en",
        description="Language code selected by the user.",
        examples=["en", "ta", "te", "kn", "ml"],
    )
    session_id: str | None = Field(
        default=None,
        description="Existing session ID. If None, a new session is created.",
        examples=["sess_abc123"],
    )
    restaurant_id: str | None = Field(
        default=None,
        max_length=255,
        description="Restaurant identifier within the tenant platform.",
        examples=["spice-garden"],
    )


# ── Response Schemas ─────────────────────────────────────────


class VoiceAction(BaseModel):
    """An action triggered by the AI response (e.g., order created)."""

    type: str = Field(..., examples=["order_created"])
    data: dict = Field(default_factory=dict, examples=[{"order_id": "ORD-1042", "total": 880}])


class VoiceMessageResponse(BaseModel):
    """AI response to a voice message (text mode)."""

    text: str = Field(..., examples=["We have Butter Chicken ₹350, Paneer Tikka ₹250..."])
    session_id: str = Field(..., examples=["sess_abc123"])
    restaurant_id: str | None = Field(default=None, examples=["spice-garden"])
    actions: list[VoiceAction] = Field(default_factory=list)
    message_count: int = Field(default=0, description="Total messages in this session")


class VoiceAudioResponse(BaseModel):
    """AI response with audio (audio mode).

    Contains both the text response AND the base64-encoded audio
    so the widget can play it directly without doing browser TTS.
    """

    text: str = Field(..., description="AI response text (for display + accessibility)")
    audio: str = Field(..., description="Base64-encoded MP3 audio of the AI response")
    transcribed_text: str = Field(
        ...,
        description="What the user said (STT result, for display in chat)",
        examples=["What's on the menu?"],
    )
    language: str = Field(..., description="Language used for this exchange", examples=["ta"])
    session_id: str = Field(..., examples=["sess_abc123"])
    restaurant_id: str | None = Field(default=None, examples=["spice-garden"])
    actions: list[VoiceAction] = Field(default_factory=list)
    message_count: int = Field(default=0, description="Total messages in this session")


class SessionResponse(BaseModel):
    """Voice conversation session state."""

    session_id: str
    tenant_id: UUID
    restaurant_id: str | None = None
    state: str = Field(
        ..., examples=["browsing"]
    )  # browsing | ordering | confirming | completed
    message_count: int = 0
    started_at: datetime
    last_active_at: datetime


class SupportedLanguagesResponse(BaseModel):
    """List of supported languages for the widget language selector."""

    languages: list[dict] = Field(
        ...,
        examples=[[
            {"code": "en", "name": "English", "provider": "deepgram"},
            {"code": "ta", "name": "Tamil", "provider": "sarvam"},
        ]],
    )
