"""
Voice Agent Platform — LiveKit Agent Worker

The real-time voice agent that connects to LiveKit rooms and handles
live audio conversations with customers via WebRTC.

Architecture:
    LiveKit Room (WebRTC audio)
        → Silero VAD (detect speech)
            → Deepgram/Sarvam STT (speech → text)
                → DeepSeek LLM via OpenAI plugin (text → AI response + tool calls)
                    → ActionRouter (execute restaurant API calls)
                → Deepgram/Sarvam TTS (AI response → speech)
            → LiveKit Room (play audio back)

This runs as a SEPARATE PROCESS alongside the FastAPI server:
    python -m app.livekit_agent dev

The agent worker registers with the LiveKit server and is dispatched
to rooms when participants join. Each room gets its own agent instance.

Tenant context (tenant_id, api_key, restaurant_id, language) is passed
via participant metadata as JSON.
"""

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

# Ensure UTF-8 output on Windows terminals to avoid charmap encoding errors with Indic scripts
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import re

from dotenv import load_dotenv
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
    metrics,
)
from livekit.agents.voice import AgentSession, Agent
from livekit.plugins import deepgram, openai, sarvam, silero

from app.livekit_tools import RestaurantTools

load_dotenv()

logger = logging.getLogger("voice-agent-livekit")
logger.setLevel(logging.INFO)


# ──────────────────────────────────────────────────────────────
# TTS Text Filter — strips DeepSeek token leakage before Sarvam
# ──────────────────────────────────────────────────────────────

# Regex to detect DeepSeek internal tool-call tokens that sometimes
# bleed into the text stream (e.g. "function<｜tool▁sep｜>create_order")
_TOOL_LEAK_PATTERN = re.compile(
    r"<[^>]+>|function\s*<|json\s*\{",
    re.IGNORECASE,
)

# Unicode ranges considered "safe" for Sarvam (Tamil + Latin + digits + punctuation)
# Blocks: Basic Latin, Latin-1, Tamil script, common punctuation
_SAFE_CHARS_PATTERN = re.compile(
    r"[^\u0000-\u007F\u0B80-\u0BFF\u0900-\u097F\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F]+"
)


def _clean_tts_text(text: str) -> str:
    """
    Strip DeepSeek tool-call token leakage and unsupported Unicode characters
    from text before it is sent to the Sarvam TTS engine.

    Sarvam rejects text with Arabic/CJK/etc. characters with a 400 error.
    DeepSeek occasionally leaks its internal chain-of-thought or tool tokens
    (e.g. 'تمام الدايرهfunction<｜tool▁sep｜>create_order') into the stream.
    """
    # Remove tool-call token patterns
    text = _TOOL_LEAK_PATTERN.sub("", text)
    # Remove characters outside safe Unicode ranges
    text = _SAFE_CHARS_PATTERN.sub("", text)
    return text.strip()


class FilteredAgent(Agent):
    """
    Agent subclass that intercepts the TTS text stream and strips garbage
    characters (DeepSeek token leakage, unsupported Unicode) before they
    reach the Sarvam TTS engine.

    Filtering is only applied for non-English (Sarvam) TTS. Deepgram
    handles all Unicode natively, so filtering English text would strip
    curly quotes, em-dashes, etc. and cause words to be skipped.
    """

    def __init__(self, *, language: str = "english", **kwargs):
        super().__init__(**kwargs)
        self._language = language

    async def tts_node(self, text, model_settings):
        """Filter TTS input text before passing to the TTS plugin."""
        if self._language == "english":
            async for frame in super().tts_node(text, model_settings):
                yield frame
            return

        async def filtered_text():
            async for chunk in text:
                cleaned = _clean_tts_text(chunk)
                if cleaned:
                    yield cleaned

        async for frame in super().tts_node(filtered_text(), model_settings):
            yield frame


# ──────────────────────────────────────────────────────────────
# System Prompt Builder (standalone — no DB dependency)
# ──────────────────────────────────────────────────────────────

