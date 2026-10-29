"""
Voice Agent Platform — Speech Service

Handles Speech-to-Text (STT) and Text-to-Speech (TTS) processing.
Routes audio to the correct provider based on the selected language:

    English (en)       → Deepgram via OpenRouter
    Tamil (ta)         → Sarvam AI
    Telugu (te)        → Sarvam AI
    Kannada (kn)       → Sarvam AI
    Malayalam (ml)     → Sarvam AI

Flow:
    STT: Audio bytes + language → Provider → Transcribed text
    TTS: Text + language → Provider → Audio bytes (MP3)

Usage:
    from app.services.speech_service import SpeechService

    speech = SpeechService()

    # Speech-to-Text
    text = await speech.transcribe(audio_bytes, language="ta", audio_format="wav")

    # Text-to-Speech
    audio = await speech.synthesize("வணக்கம்! மெனு பாருங்க.", language="ta")
"""

import base64
import struct
from typing import Any

import httpx

from app.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# Language Configuration
# ──────────────────────────────────────────────────────────────

# Supported languages with their provider routing and Sarvam language codes
LANGUAGE_CONFIG = {
    "en": {
        "provider": "deepgram",
        "name": "English",
        "sarvam_code": None,
    },
    "ta": {
        "provider": "sarvam",
        "name": "Tamil",
        "sarvam_code": "ta-IN",
    },
    "te": {
        "provider": "sarvam",
        "name": "Telugu",
        "sarvam_code": "te-IN",
    },
    "kn": {
        "provider": "sarvam",
        "name": "Kannada",
        "sarvam_code": "kn-IN",
    },
    "ml": {
        "provider": "sarvam",
        "name": "Malayalam",
        "sarvam_code": "ml-IN",
    },
}

SUPPORTED_LANGUAGES = list(LANGUAGE_CONFIG.keys())
DEFAULT_LANGUAGE = "en"


class SpeechServiceError(Exception):
    """Base exception for speech service errors."""

    def __init__(self, message: str, provider: str | None = None):
        super().__init__(message)
        self.provider = provider


