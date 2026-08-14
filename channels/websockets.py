"""Browser WebSocket voice channel for the voice orb UI.

Compatible with Rasa Pro 3.17+. Copy into your bot and register as
``channels.websockets.WebSocketsInputChannel`` in ``credentials.yml``.

Requires only packages already provided by Rasa Pro (``sanic``, ``structlog``,
``rasa``). No extra dependencies.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
import wave
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import structlog
from sanic import Blueprint, HTTPResponse, Request, Websocket, response

from rasa.core.channels.channel import RuntimeAgent
from rasa.core.channels.voice_ready.utils import CallParameters
from rasa.core.channels.voice_stream.asr.asr_event import ASREvent, NewTranscript
from rasa.core.channels.voice_stream.audio_bytes import (
    L16_24KHZ,
    L16_48KHZ,
    MULAW_8KHZ,
    RasaAudioBytes,
)
from rasa.core.channels.voice_stream.call_state import call_state
from rasa.core.channels.voice_stream.tts.tts_engine import TTSEngine
from rasa.core.channels.voice_stream.util import repack_voice_credentials
from rasa.core.channels.voice_stream.voice_channel import (
    ContinueConversationAction,
    EndConversationAction,
    MarkerInput,
    MarkerMessageOutput,
    NewAudioAction,
    VoiceChannelAction,
    VoiceInputChannel,
    VoiceOutputChannel,
    asr_engine_from_config,
    tts_engine_from_config,
)

if TYPE_CHECKING:
    from rasa.core.channels.conversation_queue.queue import ConversationQueue
    from rasa.engine.storage.storage import ModelMetadata

logger = structlog.get_logger()

# Active browser sockets for the optional /trace tool-call broadcast endpoint.
_active_ws: List[Websocket] = []

_SAMPLE_RATE_TO_FORMAT = {
    8000: MULAW_8KHZ,
    24000: L16_24KHZ,
    48000: L16_48KHZ,
}
_DEFAULT_SAMPLE_RATE = 48000


async def _safe_send(ws: Optional[Websocket], payload: Dict[str, Any]) -> None:
    """Send a JSON payload; ignore closed / failed sockets."""
    if ws is None:
        return
    try:
        await ws.send(json.dumps(payload))
    except Exception as e:
        logger.debug("websockets.send_skipped", error=str(e))


class WebsocketsOutputChannel(VoiceOutputChannel):
    """Sends audio, text, and UI events to the browser over WebSocket."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._next_marker_is_end = False
        self._last_sent_skill: Optional[str] = None

    @classmethod
    def name(cls) -> str:
        return "websockets"

    async def send_response_chunk_start(
        self, recipient_id: str, **kwargs: Any
    ) -> None:
        """Notify the UI, then start base-class streaming TTS."""
        active_skill = kwargs.get("active_skill")
        if active_skill and active_skill != self._last_sent_skill:
            self._last_sent_skill = active_skill
            await _safe_send(self.voice_websocket, {"skill": active_skill})

        await _safe_send(self.voice_websocket, {"response_chunk_start": True})
        await super().send_response_chunk_start(recipient_id, **kwargs)

    async def send_response_chunk(
        self, recipient_id: str, chunk: str, **kwargs: Any
    ) -> None:
        """Forward a generative text chunk to the UI and TTS."""
        if not chunk:
            return
        await _safe_send(self.voice_websocket, {"response_chunk": chunk})
        await super().send_response_chunk(recipient_id, chunk, **kwargs)

    async def send_response_chunk_end(
        self, recipient_id: str, **kwargs: Any
    ) -> None:
        """Close a generative stream on the UI, then finish base-class TTS."""
        await _safe_send(self.voice_websocket, {"response_chunk_end": True})
        await super().send_response_chunk_end(recipient_id, **kwargs)

    async def send_text_message(
        self, recipient_id: str, text: str, **kwargs: Any
    ) -> None:
        """Send non-streamed bot text to the UI, then synthesize TTS."""
        if not self._is_duplicate_of_last_streamed_response(text):
            await _safe_send(self.voice_websocket, {"response": text})
        await super().send_text_message(recipient_id, text, **kwargs)

    async def hangup(self, recipient_id: str, **kwargs: Any) -> None:
        """Hang up after current audio finishes (marker echo), not immediately."""
        call_state.should_hangup = True
        logger.info("websockets.hangup_scheduled", recipient_id=recipient_id)

    def rasa_audio_bytes_to_channel_bytes(
        self, rasa_audio_bytes: RasaAudioBytes
    ) -> bytes:
        """Unwrap Rasa's typed audio container to raw bytes for the browser."""
        return rasa_audio_bytes.data

    def channel_bytes_to_message(
        self, recipient_id: str, channel_bytes: bytes
    ) -> str:
        """Encode TTS audio bytes as a base64 JSON message for the browser."""
        return json.dumps(
            {"audio": base64.b64encode(channel_bytes).decode("utf-8")}
        )

    async def send_end_marker(self, marker_input: MarkerInput) -> None:
        """Mark the next marker as final so the browser drains audio first."""
        self._next_marker_is_end = True
        await super().send_end_marker(marker_input)

    async def _send_marker_message_via_websocket(
        self, mark_id: str, marker_message: str
    ) -> None:
        """Send a marker and remember its id for playback-complete detection."""
        await super()._send_marker_message_via_websocket(mark_id, marker_message)
        call_state.latest_bot_audio_id = mark_id

    def create_marker_message(
        self, marker_input: MarkerInput
    ) -> MarkerMessageOutput:
        """Build an audio-boundary marker, optionally with latency metrics."""
        message_id = uuid.uuid4().hex
        marker_data: Dict[str, Any] = {"marker": message_id}

        latency_data = {
            "asr_latency_ms": call_state.asr_latency_ms,
            "response_generation_latency_ms": call_state.rasa_processing_latency_ms,
            "tts_first_byte_latency_ms": call_state.tts_first_byte_latency_ms,
            "tts_complete_latency_ms": call_state.tts_complete_latency_ms,
        }
        latency_data = {k: v for k, v in latency_data.items() if v is not None}
        if latency_data:
            marker_data["latency"] = latency_data

        if self._next_marker_is_end:
            marker_data["final"] = True
            self._next_marker_is_end = False

        return MarkerMessageOutput(
            message_id=message_id, message=json.dumps(marker_data)
        )