def build_instruction_text(tenant_name: str, language: str, voice_config: dict | None = None) -> str:
    """
    Build the LLM instruction text for the LiveKit agent.

    Uses the same guidelines as PromptBuilder but in a format suited
    for LiveKit's Agent(instructions=...) parameter.
    """
    voice_config = voice_config or {}
    tone = voice_config.get("tone", "friendly and professional")
    extra = voice_config.get("extra_instructions", "")

    instruction = (
        f"You are a friendly and helpful voice ordering assistant for {tenant_name}. "
        f"Help customers browse the menu, ask about items, and place orders. "
        f"Be warm, conversational, and efficient. Keep responses concise since they will be spoken aloud. "
        f"Always confirm order details (items, quantities, special instructions) before placing an order. "
        f"If a customer is unsure, suggest popular items or ask about their preferences. "
        f"Use a {tone} tone. "
        f"Keep responses short — 2 to 3 sentences max — since this is a live voice conversation. "
        f"NEVER use markdown formatting (asterisks, dashes, hashes). Write in plain conversational text. "
        f"NEVER make up menu items or prices. Always use check_menu or get_item_details to get real data. "
        f"When you need live data, call check_menu, get_item_details, or get_item_price straight away. "
        f"Do NOT announce that you are about to look something up — the system plays that line itself if the lookup is slow. "
        f"ALWAYS confirm the order summary with the customer before calling create_order. "
    )

    # Language-specific instructions
    # Dish names must be phonetically transliterated to local script so the
    # Sarvam TTS engine can pronounce them naturally instead of spelling out English letters.
    SCRIPT_NAMES = {
        "tamil": "Tamil",
        "telugu": "Telugu",
        "kannada": "Kannada",
        "malayalam": "Malayalam",
    }
    MENU_NAME_RULE = ""
    if language in SCRIPT_NAMES:
        script = SCRIPT_NAMES[language]
        MENU_NAME_RULE = (
            f" CRITICAL RULE about menu item names: This is a VOICE agent using {script} TTS. "
            f"You MUST phonetically transliterate ALL dish names and category names into {script} script "
            f"so the TTS engine can pronounce them naturally. Do NOT leave any English words as-is — "
            f"the TTS will spell them out letter by letter. "
            f"Keep numbers in digits (e.g. 65, not spelled out in {script}). "
            f"Transliterate based on how the word SOUNDS in English, not its meaning. "
            f"EXCEPTION: Order IDs, reference codes, and technical identifiers (e.g. ORD-07f1014f) "
            f"must ALWAYS stay in English letters exactly as received — NEVER transliterate them to {script} script."
        )

    if language == "telugu":
        instruction += (
            " IMPORTANT: Speak in casual, friendly, conversational spoken Telugu, NOT formal textbook Telugu. "
            "Write everyday loanwords naturally in Telugu script instead of archaic words. "
            "Reply entirely in Telugu script."
            + MENU_NAME_RULE
        )
    elif language == "tamil":
        instruction += (
            " IMPORTANT: Speak in casual, natural conversational spoken Tamil slang (Koduntamil), NOT formal literary bookish Tamil (Senthamizh). "
            "Do not use formal words like 'எங்கள்' (use 'எங்க'), 'உள்ளன' or 'உள்ளது' (use 'இருக்கு'), or 'உணவுகள்' (use 'ஐட்டம்ஸ்' or 'சாப்பாடு'). "
            "Reply ENTIRELY in Tamil script at ALL times — NEVER switch to English sentences mid-conversation. "
            "Keep responses friendly, quick, and natural for a voice conversation."
            + MENU_NAME_RULE + "\n"
            "TAMIL ORDER CONFIRMATION RULES:\n"
            "(1) Confirm the order EXACTLY ONCE by reading back the items in Tamil.\n"
            "(2) The moment the customer says any of these — 'எஸ்', 'சரி', 'ஆமா', 'ஓகே', 'பண்ணுங்க', 'ம்', 'yes', 'ok' — IMMEDIATELY call create_order. Do NOT ask again.\n"
            "(3) If the customer says 'வேறு ஒன்னும் வேண்டாம்', 'போதும்', 'அவ்வளவுதான்', 'இதுதான் வேணும்' after you ask about their order — treat this as YES and IMMEDIATELY call create_order. Do NOT ask again.\n"
            "(4) NEVER ask for confirmation a second time.\n"
            "(5) NEVER use markdown formatting (dashes, bullet points, asterisks) — write in plain spoken Tamil only.\n"
            "Here are examples of how you MUST talk (notice dish names are in Tamil script, not English):\n"
            "User: மெனுல என்ன இருக்கு?\n"
            "You: நம்ம மெனுல நெறைய ஐட்டம்ஸ் இருக்கு. என்ன சாப்பிட பிடிக்கும்?\n"
            "User: சிக்கன் பிரியாணி வேணும்.\n"
            "You: சரி! ஒரு சிக்கன் பிரியாணி ஆர்டர் பண்ணலாமா? வேற ஏதாவது வேணுமா?\n"
            "User: எஸ் பண்ணுங்க.\n"
            "You: (calls create_order immediately)\n"
            "User: நான் எப்படி பே பண்ணலாம்?\n"
            "You: நீங்க UPI இல்லனா கேஷ் ஆன் டெலிவரில பே பண்ணலாம்."
        )
    elif language == "kannada":
        instruction += (
            " IMPORTANT: Speak in casual, natural conversational Kannada. "
            "Reply entirely in Kannada script."
            + MENU_NAME_RULE
        )
    elif language == "malayalam":
        instruction += (
            " IMPORTANT: Speak in casual, natural conversational Malayalam. "
            "Reply entirely in Malayalam script."
            + MENU_NAME_RULE
        )

    if extra:
        instruction += f" {extra}"

    return instruction


