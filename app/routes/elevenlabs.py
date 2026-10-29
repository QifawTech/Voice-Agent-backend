"""
Voice Agent Platform — ElevenLabs Routes

Thin, read-only proxy the dashboard uses to populate the ElevenLabs picker in
the Voice Config tab. The browser never sees the ElevenLabs key — these
endpoints add it server-side:

    GET /api/v1/elevenlabs/models          — TTS-capable models (for the dropdown)
    GET /api/v1/elevenlabs/voices          — the account's voices (for the dropdown)
    GET /api/v1/elevenlabs/voices/{id}     — look up ONE voice by id (the search box)

All require admin JWT, like the rest of the dashboard API. They are not on the
hot call path — the LiveKit agent talks to ElevenLabs directly — so a plain
per-request httpx client is fine here.
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.config import get_settings
from app.logger import get_logger
from app.middleware.auth import get_current_user
from app.models.admin_user import AdminUser

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/elevenlabs", tags=["ElevenLabs"])

ELEVEN_BASE = "https://api.elevenlabs.io/v1"

# Fallback model list, used only if GET /v1/models fails (e.g. a key without the
# models scope). Matches the current ElevenLabs text-to-speech models.
FALLBACK_MODELS = [
    {"model_id": "eleven_multilingual_v2", "name": "Multilingual v2 — best quality"},
    {"model_id": "eleven_turbo_v2_5", "name": "Turbo v2.5 — low latency, multilingual"},
    {"model_id": "eleven_flash_v2_5", "name": "Flash v2.5 — fastest, 32 langs"},
    {"model_id": "eleven_turbo_v2", "name": "Turbo v2 — low latency, English"},
    {"model_id": "eleven_flash_v2", "name": "Flash v2 — fastest, English"},
    {"model_id": "eleven_multilingual_v1", "name": "Multilingual v1 (legacy)"},
    {"model_id": "eleven_monolingual_v1", "name": "Monolingual v1 (legacy, English)"},
]


def _require_key() -> str:
    key = (get_settings().ELEVENLABS_API_KEY or "").strip()
    if not key or key == "placeholder":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ElevenLabs is not configured. Set ELEVENLABS_API_KEY in the backend .env.",
        )
    return key


@router.get(
    "/models",
    summary="List ElevenLabs TTS models",
    description="TTS-capable ElevenLabs models for the dashboard dropdown (static fallback on failure).",
)
async def list_models(current_user: AdminUser = Depends(get_current_user)):
    key = _require_key()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{ELEVEN_BASE}/models", headers={"xi-api-key": key})
    except httpx.HTTPError as e:
        logger.warning("elevenlabs_models_fetch_failed", error=str(e))
        return {"models": FALLBACK_MODELS, "source": "fallback"}

    if r.status_code != 200:
        logger.warning("elevenlabs_models_http_error", status=r.status_code)
        return {"models": FALLBACK_MODELS, "source": "fallback"}

    tts_models = [
        {"model_id": m.get("model_id"), "name": m.get("name") or m.get("model_id")}
        for m in r.json()
        if m.get("can_do_text_to_speech")
    ]
    return {"models": tts_models or FALLBACK_MODELS, "source": "live" if tts_models else "fallback"}


@router.get(
    "/voices",
    summary="List ElevenLabs voices",
    description="The account's voices (id, name, category) for the dashboard dropdown.",
)
async def list_voices(current_user: AdminUser = Depends(get_current_user)):
    key = _require_key()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{ELEVEN_BASE}/voices", headers={"xi-api-key": key})
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach ElevenLabs: {e}",
        )

    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text[:300])

    voices = [
        {
            "voice_id": v.get("voice_id"),
            "name": v.get("name"),
            "category": v.get("category"),
        }
        for v in r.json().get("voices", [])
    ]
    return {"voices": voices}


@router.get(
    "/voices/{voice_id}",
    summary="Look up one ElevenLabs voice by id",
    description="Resolve a single voice id to its name/category — powers the dashboard's 'search by voice ID' box.",
)
async def get_voice(voice_id: str, current_user: AdminUser = Depends(get_current_user)):
    key = _require_key()
    voice_id = voice_id.strip()
    if not voice_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Voice id is required.")

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{ELEVEN_BASE}/voices/{voice_id}", headers={"xi-api-key": key}
            )
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach ElevenLabs: {e}",
        )

    if r.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No ElevenLabs voice with id '{voice_id}'.",
        )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text[:300])

    data = r.json()
    return {
        "voice_id": data.get("voice_id"),
        "name": data.get("name"),
        "category": data.get("category"),
    }
