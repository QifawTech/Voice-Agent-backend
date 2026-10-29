"""
Voice Agent Platform — Voice Routes

API endpoints for voice conversations:
    POST /api/v1/voice/session          — Create a new voice session
    POST /api/v1/voice/message          — Send a text message and get AI response
    POST /api/v1/voice/audio            — Send audio and get AI response + audio back
    GET  /api/v1/voice/session/{id}     — Get session state
    GET  /api/v1/voice/languages        — Get supported languages
    WS   /ws/voice                      — WebSocket real-time conversation

Audio pipeline:
    🎙️ Customer speaks → Widget captures raw audio → POST /voice/audio
        → Backend STT (Deepgram for EN / Sarvam for TA,TE,KN,ML)
            → Text → ConversationEngine → AI response text
                → Backend TTS (same provider routing)
                    → Audio bytes → Widget plays response 🔊

All widget endpoints require tenant authentication via X-Tenant-ID + X-API-Key headers.
"""

import base64
import json
import time
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.logger import get_logger
from app.middleware.rate_limiter import check_message_rate_limit
from app.middleware.tenant_context import TenantContext, get_tenant_context
from app.middleware.tenant_resolver import get_current_tenant
from app.models.tenant import Tenant
from app.redis_client import get_redis
from app.schemas.voice import (
    SessionResponse,
    SupportedLanguagesResponse,
    VoiceAudioRequest,
    VoiceAudioResponse,
    VoiceMessageRequest,
    VoiceMessageResponse,
)
from app.services.conversation_engine import ConversationEngine, ConversationEngineError
from app.services.session_manager import SessionManager
from app.services.speech_service import SpeechService, SpeechServiceError
from app.services.tenant_service import TenantService

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/voice", tags=["Voice"])


def _tts_opts(tenant) -> dict:
    """TTS provider overrides from a tenant's voice_config, for SpeechService.

    Returns ElevenLabs options only when the tenant switched to it in the
    dashboard (voice_config.tts_provider == "elevenlabs"); otherwise an empty
    dict so synthesize() keeps its language-based Deepgram/Sarvam routing.
    """
    vc = getattr(tenant, "voice_config", None) or {}
    if (vc.get("tts_provider") or "").lower() == "elevenlabs":
        return {
            "provider": "elevenlabs",
            "voice_id": vc.get("elevenlabs_voice_id"),
            "model": vc.get("elevenlabs_model"),
        }
    return {}


# ──────────────────────────────────────────────────────────────
# REST Endpoints
# ──────────────────────────────────────────────────────────────


@router.post(
    "/session",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Voice Session",
    description="Create a new voice conversation session for a tenant.",
)
async def create_session(
    restaurant_id: str | None = Query(
        default=None,
        description="Sub-tenant slug. Required for multi-tenant accounts.",
    ),
    context: TenantContext = Depends(get_tenant_context),
    redis: Redis = Depends(get_redis),
):
    """
    Create a new voice conversation session.

    Returns a session ID that the widget uses for subsequent messages.
    Sessions auto-expire after 30 minutes of inactivity.
    """
    # Validate the sub-tenant before a session exists, so an invalid slug can
    # never be written into conversation logs.
    await context.attach_sub_tenant(restaurant_id)
    tenant = context.tenant
    restaurant_id = context.restaurant_id

    session_manager = SessionManager(redis)
    session_id = await session_manager.create_session(str(tenant.id))

    # Get the created session data
    session = await session_manager.get_session(session_id)

    logger.info(
        "voice_session_created",
        tenant_id=str(tenant.id),
        session_id=session_id,
        restaurant_id=restaurant_id,
    )

    return SessionResponse(
        session_id=session_id,
        tenant_id=tenant.id,
        restaurant_id=restaurant_id,
        state="browsing",
        message_count=0,
        started_at=datetime.fromisoformat(session["created_at"]) if session else datetime.now(timezone.utc),
        last_active_at=datetime.fromisoformat(session["last_active"]) if session else datetime.now(timezone.utc),
    )