def build_initial_greeting(tenant_name: str, language: str, participant_name: str = "", voice_config: dict | None = None) -> str:
    """Build the initial greeting the agent speaks when a customer connects."""
    voice_config = voice_config or {}
    # Note: Custom greetings from voice_config are ignored in favor of
    # the dynamic greeting that includes participant name and tenant name.
    # To re-enable, uncomment the lines below:
    # custom_greeting = voice_config.get("greeting", "")
    # if custom_greeting:
    #     return custom_greeting

    name_part = f" {participant_name}" if participant_name else ""

    greetings = {
        "english": f"Hello{name_part}! Welcome to {tenant_name}. How can I help you today?",
        "telugu": f"నమస్కారం{name_part}! {tenant_name} కి స్వాగతం. నేను మీకు ఎలా సహాయం చేయగలను?",
        "tamil": f"வணக்கம்{name_part}! {tenant_name} க்கு வரவேற்கிறோம். நான் எப்படி உங்களுக்கு உதவ முடியும்?",
        "kannada": f"ನಮಸ್ಕಾರ{name_part}! {tenant_name} ಗೆ ಸ್ವಾಗತ. ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
        "malayalam": f"നമസ്കാരം{name_part}! {tenant_name} ലേക്ക് സ്വാഗതം. എനിക്ക് നിങ്ങളെ എങ്ങനെ സഹായിക്കാനാകും?",
    }

    return greetings.get(language, greetings["english"])


# ──────────────────────────────────────────────────────────────
# Language → STT/TTS Plugin Configuration
# ──────────────────────────────────────────────────────────────

# Maps language names to Sarvam language codes
SARVAM_LANGUAGE_MAP = {
    "english": "en-IN",
    "telugu": "te-IN",
    "tamil": "ta-IN",
    "kannada": "kn-IN",
    "malayalam": "ml-IN",
}


