"""Endpoint-aware Deepgram TTS: Aura (v1/speak) and Flux (v2/speak) in one bot.

Example config:

    tts:
      name: channels.deepgram_tts.EndpointAwareDeepgramTTS
      endpoint: wss://<proxy>/Voicebot-deepgram/v1/speak
      language_map:
        en-GB:
          language: en
          model: flux-colin-en          # Flux → this engine uses /v2/speak
        fr-BE:
          language: fr-fr
          model: aura-2-agathe-fr       # Aura → stays on /v1/speak
        nl-BE:
          language: nl-nl
          model: aura-2-daphne-nl
          # endpoint: wss://other-host/.../v1/speak   # optional per-language override

Why this exists
---------------
Native Deepgram TTS uses one endpoint for every language. Flux
(flux-*) needs /v2/speak; Aura (aura-*) needs /v1/speak.
This class picks the path from the current model on each connect.

NOTE: A per-language endpoint key in language_map overrides the rewrite.

Requires Rasa Pro 3.17+ and DEEPGRAM_API_KEY like native Deepgram TTS.
"""

from typing import AsyncIterator, Dict, List, Optional

import aiohttp
import orjson
import structlog
from aiohttp import WSMsgType

from rasa.core.channels.voice_stream.audio_bytes import AudioFormat, RasaAudioBytes
from rasa.core.channels.voice_stream.tts.deepgram import DeepgramTTS, DeepgramTTSConfig
from rasa.core.channels.voice_stream.tts.tts_engine import TTSError

logger = structlog.get_logger()

_V1_SPEAK = "/v1/speak"
_V2_SPEAK = "/v2/speak"

# Aura: Flushed / Cleared / Close. Flux: SpeechMetadata / SpeechInterrupted.
_TURN_COMPLETE = frozenset(
    {"Flushed", "Close", "Cleared", "SpeechMetadata", "SpeechInterrupted"}
)


def _uses_flux(model: Optional[str]) -> bool:
    return (model or "").startswith("flux-")


def _swap_speak_path(url: str, *, flux: bool) -> str:
    """Keep the proxy prefix; point the speak path at v1 (Aura) or v2 (Flux)."""
    wanted = _V2_SPEAK if flux else _V1_SPEAK
    other = _V1_SPEAK if flux else _V2_SPEAK
    prefix, found, suffix = url.rpartition(other)
    if found:
        return f"{prefix}{wanted}{suffix}"
    return url


def _optional_endpoints(config: Dict) -> Dict[str, str]:
    """Collect per-language ``endpoint`` keys from ``language_map`` (if any)."""
    endpoints: Dict[str, str] = {}
    for language, entry in (config.get("language_map") or {}).items():
        if isinstance(entry, dict) and entry.get("endpoint"):
            endpoints[language] = entry["endpoint"]
    return endpoints


class EndpointAwareDeepgramTTS(DeepgramTTS):
    """Deepgram TTS with per-language speak URL selection."""

    ws: Optional[aiohttp.ClientWebSocketResponse] = None

    def __init__(
        self,
        rasa_language: str,
        format: AudioFormat,
        config: Optional[DeepgramTTSConfig] = None,
        additional_languages: Optional[List[str]] = None,
    ) -> None:
        super().__init__(rasa_language, format, config, additional_languages)
        # Filled in from_config_dict when a language_map row has its own endpoint.
        self._extra_endpoints: Dict[str, str] = {}

    @classmethod
    def from_config_dict(
        cls,
        config: Dict,
        format: AudioFormat,
        rasa_language: str,
        additional_languages: Optional[List[str]] = None,
    ) -> "EndpointAwareDeepgramTTS":
        engine = super().from_config_dict(
            config, format, rasa_language, additional_languages
        )
        engine._extra_endpoints = _optional_endpoints(config)
        return engine

    def get_websocket_url(self, config: DeepgramTTSConfig) -> str:
        """Build the speak URL for the language currently in use."""
        resolved = config.model_copy(update={"endpoint": self._endpoint_for(config)})
        return super().get_websocket_url(resolved)

    async def signal_interrupt(self) -> None:
        """Barge-in: Aura expects ``Clear``; Flux expects ``Interrupt``."""
        async with self._get_engine_lock():
            payload = {"type": "Interrupt" if self._flux else "Clear"}
            await self._require_ws().send_json(payload)
            logger.debug("deepgram_custom.tts.interrupt", message_type=payload["type"])

    async def stream_audio(self) -> AsyncIterator[RasaAudioBytes]:
        """Yield audio until Aura or Flux reports the turn is finished."""
        try:
            async for message in self._require_ws():
                chunk = self._audio_or_none(message)
                if chunk is not None:
                    yield chunk
                    continue
                if self._turn_is_done(message):
                    break
        except TTSError:
            raise
        except Exception as error:
            logger.error("deepgram_custom.stream_audio.error", error=str(error))
            raise TTSError(f"Error during audio streaming: {error}") from error

    @property
    def _flux(self) -> bool:
        return _uses_flux(self.current_language_config.model)

    def _endpoint_for(self, config: DeepgramTTSConfig) -> str:
        language = self.current_language_config.rasa_language_key
        if language in self._extra_endpoints:
            return self._extra_endpoints[language]
        return _swap_speak_path(config.endpoint or "", flux=self._flux)

    def _require_ws(self) -> aiohttp.ClientWebSocketResponse:
        if not self.ws or self.ws.closed:
            raise TTSError("WebSocket connection not established")
        return self.ws

    def _audio_or_none(self, message: aiohttp.WSMessage) -> Optional[RasaAudioBytes]:
        if message.type == WSMsgType.BINARY:
            return self.engine_bytes_to_rasa_audio_bytes(message.data)
        return None

    def _turn_is_done(self, message: aiohttp.WSMessage) -> bool:
        if message.type == WSMsgType.CLOSED:
            logger.debug("deepgram_custom.stream_audio.ws_closed")
            return True
        if message.type == WSMsgType.ERROR:
            logger.error(
                "deepgram_custom.stream_audio.ws_error", error=str(message.data)
            )
            raise TTSError(f"WebSocket error: {message.data}")
        if message.type != WSMsgType.TEXT:
            return False
        message_type = orjson.loads(message.data).get("type")
        if message_type in _TURN_COMPLETE:
            logger.debug("deepgram_custom.stream_audio.stop", message_type=message_type)
            return True
        return False