@router.post(
    "/message",
    response_model=VoiceMessageResponse,
    summary="Send Text Message",
    description="Send a text message and receive an AI-generated text response.",
)
async def send_message(
    payload: VoiceMessageRequest,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _rate_limit: None = Depends(check_message_rate_limit),
):
    """
    Process a text message through the AI conversation engine.

    This is the text-only endpoint (no audio processing).
    Use POST /voice/audio for the full audio pipeline.
    """
    await context.attach_sub_tenant(payload.restaurant_id)
    tenant = context.tenant

    try:
        engine = ConversationEngine(db=db, redis=redis)
        result = await engine.process_message(
            tenant=tenant,
            message=payload.text,
            session_id=payload.session_id,
            restaurant_id=context.restaurant_id,
        )

        logger.info(
            "voice_message_processed",
            tenant_id=str(tenant.id),
            session_id=result["session_id"],
            message_length=len(payload.text),
            response_length=len(result["text"]),
            action_count=len(result.get("actions", [])),
        )

        return VoiceMessageResponse(
            text=result["text"],
            session_id=result["session_id"],
            restaurant_id=result.get("restaurant_id"),
            actions=[
                {"type": a["type"], "data": a.get("data", {})}
                for a in result.get("actions", [])
            ],
            message_count=result.get("message_count", 0),
        )

    except ConversationEngineError as e:
        logger.error(
            "voice_message_engine_error",
            tenant_id=str(tenant.id),
            session_id=payload.session_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process message. Please try again.",
        )
    except Exception as e:
        logger.error(
            "voice_message_unexpected_error",
            tenant_id=str(tenant.id),
            error_type=type(e).__name__,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        )


# ──────────────────────────────────────────────────────────────
# Audio Endpoint (Full STT → AI → TTS Pipeline)
# ──────────────────────────────────────────────────────────────


@router.post(
    "/audio",
    response_model=VoiceAudioResponse,
    summary="Send Audio Message",
    description=(
        "Send recorded audio and receive AI response as both text and audio. "
        "The backend handles STT (speech-to-text) and TTS (text-to-speech). "
        "English uses Deepgram via OpenRouter. Tamil, Telugu, Kannada, Malayalam use Sarvam AI."
    ),
)
async def send_audio_message(
    payload: VoiceAudioRequest,
    context: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _rate_limit: None = Depends(check_message_rate_limit),
):
    """
    Full audio pipeline:
        1. Decode base64 audio from widget
        2. STT: Convert audio → text (Deepgram for EN / Sarvam for TA,TE,KN,ML)
        3. Process text through ConversationEngine (DeepSeek AI)
        4. TTS: Convert AI response text → audio (same provider routing)
        5. Return text + base64 audio to widget
    """
    # Validate the sub-tenant before spending money on STT.
    await context.attach_sub_tenant(payload.restaurant_id)
    tenant = context.tenant

    speech = SpeechService()

    try:
        # ── Step 1: Decode incoming audio ────────────────────
        try:
            audio_bytes = base64.b64decode(payload.audio)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid base64 audio data",
            )

        if len(audio_bytes) < 100:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Audio data too small — may be empty or corrupt",
            )

        logger.info(
            "voice_audio_received",
            tenant_id=str(tenant.id),
            language=payload.language,
            audio_format=payload.audio_format,
            audio_size=len(audio_bytes),
        )

        # ── Step 2: Speech-to-Text ───────────────────────────
        try:
            transcribed_text = await speech.transcribe(
                audio_data=audio_bytes,
                language=payload.language,
                audio_format=payload.audio_format,
            )
        except SpeechServiceError as e:
            logger.error(
                "voice_audio_stt_failed",
                tenant_id=str(tenant.id),
                provider=e.provider,
                error=str(e),
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Could not understand the audio. Please try speaking again. ({e.provider})",
            )

        logger.info(
            "voice_audio_transcribed",
            tenant_id=str(tenant.id),
            language=payload.language,
            transcribed_text=transcribed_text[:100],
        )

        # ── Step 3: Process through ConversationEngine ───────
        engine = ConversationEngine(db=db, redis=redis)
        result = await engine.process_message(
            tenant=tenant,
            message=transcribed_text,
            session_id=payload.session_id,
            restaurant_id=context.restaurant_id,
        )

        ai_response_text = result["text"]

        # ── Step 4: Text-to-Speech ───────────────────────────
        try:
            response_audio = await speech.synthesize(
                text=ai_response_text,
                language=payload.language,
                **_tts_opts(tenant),
            )
            response_audio_b64 = base64.b64encode(response_audio).decode("utf-8")
        except SpeechServiceError as e:
            logger.error(
                "voice_audio_tts_failed",
                tenant_id=str(tenant.id),
                provider=e.provider,
                error=str(e),
            )
            # TTS failure is non-critical — return text without audio
            response_audio_b64 = ""

        logger.info(
            "voice_audio_complete",
            tenant_id=str(tenant.id),
            session_id=result["session_id"],
            language=payload.language,
            transcribed_length=len(transcribed_text),
            response_length=len(ai_response_text),
            audio_response_size=len(response_audio_b64),
            action_count=len(result.get("actions", [])),
        )

        # ── Step 5: Return response ──────────────────────────
        return VoiceAudioResponse(
            text=ai_response_text,
            audio=response_audio_b64,
            transcribed_text=transcribed_text,
            language=payload.language,
            session_id=result["session_id"],
            restaurant_id=result.get("restaurant_id"),
            actions=[
                {"type": a["type"], "data": a.get("data", {})}
                for a in result.get("actions", [])
            ],
            message_count=result.get("message_count", 0),
        )

    except HTTPException:
        raise
    except ConversationEngineError as e:
        logger.error(
            "voice_audio_engine_error",
            tenant_id=str(tenant.id),
            session_id=payload.session_id,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process message. Please try again.",
        )
    except Exception as e:
        logger.error(
            "voice_audio_unexpected_error",
            tenant_id=str(tenant.id),
            error_type=type(e).__name__,
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        )
    finally:
        await speech.close()