def get_stt_plugin(language: str):
    """
    Get the appropriate STT plugin for the language.

    English goes to Deepgram, the Indian languages to Sarvam — the same split
    get_tts_plugin makes. The `language != "english"` guard is load-bearing:
    "english" is a key in SARVAM_LANGUAGE_MAP (as en-IN), so without it every
    English call fell through to Sarvam and the Deepgram branch below was
    unreachable. Measured on one utterance of English, final transcript after
    end-of-audio: Deepgram nova-3 42-169 ms, Sarvam saaras never finalised
    within 30 s, because it endpoints with its own server-side VAD on top of
    the Silero VAD this session already runs.

    Set STT_ENGLISH_ENGINE=sarvam to go back to Sarvam for English — worth
    trying if Deepgram mishears strongly accented Indian English, which is the
    one thing Sarvam's en-IN model is better at.
    """
    if language in SARVAM_LANGUAGE_MAP and not (
        language == "english" and os.environ.get("STT_ENGLISH_ENGINE", "deepgram") == "deepgram"
    ):
        return sarvam.STT(language=SARVAM_LANGUAGE_MAP[language])
    # Default: English via Deepgram
    return deepgram.STT(model=os.environ.get("DEEPGRAM_STT_MODEL_NAME", "nova-3"), language="en-US")


def get_tts_plugin(language: str, tts_provider: str = "deepgram", voice_gender: str = "female"):
    """
    Get the appropriate TTS plugin for the language and requested voice.

    voice_gender ("female" default | "male") picks the speaker:
        - English (Deepgram Aura): female aura-asteria-en, male aura-orion-en.
          Override per gender with DEEPGRAM_TTS_VOICE / DEEPGRAM_TTS_VOICE_MALE.
        - Indian languages (Sarvam bulbul): female from SARVAM_TTS_SPEAKER
          (default "priya"); male from SARVAM_TTS_SPEAKER_MALE. The male env has
          NO built-in default — Sarvam speaker names must be valid v3 ids, so if
          it is unset we fall back to the female speaker rather than risk an
          invalid one. Set SARVAM_TTS_SPEAKER_MALE to a real male v3 speaker to
          get a male voice for Indian languages.

    For English, supports two TTS backends:
        - "deepgram" (default): Direct Deepgram TTS
        - "openrouter": Deepgram Flux TTS via OpenRouter
    """
    is_male = voice_gender == "male"

    if language in SARVAM_LANGUAGE_MAP and language != "english":
        female_speaker = os.environ.get("SARVAM_TTS_SPEAKER", "priya")
        speaker = os.environ.get("SARVAM_TTS_SPEAKER_MALE", female_speaker) if is_male else female_speaker
        return sarvam.TTS(
            target_language_code=SARVAM_LANGUAGE_MAP[language],
            model="bulbul:v3",
            speaker=speaker,
        )

    # English TTS
    if tts_provider == "openrouter":
        tts_model = os.environ.get("DEEPGRAM_TTS_MODEL", "deepgram/flux-tts:free")
        female_voice = os.environ.get("DEEPGRAM_TTS_VOICE", "flux-meena-en")
        tts_voice = os.environ.get("DEEPGRAM_TTS_VOICE_MALE", female_voice) if is_male else female_voice
        return openai.TTS(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            model=tts_model,
            voice=tts_voice,
        )
    else:
        # Use Deepgram natively. Aura ships distinct male/female voices.
        if is_male:
            native_voice = os.environ.get("DEEPGRAM_TTS_VOICE_MALE", "aura-orion-en")
        else:
            native_voice = os.environ.get("DEEPGRAM_TTS_VOICE", "aura-asteria-en")
        return deepgram.TTS(model=native_voice)


# ──────────────────────────────────────────────────────────────
# Tenant Resolution (from participant metadata)
# ──────────────────────────────────────────────────────────────