class WebSocketsInputChannel(VoiceInputChannel):
    """Receives browser mic audio and drives ASR/TTS for the voice orb.

    Features:
    - Client-provided sample rate and language (matched to ``language_map``)
    - Optional WAV recording for debugging
    - Barge-in via standard ``interruptions`` credentials
    - Transcript / skill / tool-call events for the orb UI
    """

    requires_voice_license = False

    def __init__(
        self,
        server_url: str,
        asr_config: Dict[str, Any],
        tts_config: Dict[str, Any],
        recording: bool = False,
        interruptions: Optional[Dict[str, Any]] = None,
        **_: Any,
    ) -> None:
        """Create the channel.

        Extra credential keys (e.g. legacy ``cfm``) are accepted and ignored so
        older credentials.yml files keep loading.

        Args:
            server_url: Public base URL of the Rasa server.
            asr_config: ASR engine configuration (with ``language_map``).
            tts_config: TTS engine configuration (with ``language_map``).
            recording: When True, write user audio under ``recordings/``.
            interruptions: Barge-in settings (``enabled``, ``min_words``).
        """
        super().__init__(server_url, asr_config, tts_config, interruptions)
        self._recording_enabled = recording
        self._wav_file: Optional[wave.Wave_write] = None
        self._sample_rate = _DEFAULT_SAMPLE_RATE
        self._is_user_speaking = False
        self._channel_websocket: Optional[Websocket] = None
        # Must be a key present in ASR/TTS language_map after connect.
        self.language: str = "en"

    @classmethod
    def name(cls) -> str:
        return "websockets"

    @classmethod
    def from_credentials(
        cls,
        credentials: Optional[Dict[str, Any]],
    ) -> "WebSocketsInputChannel":
        """Build the channel from a credentials.yml block."""
        cls.validate_basic_credentials(credentials)
        return cls(**repack_voice_credentials(credentials or {}))

    def channel_bytes_to_rasa_audio_bytes(
        self, input_bytes: bytes
    ) -> RasaAudioBytes:
        """Wrap raw browser audio bytes in Rasa's typed audio container."""
        return RasaAudioBytes(data=input_bytes, format=self.audio_format)

    def create_output_channel(
        self, voice_websocket: Websocket, tts_engine: TTSEngine
    ) -> VoiceOutputChannel:
        """Create the output channel that streams TTS audio back to the browser."""
        return WebsocketsOutputChannel(
            voice_websocket,
            tts_engine,
            self.tts_cache,
            tts_engine.audio_format,
            min_delay_between_bot_messages_seconds=0.2,
        )

    async def collect_call_parameters(
        self,
        channel_websocket: Websocket,
        request: Optional[Any] = None,
    ) -> Optional[CallParameters]:
        """Read the browser handshake and prepare audio/language for the call."""
        self._channel_websocket = channel_websocket

        sample_rate = _DEFAULT_SAMPLE_RATE
        requested_language = call_state.current_language

        try:
            init_message = await asyncio.wait_for(
                channel_websocket.recv(), timeout=5.0
            )
            init_data = json.loads(init_message)
            sample_rate = init_data.get("sample_rate", sample_rate)
            requested_language = init_data.get("language", requested_language)
        except asyncio.TimeoutError:
            logger.warning("websockets.call_parameters_timeout")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "websockets.call_parameters_parse_error", error=str(e)
            )

        call_id = f"WebSockets-{uuid.uuid4()}"
        self.audio_format = _SAMPLE_RATE_TO_FORMAT.get(
            sample_rate, _SAMPLE_RATE_TO_FORMAT[_DEFAULT_SAMPLE_RATE]
        )
        self.language = self._resolve_language_key(requested_language)
        self._sample_rate = sample_rate

        logger.info(
            "websockets.call_parameters_collected",
            call_id=call_id,
            sample_rate=sample_rate,
            requested_language=requested_language,
            resolved_language=self.language,
        )
        self._start_recording(call_id, "browser", sample_rate)

        return CallParameters(
            call_id=call_id,
            user_phone="browser",
            bot_phone="rasa",
            stream_id=call_id,
            language=self.language,
        )

    def _resolve_language_key(self, requested_language: Optional[str]) -> str:
        """Map the browser language to a configured language_map key."""
        supported = set(self.asr_config.get("language_map") or {}) | set(
            self.tts_config.get("language_map") or {}
        )
        if requested_language and (
            not supported or requested_language in supported
        ):
            return requested_language

        fallback = call_state.current_language or "en"
        logger.warning(
            "websockets.language_not_in_language_map",
            requested=requested_language,
            supported=sorted(supported),
            fallback=fallback,
        )
        return fallback

    def _get_asr_and_tts_engines(
        self, model_metadata: Optional["ModelMetadata"]
    ):
        """Create engines for the resolved call language."""
        additional_languages = (
            model_metadata.additional_languages if model_metadata else None
        )
        asr_engine = asr_engine_from_config(
            asr_config=self.asr_config,
            format=self.audio_format,
            language=self.language,
            additional_languages=additional_languages,
        )
        tts_engine = tts_engine_from_config(
            tts_config=self.tts_config,
            format=self.audio_format,
            language=self.language,
            additional_languages=additional_languages,
        )
        return asr_engine, tts_engine

    async def handle_asr_event(
        self,
        asr_event: ASREvent,
        input_queue: "ConversationQueue",
        call_parameters: CallParameters,
    ) -> None:
        """Send user transcripts to the orb, then use the base handler."""
        if isinstance(asr_event, NewTranscript) and asr_event.text:
            await _safe_send(
                self._channel_websocket, {"trace_event": "user_speaking_start"}
            )
            await _safe_send(
                self._channel_websocket,
                {"trace_event": "transcript", "text": asr_event.text},
            )
        await super().handle_asr_event(asr_event, input_queue, call_parameters)

    async def map_input_message(
        self,
        message: Any,
        ws: Websocket,
    ) -> VoiceChannelAction:
        """Map browser audio / marker messages to channel actions."""
        data = json.loads(message)

        if "audio" in data:
            return self._handle_audio_message(data)

        if "marker" in data:
            return await self._handle_marker_message(data["marker"], ws)

        return ContinueConversationAction()

    def _handle_audio_message(self, data: Dict[str, Any]) -> VoiceChannelAction:
        if not data["audio"]:
            logger.debug("websockets.audio_transmission_stopped")
            self._is_user_speaking = False
            return ContinueConversationAction()

        if not self._is_user_speaking:
            self._is_user_speaking = True
            logger.debug("websockets.user_started_speaking")

        try:
            channel_bytes = base64.b64decode(data["audio"])
            self._append_audio_to_recording(channel_bytes)
            return NewAudioAction(
                self.channel_bytes_to_rasa_audio_bytes(channel_bytes)
            )
        except Exception as e:
            logger.error("websockets.audio_decode_error", error=str(e))
            return ContinueConversationAction()

    async def _handle_marker_message(
        self, received_marker: str, ws: Websocket
    ) -> VoiceChannelAction:
        latest_bot_audio_id = getattr(call_state, "latest_bot_audio_id", None)

        if (
            latest_bot_audio_id is not None
            and received_marker == latest_bot_audio_id
        ):
            call_state.is_bot_speaking = False
            logger.debug(
                "websockets.audio_playback_complete",
                marker=latest_bot_audio_id,
            )
            if call_state.should_hangup:
                logger.debug(
                    "websockets.hangup_requested", marker=latest_bot_audio_id
                )
                await _safe_send(ws, {"hangup": True})
                return EndConversationAction()
        else:
            # A second echo of the same non-final marker means audio drained.
            last_seen = getattr(call_state, "_last_received_marker", None)
            if received_marker == last_seen and call_state.should_hangup:
                logger.debug(
                    "websockets.hangup_requested_deferred_echo",
                    marker=received_marker,
                )
                await _safe_send(ws, {"hangup": True})
                return EndConversationAction()
            call_state.is_bot_speaking = True
            logger.debug(
                "websockets.audio_playback_started", marker=received_marker
            )

        call_state._last_received_marker = received_marker
        return ContinueConversationAction()

    async def interrupt_playback(
        self, ws: Websocket, call_parameters: CallParameters
    ) -> None:
        """Stop browser playback, then cancel in-flight TTS on the server."""
        logger.debug(
            "websockets.interrupt_playback", call_id=call_parameters.call_id
        )
        await _safe_send(ws, {"interruptPlayback": True})
        await super().interrupt_playback(ws, call_parameters)

    def _start_recording(
        self, call_id: str, user_id: str, sample_rate: int = _DEFAULT_SAMPLE_RATE
    ) -> None:
        if not self._recording_enabled:
            return

        os.makedirs("recordings", exist_ok=True)
        file_path = os.path.join("recordings", f"{user_id}_{call_id}.wav")
        self._wav_file = wave.open(file_path, "wb")
        self._wav_file.setnchannels(1)
        self._wav_file.setsampwidth(2)
        self._wav_file.setframerate(sample_rate)
        logger.info(
            "websockets.user_audio_recording.started",
            file_path=file_path,
            call_id=call_id,
            sample_rate=sample_rate,
        )

    def _append_audio_to_recording(self, audio_bytes: bytes) -> None:
        if self._wav_file and self._recording_enabled:
            self._wav_file.writeframes(audio_bytes)

    def _stop_recording(self) -> None:
        if self._wav_file:
            self._wav_file.close()
            self._wav_file = None
            logger.debug("websockets.user_audio_recording.stopped")

    def conversation_blueprint(self, agent: RuntimeAgent) -> Blueprint:
        """Expose health, WebSocket streaming, and optional /trace endpoints."""
        blueprint = Blueprint("websockets", __name__)

        @blueprint.route("/", methods=["GET"])
        async def health(_: Request) -> HTTPResponse:
            return response.json({"status": "ok"})

        @blueprint.websocket("/websocket")
        async def handle_message(request: Request, ws: Websocket) -> None:
            _active_ws.append(ws)
            try:
                logger.info(
                    "websockets.websocket_connection_opened",
                    client=request.ip,
                )
                await self.run_audio_streaming(agent, ws, request)
            except Exception as e:
                logger.exception("websockets.websocket_error", error=str(e))
            finally:
                try:
                    _active_ws.remove(ws)
                except ValueError:
                    pass
                logger.info("websockets.websocket_connection_closed")
                self._stop_recording()

        @blueprint.route("/trace", methods=["POST"])
        async def trace_endpoint(request: Request) -> HTTPResponse:
            """Broadcast tool-call events from actions to connected browsers."""
            try:
                data = request.json or {}
                data.pop("recipient_id", None)
                msg = json.dumps({"trace_event": "tool_call", **data})
                logger.info(
                    "websockets.trace_broadcast",
                    active_ws_count=len(_active_ws),
                    tool=data.get("tool"),
                )
                for active_ws in list(_active_ws):
                    try:
                        await active_ws.send(msg)
                    except Exception as e:
                        logger.warning(
                            "websockets.trace_ws_send_failed", error=str(e)
                        )
            except Exception as e:
                logger.warning(
                    "websockets.trace_endpoint_error", error=str(e)
                )
            return response.json({"ok": True})

        return blueprint