# ──────────────────────────────────────────────────────────────
# Session & Language Endpoints
# ──────────────────────────────────────────────────────────────


@router.get(
    "/session/{session_id}",
    response_model=SessionResponse,
    summary="Get Session State",
    description="Retrieve the current state of a voice conversation session.",
)
async def get_session(
    session_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    redis: Redis = Depends(get_redis),
):
    """
    Get the current state of a voice session.

    Returns session metadata including message count, state, and timestamps.
    Returns 404 if the session has expired or doesn't exist.
    """
    session_manager = SessionManager(redis)
    session = await session_manager.get_session(session_id)

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found or expired",
        )

    # Verify the session belongs to this tenant
    if session.get("tenant_id") != str(tenant.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session does not belong to this tenant",
        )

    history = session.get("history", [])

    # Determine state from session data
    state = session.get("status", "browsing")
    if state == "active":
        state = "browsing"

    return SessionResponse(
        session_id=session_id,
        tenant_id=tenant.id,
        restaurant_id=session.get("restaurant_id"),
        state=state,
        message_count=len(history),
        started_at=datetime.fromisoformat(session["created_at"]),
        last_active_at=datetime.fromisoformat(session["last_active"]),
    )


@router.get(
    "/languages",
    response_model=SupportedLanguagesResponse,
    summary="Supported Languages",
    description="Get the list of supported languages for voice conversations.",
)
async def get_supported_languages():
    """
    Return the list of supported languages.

    The widget uses this to render a language selector dropdown.
    Each language has a code, name, and which STT/TTS provider powers it.
    """
    return SupportedLanguagesResponse(
        languages=SpeechService.get_supported_languages()
    )


# ──────────────────────────────────────────────────────────────
# WebSocket Endpoint
# ──────────────────────────────────────────────────────────────