async def resolve_tenant_from_metadata(metadata: dict) -> dict | None:
    """
    Resolve a tenant from the metadata passed by the frontend.

    The frontend sends metadata as JSON with:
        - tenant_id (str): UUID of the tenant
        - api_key (str): Tenant's widget API key
        - restaurant_id (str, optional): Restaurant slug
        - language (str, optional): Language preference
        - tts_provider (str, optional): TTS backend preference

    We resolve the tenant by querying the database using the tenant service.
    If DB is unavailable, we fall back to a minimal tenant dict.
    """
    tenant_id = metadata.get("tenant_id")
    api_key = metadata.get("api_key")

    if not tenant_id or not api_key:
        logger.warning(f"No tenant_id or api_key in participant metadata: {metadata}")
        return None

    try:
        from app.database import create_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from app.redis_client import get_redis_sync
        from app.services.tenant_service import TenantService

        redis = get_redis_sync()
        
        # Create a local engine bound to THIS thread's event loop
        # (livekit-agents spawns a new loop for the worker job)
        local_engine = create_engine()
        local_session = async_sessionmaker(local_engine, expire_on_commit=False)
        
        async with local_session() as db:
            service = TenantService(db=db, redis=redis)
            tenant = await service.get_tenant_by_api_key(api_key)

            if tenant and str(tenant.id) == tenant_id and tenant.is_active:
                logger.info(f"Tenant resolved: {tenant.name} ({tenant.id})")
                await local_engine.dispose()
                return tenant

            if not tenant:
                logger.warning(f"Tenant not found in DB for api_key: {api_key}")
            elif str(tenant.id) != tenant_id:
                logger.warning(f"Tenant ID mismatch: DB={tenant.id}, Meta={tenant_id}")
            elif not tenant.is_active:
                logger.warning(f"Tenant is inactive: {tenant.id}")

            await local_engine.dispose()
            return None

    except Exception as e:
        logger.error(f"Could not resolve tenant from DB: {e}")
        # Fall back: return None — the agent will work without restaurant API tools
        return None