class SpeechService:
    """
    Unified speech service that routes STT/TTS to the correct provider.

    - English → Deepgram (via OpenRouter API)
    - South Indian languages → Sarvam AI (direct API)
    """

    @staticmethod
    def is_supported_language(language: str) -> bool:
        return language in SUPPORTED_LANGUAGES

    @staticmethod
    def get_supported_languages() -> list[dict]:
        return [
            {"code": code, "name": config["name"]}
            for code, config in LANGUAGE_CONFIG.items()
        ]

    def __init__(self):
        self.settings = get_settings()
        self._http_client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create a reusable async HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=60.0)  # 60s for large TTS
        return self._http_client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    # ──────────────────────────────────────────────────────────
    # Speech-to-Text (STT)
    # ──────────────────────────────────────────────────────────

    async def transcribe(
        self,
        audio_data: bytes,
        language: str = DEFAULT_LANGUAGE,
        audio_format: str = "wav",
    ) -> str:
        """
        Transcribe audio to text using the appropriate provider.

        Args:
            audio_data: Raw audio bytes.
            language: Language code ("en", "ta", "te", "kn", "ml").
            audio_format: Audio format ("wav", "mp3", "webm", "ogg").

        Returns:
            Transcribed text string.

        Raises:
            SpeechServiceError: If transcription fails.
        """
        language = language.lower().strip()
        if language not in LANGUAGE_CONFIG:
            raise SpeechServiceError(
                f"Unsupported language: {language}. "
                f"Supported: {', '.join(SUPPORTED_LANGUAGES)}"
            )

        config = LANGUAGE_CONFIG[language]
        provider = config["provider"]

        logger.info(
            "stt_request",
            language=language,
            provider=provider,
            audio_size=len(audio_data),
            audio_format=audio_format,
        )

        try:
            if provider == "deepgram":
                text = await self._deepgram_stt(audio_data, audio_format)
            else:
                text = await self._sarvam_stt(audio_data, config["sarvam_code"], audio_format)

            logger.info(
                "stt_success",
                language=language,
                provider=provider,
                text_length=len(text),
            )
            return text

        except SpeechServiceError:
            raise
        except Exception as e:
            logger.error(
                "stt_unexpected_error",
                language=language,
                provider=provider,
                error_type=type(e).__name__,
                error=str(e),
            )
            raise SpeechServiceError(
                f"Speech-to-text failed ({provider}): {str(e)}",
                provider=provider,
            )

    async def _deepgram_stt(self, audio_data: bytes, audio_format: str) -> str:
        """
        Transcribe audio using Deepgram via OpenRouter.

        Endpoint: POST https://openrouter.ai/api/v1/audio/transcriptions
        Auth: Bearer token (OpenRouter API key)
        Body: JSON with model + base64-encoded audio
        """
        client = await self._get_client()

        # Encode audio to base64
        audio_b64 = base64.b64encode(audio_data).decode("utf-8")

        response = await client.post(
            f"{self.settings.OPENROUTER_BASE_URL}/audio/transcriptions",
            headers={
                "Authorization": f"Bearer {self.settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.DEEPGRAM_STT_MODEL,
                "input_audio": {
                    "data": audio_b64,
                    "format": audio_format,
                },
            },
        )

        if response.status_code != 200:
            error_text = response.text
            logger.error(
                "deepgram_stt_api_error",
                status=response.status_code,
                error=error_text[:500],
            )
            raise SpeechServiceError(
                f"Deepgram STT failed (HTTP {response.status_code}): {error_text[:200]}",
                provider="deepgram",
            )

        result = response.json()

        # OpenRouter returns OpenAI-compatible response format
        text = result.get("text", "")
        if not text:
            # Try alternative response formats
            text = result.get("transcript", "")
            if not text and "results" in result:
                # Deepgram native format (in case OpenRouter passes it through)
                channels = result.get("results", {}).get("channels", [])
                if channels:
                    alternatives = channels[0].get("alternatives", [])
                    if alternatives:
                        text = alternatives[0].get("transcript", "")

        if not text:
            raise SpeechServiceError(
                "Deepgram returned empty transcription",
                provider="deepgram",
            )

        return text.strip()

    async def _sarvam_stt(
        self,
        audio_data: bytes,
        language_code: str,
        audio_format: str,
    ) -> str:
        """
        Transcribe audio using Sarvam AI (direct API).

        Endpoint: POST https://api.sarvam.ai/speech-to-text
        Auth: API-Subscription-Key header
        Body: Multipart form with audio file + model + language_code
        """
        client = await self._get_client()

        # Map format to MIME type
        mime_map = {
            "wav": "audio/wav",
            "mp3": "audio/mpeg",
            "webm": "audio/webm",
            "ogg": "audio/ogg",
            "flac": "audio/flac",
            "aac": "audio/aac",
        }
        mime_type = mime_map.get(audio_format, "audio/wav")

        response = await client.post(
            "https://api.sarvam.ai/speech-to-text",
            headers={
                "API-Subscription-Key": self.settings.SARVAM_API_KEY,
            },
            files={
                "file": (f"audio.{audio_format}", audio_data, mime_type),
            },
            data={
                "model": self.settings.SARVAM_STT_MODEL,
                "language_code": language_code,
            },
        )

        if response.status_code != 200:
            error_text = response.text
            logger.error(
                "sarvam_stt_api_error",
                status=response.status_code,
                language_code=language_code,
                error=error_text[:500],
            )
            raise SpeechServiceError(
                f"Sarvam STT failed (HTTP {response.status_code}): {error_text[:200]}",
                provider="sarvam",
            )

        result = response.json()
        text = result.get("transcript", "")

        if not text:
            raise SpeechServiceError(
                "Sarvam returned empty transcription",
                provider="sarvam",
            )

        return text.strip()

    # ──────────────────────────────────────────────────────────
    # Text-to-Speech (TTS)
    # ──────────────────────────────────────────────────────────

    async def synthesize(
        self,
        text: str,
        language: str = DEFAULT_LANGUAGE,
        provider: str | None = None,
        voice_id: str | None = None,
        model: str | None = None,
    ) -> bytes:
        """
        Convert text to speech audio using the appropriate provider.

        Args:
            text: Text to convert to speech.
            language: Language code ("en", "ta", "te", "kn", "ml").
            provider: Optional provider override. "elevenlabs" uses ElevenLabs
                for ANY language (its models are multilingual), bypassing the
                language-based Deepgram/Sarvam routing. When None, the provider
                is chosen from the language as before.
            voice_id: ElevenLabs voice id (only used when provider="elevenlabs").
            model: ElevenLabs model id (only used when provider="elevenlabs").

        Returns:
            Audio bytes (MP3 format).

        Raises:
            SpeechServiceError: If synthesis fails.
        """
        language = language.lower().strip()
        if language not in LANGUAGE_CONFIG:
            raise SpeechServiceError(
                f"Unsupported language: {language}. "
                f"Supported: {', '.join(SUPPORTED_LANGUAGES)}"
            )

        config = LANGUAGE_CONFIG[language]
        # An explicit ElevenLabs override wins over the language-based provider.
        provider = (provider or config["provider"]).lower()

        logger.info(
            "tts_request",
            language=language,
            provider=provider,
            text_length=len(text),
        )

        try:
            if provider == "elevenlabs":
                audio = await self._elevenlabs_tts(text, voice_id, model)
            elif provider == "deepgram":
                audio = await self._deepgram_tts(text)
            else:
                audio = await self._sarvam_tts(text, config["sarvam_code"])

            logger.info(
                "tts_success",
                language=language,
                provider=provider,
                audio_size=len(audio),
            )
            return audio

        except SpeechServiceError:
            raise
        except Exception as e:
            logger.error(
                "tts_unexpected_error",
                language=language,
                provider=provider,
                error_type=type(e).__name__,
                error=str(e),
            )
            raise SpeechServiceError(
                f"Text-to-speech failed ({provider}): {str(e)}",
                provider=provider,
            )

    async def _deepgram_tts(self, text: str) -> bytes:
        """
        Generate speech from text using Deepgram Flux TTS via OpenRouter.

        NOTE: OpenRouter returns raw PCM audio (audio/pcm;rate=24000;channels=1),
        NOT mp3. We must wrap it in a WAV header before sending to the browser.

        Endpoint: POST https://openrouter.ai/api/v1/audio/speech
        Model: deepgram/flux-tts:free
        Voice: set via DEEPGRAM_TTS_VOICE (e.g. flux-meena-en)
        Returns: WAV bytes (PCM wrapped with header)
        """
        client = await self._get_client()

        voice = getattr(self.settings, "DEEPGRAM_TTS_VOICE", "flux-meena-en")

        response = await client.post(
            f"{self.settings.OPENROUTER_BASE_URL}/audio/speech",
            headers={
                "Authorization": f"Bearer {self.settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.DEEPGRAM_TTS_MODEL,
                "input": text,
                "voice": voice,
            },
        )

        if response.status_code != 200:
            error_text = response.text
            logger.error(
                "deepgram_tts_api_error",
                status=response.status_code,
                error=error_text[:500],
            )
            raise SpeechServiceError(
                f"Deepgram TTS failed (HTTP {response.status_code}): {error_text[:200]}",
                provider="deepgram",
            )

        audio_data = response.content
        content_type = response.headers.get("content-type", "")

        logger.debug(
            "deepgram_tts_raw_response",
            content_type=content_type,
            raw_size=len(audio_data),
        )

        if not audio_data or len(audio_data) < 100:
            raise SpeechServiceError(
                "Deepgram returned empty or invalid audio",
                provider="deepgram",
            )

        # OpenRouter Flux TTS returns raw PCM — convert to WAV so browser can play it
        if "pcm" in content_type or "audio/wav" not in content_type and "audio/mpeg" not in content_type:
            # Parse rate from content-type, e.g. "audio/pcm;rate=24000;channels=1"
            sample_rate = 24000
            channels = 1
            bits_per_sample = 16

            for part in content_type.split(";"):
                part = part.strip()
                if part.startswith("rate="):
                    try:
                        sample_rate = int(part.split("=")[1])
                    except ValueError:
                        pass
                elif part.startswith("channels="):
                    try:
                        channels = int(part.split("=")[1])
                    except ValueError:
                        pass

            audio_data = _pcm_to_wav(audio_data, sample_rate, channels, bits_per_sample)
            logger.info(
                "deepgram_tts_pcm_converted",
                sample_rate=sample_rate,
                channels=channels,
                wav_size=len(audio_data),
            )

        return audio_data

    async def _sarvam_tts(self, text: str, language_code: str) -> bytes:
        """
        Generate speech from text using Sarvam AI (direct API).

        Endpoint: POST https://api.sarvam.ai/text-to-speech
        Auth: API-Subscription-Key header
        Body: JSON with text + language_code + speaker
        Returns: Audio bytes (base64-decoded or raw)
        """
        client = await self._get_client()

        response = await client.post(
            "https://api.sarvam.ai/text-to-speech",
            headers={
                "API-Subscription-Key": self.settings.SARVAM_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "inputs": [text],
                "target_language_code": language_code,
                "speaker": self.settings.SARVAM_TTS_SPEAKER,
                "model": "bulbul:v3",
            },
        )

        if response.status_code != 200:
            error_text = response.text
            logger.error(
                "sarvam_tts_api_error",
                status=response.status_code,
                language_code=language_code,
                error=error_text[:500],
            )
            raise SpeechServiceError(
                f"Sarvam TTS failed (HTTP {response.status_code}): {error_text[:200]}",
                provider="sarvam",
            )

        result = response.json()

        # Sarvam returns base64-encoded audio in the "audios" array
        audios = result.get("audios", [])
        if not audios:
            raise SpeechServiceError(
                "Sarvam returned no audio data",
                provider="sarvam",
            )

        # Decode the base64 audio
        audio_b64 = audios[0]
        audio_data = base64.b64decode(audio_b64)

        if not audio_data or len(audio_data) < 100:
            raise SpeechServiceError(
                "Sarvam returned empty or invalid audio",
                provider="sarvam",
            )

        return audio_data

    async def _elevenlabs_tts(
        self,
        text: str,
        voice_id: str | None = None,
        model: str | None = None,
    ) -> bytes:
        """
        Generate speech from text using ElevenLabs (direct API).

        ElevenLabs is multilingual, so one voice id serves every language — the
        caller passes the tenant's configured voice_id/model, falling back to
        the server defaults (ELEVENLABS_VOICE_ID / ELEVENLABS_MODEL).

        Endpoint: POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
        Auth: xi-api-key header
        Returns: MP3 bytes (audio/mpeg).
        """
        client = await self._get_client()

        api_key = (getattr(self.settings, "ELEVENLABS_API_KEY", "") or "").strip()
        if not api_key or api_key == "placeholder":
            raise SpeechServiceError(
                "ElevenLabs is not configured (ELEVENLABS_API_KEY missing).",
                provider="elevenlabs",
            )

        voice = (voice_id or "").strip() or getattr(
            self.settings, "ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
        )
        model_id = (model or "").strip() or getattr(
            self.settings, "ELEVENLABS_MODEL", "eleven_multilingual_v2"
        )

        response = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
            params={"output_format": "mp3_44100_128"},
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={"text": text, "model_id": model_id},
        )

        if response.status_code != 200:
            error_text = response.text
            logger.error(
                "elevenlabs_tts_api_error",
                status=response.status_code,
                error=error_text[:500],
            )
            raise SpeechServiceError(
                f"ElevenLabs TTS failed (HTTP {response.status_code}): {error_text[:200]}",
                provider="elevenlabs",
            )

        audio_data = response.content
        if not audio_data or len(audio_data) < 100:
            raise SpeechServiceError(
                "ElevenLabs returned empty or invalid audio",
                provider="elevenlabs",
            )

        return audio_data

    # ──────────────────────────────────────────────────────────
    # Utility
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def get_supported_languages() -> list[dict[str, str]]:
        """Return list of supported languages with metadata."""
        return [
            {
                "code": code,
                "name": config["name"],
                "provider": config["provider"],
            }
            for code, config in LANGUAGE_CONFIG.items()
        ]

    @staticmethod
    def is_supported_language(language: str) -> bool:
        """Check if a language code is supported."""
        return language.lower().strip() in LANGUAGE_CONFIG


def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 24000, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    """
    Wrap raw PCM bytes in a WAV (RIFF) container header.
    Browsers can play WAV natively — they cannot play raw PCM.

    WAV header format (44 bytes):
      RIFF chunk: 'RIFF' + file_size(4) + 'WAVE'
      fmt  chunk: 'fmt ' + 16(4) + PCM(2) + channels(2) + sample_rate(4)
                  + byte_rate(4) + block_align(2) + bits_per_sample(2)
      data chunk: 'data' + data_size(4) + <pcm bytes>
    """
    data_size = len(pcm_data)
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    file_size = 36 + data_size  # 36 = header size excluding 'RIFF' + file_size field

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        file_size,
        b"WAVE",
        b"fmt ",
        16,              # fmt chunk size
        1,               # audio format: PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + pcm_data