@router.websocket("/ws")
async def voice_websocket(
    websocket: WebSocket,
    tenant_id: str | None = Query(default=None, alias="tenant_id"),
    api_key: str | None = Query(default=None, alias="api_key"),
    restaurant_id: str | None = Query(default=None, alias="restaurant_id"),
    language: str | None = Query(default="en", alias="language"),
):
    """
    WebSocket endpoint for real-time voice conversations.

    Connection:
        ws://host/api/v1/voice/ws?tenant_id=UUID&api_key=KEY&restaurant_id=SLUG&language=ta

    Authentication:
        Tenant ID and API key are passed as query parameters
        (WebSocket doesn't support custom headers in browsers).

    Message Protocol (JSON):
        Client → Server:
            Text mode:
                {"type": "message", "text": "What's on the menu?"}
            Audio mode:
                {"type": "audio", "audio": "BASE64_DATA", "audio_format": "webm"}
            Control:
                {"type": "set_language", "language": "ta"}
                {"type": "end_session"}

        Server → Client:
            {"type": "connected", "session_id": "...", "language": "...", "languages": [...]}
            {"type": "thinking"}
            {"type": "response", "text": "...", "audio": "BASE64_MP3", "actions": [...]}
            {"type": "error", "message": "..."}
            {"type": "session_ended", "message": "..."}
            {"type": "language_changed", "language": "...", "name": "..."}
    """
    # ── Step 1: Validate tenant credentials ──────────────────
    if not tenant_id or not api_key:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Missing tenant_id or api_key query parameters",
        )
        return

    # We need to resolve tenant manually (can't use Depends in WebSocket easily)
    from app.database import async_session_factory
    from app.redis_client import get_redis_sync

    redis = get_redis_sync()
    speech = SpeechService()

    # Track current language (mutable per session)
    current_language = language if SpeechService.is_supported_language(language or "en") else "en"

    async with async_session_factory() as db:
        try:
            # Resolve tenant
            service = TenantService(db=db, redis=redis)
            tenant = await service.get_tenant_by_api_key(api_key)

            if tenant is None:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="Invalid API key",
                )
                return

            if str(tenant.id) != tenant_id:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="Tenant ID does not match API key",
                )
                return

            if not tenant.is_active:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="Tenant account is deactivated",
                )
                return

        except Exception as e:
            logger.error("websocket_auth_error", error=str(e))
            await websocket.close(
                code=status.WS_1011_INTERNAL_ERROR,
                reason="Authentication failed",
            )
            return

        # ── Step 1b: Validate the sub-tenant ─────────────────────
        # Browsers cannot set custom headers on a WebSocket, so the slug arrives
        # as a query param. It still gets the same ownership check as every
        # other path — an unvalidated slug here would reopen the hole.
        from app.middleware.tenant_context import TenantContext

        ws_context = TenantContext(tenant=tenant, db=db, redis=redis)
        try:
            await ws_context.attach_sub_tenant(restaurant_id)
        except HTTPException as e:
            logger.warning(
                "websocket_sub_tenant_rejected",
                tenant_id=str(tenant.id),
                slug=restaurant_id,
                detail=e.detail,
            )
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=str(e.detail)[:120],
            )
            return

        restaurant_id = ws_context.restaurant_id

        # ── Step 2: Accept connection and create session ─────────
        await websocket.accept()

        session_manager = SessionManager(redis)
        session_id = await session_manager.create_session(str(tenant.id))

        logger.info(
            "websocket_connected",
            tenant_id=str(tenant.id),
            session_id=session_id,
            restaurant_id=restaurant_id,
            language=current_language,
        )

        # Send connected confirmation with supported languages
        await websocket.send_json({
            "type": "connected",
            "session_id": session_id,
            "language": current_language,
            "languages": SpeechService.get_supported_languages(),
            "message": "Connected to voice agent. Send messages to start a conversation.",
        })

        # Generate and send initial greeting
        import base64
        greeting_text = tenant.voice_config.get("greeting") if tenant.voice_config else ""
        if not greeting_text:
            greeting_text = f"Welcome to {tenant.name}. How can I help you today?"
            
        try:
            greeting_audio = await speech.synthesize(
                text=greeting_text,
                language=current_language,
                **_tts_opts(tenant),
            )
            greeting_audio_b64 = base64.b64encode(greeting_audio).decode("utf-8")
        except Exception as e:
            logger.error("greeting_tts_failed", error=str(e))
            greeting_audio_b64 = ""

        # Send initial response with the greeting
        await websocket.send_json({
            "type": "response",
            "text": greeting_text,
            "audio": greeting_audio_b64,
            "language": current_language,
            "session_id": session_id,
            "actions": [],
            "message_count": 0,
        })

        # ── Step 3: Message loop ─────────────────────────────────
        message_count = 0
        start_time = time.time()

        try:
            while True:
                # Receive message from client
                raw_data = await websocket.receive_text()

                try:
                    data = json.loads(raw_data)
                except json.JSONDecodeError:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Invalid JSON format",
                    })
                    continue

                msg_type = data.get("type", "message")

                # ── Handle set_language ──────────────────────────
                if msg_type == "set_language":
                    new_lang = data.get("language", "en")
                    if SpeechService.is_supported_language(new_lang):
                        current_language = new_lang
                        from app.services.speech_service import LANGUAGE_CONFIG
                        lang_name = LANGUAGE_CONFIG[new_lang]["name"]
                        await websocket.send_json({
                            "type": "language_changed",
                            "language": new_lang,
                            "name": lang_name,
                        })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Unsupported language: {new_lang}",
                        })
                    continue

                # ── Handle end_session ───────────────────────────
                if msg_type == "end_session":
                    await session_manager.end_session(session_id, reason="user_ended")

                    await websocket.send_json({
                        "type": "session_ended",
                        "message": "Session ended. Thank you!",
                        "total_messages": message_count,
                        "duration_seconds": int(time.time() - start_time),
                    })

                    logger.info(
                        "websocket_session_ended_by_user",
                        tenant_id=str(tenant.id),
                        session_id=session_id,
                        message_count=message_count,
                    )
                    break

                # ── Handle audio message ─────────────────────────
                if msg_type == "audio":
                    audio_b64 = data.get("audio", "")
                    audio_format = data.get("audio_format", "webm")

                    if not audio_b64:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Empty audio data",
                        })
                        continue

                    # Send "thinking" indicator
                    await websocket.send_json({"type": "thinking"})

                    try:
                        # Decode audio
                        audio_bytes = base64.b64decode(audio_b64)

                        # STT
                        transcribed_text = await speech.transcribe(
                            audio_data=audio_bytes,
                            language=current_language,
                            audio_format=audio_format,
                        )

                        # Process through conversation engine
                        engine = ConversationEngine(db=db, redis=redis)
                        result = await engine.process_message(
                            tenant=tenant,
                            message=transcribed_text,
                            session_id=session_id,
                            restaurant_id=restaurant_id,
                        )

                        # Clean text for TTS
                        tts_text = result["text"]
                        import re
                        tts_text = re.sub(r'[*#_`]', '', tts_text)
                        tts_text = re.sub(r'^\s*-\s+', '', tts_text, flags=re.MULTILINE)
                        
                        # Chunk TTS for lower latency (split by punctuation)
                        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', tts_text) if s.strip()]
                        if not sentences:
                            sentences = [tts_text]

                        # Send the text response immediately so UI updates
                        await websocket.send_json({
                            "type": "response",
                            "text": result["text"],
                            "audio": "",  # audio will stream
                            "transcribed_text": transcribed_text,
                            "language": current_language,
                            "session_id": result["session_id"],
                            "actions": result.get("actions", []),
                            "message_count": result.get("message_count", 0),
                        })

                        message_count += 1

                        # Stream audio chunks
                        for chunk in sentences:
                            try:
                                chunk_audio = await speech.synthesize(
                                    text=chunk,
                                    language=current_language,
                                    **_tts_opts(tenant),
                                )
                                chunk_b64 = base64.b64encode(chunk_audio).decode("utf-8")
                                await websocket.send_json({
                                    "type": "audio_chunk",
                                    "audio": chunk_b64
                                })
                            except Exception as e:
                                logger.error("tts_chunk_error", error=str(e))
                                break

                    except SpeechServiceError as e:
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Could not understand the audio. Please try again. ({e.provider})",
                        })
                    except Exception as e:
                        logger.error("websocket_audio_error", error=str(e))
                        await websocket.send_json({
                            "type": "error",
                            "message": "Failed to process audio.",
                        })
                    continue

                # ── Handle text message ──────────────────────────
                if msg_type == "message":
                    text = data.get("text", "").strip()

                    if not text:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Empty message text",
                        })
                        continue

                    if len(text) > 2000:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Message too long (max 2000 characters)",
                        })
                        continue

                    # Send "thinking" indicator
                    await websocket.send_json({"type": "thinking"})

                    # Process through conversation engine
                    try:
                        engine = ConversationEngine(db=db, redis=redis)
                        result = await engine.process_message(
                            tenant=tenant,
                            message=text,
                            session_id=session_id,
                            restaurant_id=restaurant_id,
                        )

                        # Clean text for TTS
                        tts_text = result["text"]
                        import re
                        tts_text = re.sub(r'[*#_`]', '', tts_text)
                        tts_text = re.sub(r'^\s*-\s+', '', tts_text, flags=re.MULTILINE)

                        # Chunk TTS for lower latency (split by punctuation)
                        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', tts_text) if s.strip()]
                        if not sentences:
                            sentences = [tts_text]

                        # Send the text response immediately
                        await websocket.send_json({
                            "type": "response",
                            "text": result["text"],
                            "audio": "",  # audio will stream
                            "language": current_language,
                            "session_id": result["session_id"],
                            "actions": result.get("actions", []),
                            "message_count": result.get("message_count", 0),
                        })

                        message_count += 1

                        # Stream audio chunks
                        for chunk in sentences:
                            try:
                                chunk_audio = await speech.synthesize(
                                    text=chunk,
                                    language=current_language,
                                    **_tts_opts(tenant),
                                )
                                chunk_b64 = base64.b64encode(chunk_audio).decode("utf-8")
                                await websocket.send_json({
                                    "type": "audio_chunk",
                                    "audio": chunk_b64
                                })
                            except Exception as e:
                                logger.error("tts_chunk_error", error=str(e))
                                break

                        logger.debug(
                            "websocket_message_processed",
                            session_id=session_id,
                            message_num=message_count,
                        )

                    except ConversationEngineError as e:
                        logger.error(
                            "websocket_engine_error",
                            session_id=session_id,
                            error=str(e),
                        )
                        await websocket.send_json({
                            "type": "error",
                            "message": "Failed to process message. Please try again.",
                        })

                    except Exception as e:
                        logger.error(
                            "websocket_unexpected_error",
                            session_id=session_id,
                            error_type=type(e).__name__,
                            error=str(e),
                        )
                        await websocket.send_json({
                            "type": "error",
                            "message": "An unexpected error occurred.",
                        })

                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type}. Use 'message', 'audio', 'set_language', or 'end_session'.",
                    })

        except WebSocketDisconnect:
            # Client disconnected — save session and log analytics
            logger.info(
                "websocket_disconnected",
                tenant_id=str(tenant.id),
                session_id=session_id,
                message_count=message_count,
                duration_seconds=int(time.time() - start_time),
            )

            # End the session gracefully
            await session_manager.end_session(session_id, reason="disconnected")

        except Exception as e:
            logger.error(
                "websocket_fatal_error",
                tenant_id=str(tenant.id),
                session_id=session_id,
                error_type=type(e).__name__,
                error=str(e),
            )

            # Try to end the session
            try:
                await session_manager.end_session(session_id, reason="error")
            except Exception:
                pass

            try:
                await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            except Exception:
                pass

        finally:
            await speech.close()