async def load_tenant_tool_specs(tenant_id: str) -> list | None:
    """
    Load the tools this tenant has enabled, as plain ToolSpecs.

    Plain is the point: the worker job runs on its own event loop and the
    engine is disposed before the session starts, so anything still attached to
    a SQLAlchemy session would raise DetachedInstanceError mid-call.

    Returns None if the lookup failed, which leaves the toolset on the built-in
    restaurant tools — a database blip should not leave a caller talking to an
    agent that cannot look at the menu. An empty list is a different answer: it
    means the tenant really has every tool switched off, and that is honoured.
    """
    try:
        from app.database import create_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from app.redis_client import get_redis_sync
        from app.services.tool_registry import ToolRegistry

        redis = get_redis_sync()

        local_engine = create_engine()
        local_session = async_sessionmaker(local_engine, expire_on_commit=False)

        async with local_session() as db:
            specs = await ToolRegistry(db=db, redis=redis).load_specs(tenant_id)

        await local_engine.dispose()
        logger.info(
            f"Loaded {len(specs)} tool(s) for tenant {tenant_id}: "
            f"{', '.join(s.name for s in specs) or 'none'}"
        )
        return specs

    except Exception as e:
        logger.error(f"Could not load tenant tools from DB: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# LiveKit Agent Lifecycle
# ──────────────────────────────────────────────────────────────

def prewarm(proc: JobProcess):
    """
    Prewarm the Voice Activity Detection (VAD) model.

    Called once per worker process at startup to reduce cold-start
    latency when the first room is dispatched to this worker.
    """
    proc.userdata["vad"] = silero.VAD.load()
    logger.info("VAD model prewarmed")


@contextmanager
def _timed(phase: str):
    """
    Time one startup phase and log it as a LATENCY line.

    Same prefix as the per-turn metrics so one `grep LATENCY` over the worker
    log shows the whole picture: first the call-setup phases below (connect →
    participant → tenant → tools → session start → greeting), then the per-turn
    eou/llm/tts/tool lines for every exchange. That is the full path the
    customer waits on, start to finish, with a number on each leg — so "it feels
    slow" always resolves to a specific line instead of a guess.
    """
    t0 = time.perf_counter()
    try:
        yield
    finally:
        logger.info(f"LATENCY phase {phase}={(time.perf_counter() - t0) * 1000:.0f}ms")


async def entrypoint(ctx: JobContext):
    """
    Main entrypoint — called once per LiveKit room.

    1. Connect to the room (audio only)
    2. Wait for the remote participant
    3. Parse participant metadata (tenant, language, etc.)
    4. Resolve tenant from DB
    5. Configure STT/TTS plugins based on language
    6. Start the agent session
    7. Speak the initial greeting
    """
    entry_t0 = time.perf_counter()

    with _timed("room_connect"):
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Wait for the remote participant to join
    with _timed("wait_for_participant"):
        participant = await ctx.wait_for_participant()

    # ── Parse metadata ──────────────────────────────────────────
    metadata = {}
    if participant.metadata:
        try:
            raw_meta = participant.metadata
            if not isinstance(raw_meta, str):
                raw_meta = str(raw_meta)
            metadata = json.loads(raw_meta)
        except Exception as e:
            logger.warning(f"Could not parse participant metadata: {e} — raw: {participant.metadata!r}")

    # Fall back to job metadata if participant metadata is empty
    if not metadata and ctx.job.metadata:
        try:
            raw_job_meta = ctx.job.metadata
            if not isinstance(raw_job_meta, str):
                raw_job_meta = str(raw_job_meta)
            metadata = json.loads(raw_job_meta)
        except Exception as e:
            logger.warning(f"Could not parse job metadata: {e}")

    user_lang = metadata.get("language", "english").lower()
    tts_provider = metadata.get("ttsProvider", "deepgram").lower()
    voice_gender = "male" if metadata.get("voiceGender", "female").lower() == "male" else "female"
    tenant_name = metadata.get("tenant_name", "the restaurant")
    restaurant_id = metadata.get("restaurant_id")
    sub_tenant_id = metadata.get("sub_tenant_id")

    logger.info(
        f"Room started — language: {user_lang}, "
        f"tts_provider: {tts_provider}, voice_gender: {voice_gender}, "
        f"tenant_name: {tenant_name}, "
        f"restaurant_id: {restaurant_id}, "
        f"sub_tenant_id: {sub_tenant_id}"
    )

    # ── Resolve tenant ──────────────────────────────────────────
    from app.redis_client import init_redis, get_redis_sync

    try:
        get_redis_sync()
    except RuntimeError:
        await init_redis()

    with _timed("resolve_tenant"):
        tenant = await resolve_tenant_from_metadata(metadata)

    if tenant:
        # Use tenant name from DB
        tenant_name = getattr(tenant, "name", tenant_name)
        voice_config = getattr(tenant, "voice_config", None) or {}
    else:
        voice_config = {}
        logger.warning("Running without tenant — tool calls to restaurant API will fail")

    # For a multi-tenant account the token already carried the merged display
    # config (sub-tenant overrides on top of the parent), resolved and validated
    # when the token was issued. Prefer it so the agent greets the caller as the
    # sub-tenant rather than the parent platform. Absent for single-tenant
    # accounts, which keep the tenant's own values untouched.
    if metadata.get("display_name"):
        tenant_name = metadata["display_name"]
    if metadata.get("voice_config"):
        voice_config = metadata["voice_config"]

    # ── Configure plugins ───────────────────────────────────────
    stt_plugin = get_stt_plugin(user_lang)
    tts_plugin = get_tts_plugin(user_lang, tts_provider, voice_gender)

    deepseek_llm = openai.LLM(
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
    )

    # ── Build prompts ───────────────────────────────────────────
    instruction_text = build_instruction_text(tenant_name, user_lang, voice_config)
    participant_name = participant.name or ""
    initial_greeting = build_initial_greeting(tenant_name, user_lang, participant_name, voice_config)

    # ── Create agent ────────────────────────────────────────────
    # Extract only what we need from the ORM object into a plain dict
    # so we don't hit DetachedInstanceError across async threads
    tenant_dict = None
    if tenant:
        tenant_dict = {
            "id": str(tenant.id),
            "name": tenant.name,
            "api_base_url": tenant.api_base_url,
            "api_auth_token": tenant.api_auth_token,
        }

    # Which tools this tenant has enabled, straight from its dashboard config.
    # None — no tenant, or the lookup failed — leaves RestaurantTools on the
    # built-in restaurant set.
    with _timed("load_tool_specs"):
        tool_specs = await load_tenant_tool_specs(str(tenant.id)) if tenant else None

    # One session id for every row this call writes — order events during the
    # call and the conversation log at the end. The room name is unique per
    # call and shared by both sides, so it ties them together cleanly.
    session_id = ctx.room.name

    # Instantiate the tools class with our context
    tools = RestaurantTools(
        tenant=tenant_dict,
        restaurant_id=restaurant_id,
        room=ctx.room,
        language=user_lang,
        specs=tool_specs,
        session_id=session_id,
        sub_tenant_id=sub_tenant_id,
    )

    agent = FilteredAgent(
        language=user_lang,
        instructions=instruction_text,
        tools=[tools],
    )

    # Endpointing is pure dead air: the gap between the customer finishing a
    # sentence and the agent accepting the turn. The SDK default is 0.5 s, which
    # is tuned for a semantic end-of-turn model; with plain Silero VAD, as here,
    # it is simply a fixed wait. 0.4 s trims it without making the agent cut
    # people off mid-pause — lower it further only while watching the
    # LATENCY eou lines above, and see the note in docs about installing
    # livekit-plugins-turn-detector if sub-300 ms is wanted.
    #
    # preemptive_tts starts synthesising as soon as the LLM emits tokens rather
    # than waiting for the speech to be scheduled for playout, which hides the
    # whole TTS first-byte cost behind the LLM — worth ~0.24 s on warm Deepgram
    # and ~1.0 s on Sarvam, whose first byte is slow on every request.
    agent_session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=stt_plugin,
        llm=deepseek_llm,
        tts=tts_plugin,
        turn_handling={
            "endpointing": {
                "min_delay": float(os.environ.get("ENDPOINTING_MIN_DELAY", "0.4")),
                "max_delay": float(os.environ.get("ENDPOINTING_MAX_DELAY", "2.5")),
            },
            "preemptive_generation": {"enabled": True, "preemptive_tts": True},
        },
    )

    # ── Live turn-by-turn logging ───────────────────────────────
    # _persist_conversation only writes the transcript at shutdown, which is no
    # use while watching a live call. These two handlers mirror each turn to the
    # worker log as it happens, so `docker logs -f` reads like a transcript. The
    # TRANSCRIPT prefix is there to grep cleanly out of the SDK's own chatter.
    # Both callbacks must be sync — the SDK does not await event handlers.

    @agent_session.on("user_input_transcribed")
    def _log_user_turn(ev) -> None:
        if ev.is_final:
            logger.info(f"TRANSCRIPT user : {ev.transcript}")

    @agent_session.on("conversation_item_added")
    def _log_agent_turn(ev) -> None:
        if getattr(ev.item, "role", None) == "assistant":
            logger.info(f"TRANSCRIPT agent: {ev.item.text_content}")

    # ── Per-stage latency ───────────────────────────────────────
    # Without this, "the agent feels slow" is untestable: the four stages of a
    # turn sit behind three different vendors and the time could be in any of
    # them. The SDK already times each one, so we just write them out, prefixed
    # LATENCY to grep out of the SDK's chatter:
    #
    #   eou  — silence after the customer stops talking before the turn is
    #          declared over, plus how long the final transcript lagged. Mostly
    #          the endpointing min_delay below, and STT finalisation.
    #   llm  — time to first token. Measured at 2.1-5.6 s on gpt-6-luna and
    #          ~1.2 s on the small models; reasoning_tokens says whether the
    #          model is thinking before it answers.
    #   tts  — time to first audio byte. ~0.24 s on warm Deepgram Aura, ~1.0 s
    #          on Sarvam bulbul.
    #
    # A turn that calls a tool pays the llm cost twice, once to pick the tool
    # and once to word the answer, so llm is the number to watch.
    @agent_session.on("metrics_collected")
    def _log_latency(ev) -> None:
        m = ev.metrics
        if isinstance(m, metrics.EOUMetrics):
            logger.info(
                f"LATENCY eou  end_of_utterance={m.end_of_utterance_delay * 1000:.0f}ms "
                f"transcription={m.transcription_delay * 1000:.0f}ms"
            )
        elif isinstance(m, metrics.LLMMetrics):
            if not m.cancelled:
                logger.info(
                    f"LATENCY llm  ttft={m.ttft * 1000:.0f}ms "
                    f"total={m.duration * 1000:.0f}ms "
                    f"prompt_tokens={m.prompt_tokens} "
                    f"reasoning_tokens={m.reasoning_tokens}"
                )
        elif isinstance(m, metrics.TTSMetrics):
            if not m.cancelled:
                logger.info(
                    f"LATENCY tts  ttfb={m.ttfb * 1000:.0f}ms "
                    f"audio={m.audio_duration * 1000:.0f}ms "
                    f"conn_reused={m.connection_reused}"
                )
        elif isinstance(m, metrics.STTMetrics):
            # Streaming STT (the normal case) reports duration=0 because the
            # transcript lands incrementally; the real finalisation cost shows up
            # in the eou line's transcription_delay. This line still matters for
            # the non-streaming fallback and for spotting a slow STT connection.
            logger.info(
                f"LATENCY stt  duration={m.duration * 1000:.0f}ms "
                f"audio={m.audio_duration * 1000:.0f}ms "
                f"streamed={m.streamed}"
            )

    # ── Persist the conversation when the call ends ─────────────
    # The LiveKit path has no per-turn database logging (unlike the WebSocket
    # pipeline),
    # so we capture the whole call once, at shutdown: pull the final transcript
    # from the session history and write a single conversation_logs row with the
    # outcome the tools tracked. Registered before start so an early hangup is
    # still logged. See app/services/livekit_logging.py for why it runs on its
    # own engine.
    call_started_at = datetime.now(timezone.utc)

    async def _persist_conversation() -> None:
        if not tenant_dict:
            # No tenant resolved — nothing to attribute the conversation to.
            return
        from app.services.livekit_logging import log_conversation

        # Build [{role, content}] from the chat history, keeping only spoken
        # turns (user/assistant messages) and dropping tool-call plumbing.
        messages: list[dict] = []
        try:
            for item in agent_session.history.items:
                if getattr(item, "type", None) != "message":
                    continue
                role = getattr(item, "role", None)
                if role not in ("user", "assistant"):
                    continue
                text = getattr(item, "text_content", None) or ""
                if text:
                    messages.append({"role": role, "content": text})
        except Exception as e:  # noqa: BLE001
            logger.error(f"Could not read session history for logging: {e}")

        duration = int((datetime.now(timezone.utc) - call_started_at).total_seconds())

        await log_conversation(
            tenant_id=tenant_dict["id"],
            session_id=session_id,
            restaurant_id=restaurant_id,
            sub_tenant_id=sub_tenant_id,
            messages=messages,
            outcome=tools.outcome,
            order_value=tools.order_value,
            duration_seconds=duration,
            started_at=call_started_at,
        )

    ctx.add_shutdown_callback(_persist_conversation)

    # ── Start ───────────────────────────────────────────────────
    with _timed("session_start"):
        await agent_session.start(agent, room=ctx.room)

    # Send the LLM instructions to the frontend for Debug Mode
    try:
        debug_payload = json.dumps({
            "type": "debug",
            "event": "llm_prompt",
            "instructions": instruction_text
        }).encode()
        await ctx.room.local_participant.publish_data(debug_payload, reliable=True)
    except Exception as e:
        logger.error(f"Failed to publish debug data: {e}")

    with _timed("greeting_say"):
        await agent_session.say(initial_greeting, allow_interruptions=True)

    logger.info(
        f"LATENCY phase total_setup={(time.perf_counter() - entry_t0) * 1000:.0f}ms "
        f"(connect → first greeting spoken)"
    )

    logger.info(
        f"Agent session started in room {ctx.room.name} — "
        f"tenant: {tenant_name}, language: {user_lang}"
    )


# ──────────────────────────────────────────────────────────────
# CLI Entry Point
# ──────────────────────────────────────────────────────────────
#
# Run with:
#   python -m app.livekit_agent dev
#   python -m app.livekit_agent start   (production)
#
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        )
    )
