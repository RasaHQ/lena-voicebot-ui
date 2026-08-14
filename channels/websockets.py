"""Browser-based WebSockets voice channel for Rasa."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional
from sanic.exceptions import WebsocketClosed
import structlog
from sanic import (
    Blueprint,
    HTTPResponse,
    Request,
    Websocket,
    response,
)

from rasa.core.channels.channel import RuntimeAgent
from rasa.core.channels.voice_stream.asr.asr_event import (
    ASREvent,
    NewTranscript,
)
from rasa.core.channels.voice_stream.audio_bytes import RasaAudioBytes
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

# Registry of active voice WebSockets, used by the /trace endpoint to forward
# sub-agent tool-call events from the action server to the browser.
_active_ws: list = []

@dataclass
class CallParameters:
    """Standardized call parameters for voice channels."""

    call_id: str
    user_phone: str
    bot_phone: Optional[str] = None
    user_name: Optional[str] = None
    user_host: Optional[str] = None
    bot_host: Optional[str] = None
    direction: Optional[str] = None
    stream_id: Optional[str] = None
    sample_rate: Optional[int] = None
    language: Optional[str] = None

class WebsocketsOutputChannel(VoiceOutputChannel):
    """Output channel for sending audio to browser clients over WebSocket.

    Supports streaming of generative (Rephraser / Enterprise Search) response chunks
    so the client can show partial text while the full response is being generated.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._next_marker_is_end = False  # flag set before send_end_marker calls create_marker_message
        self._last_sent_skill: Optional[str] = None  # tracks last skill notified to client

    @classmethod
    def name(cls) -> str:
        return "websockets"

    async def send_response_chunk_start(self, recipient_id: str, **kwargs: Any) -> None:
        """Invoked once at the start of a generative response stream (Rephraser / Enterprise Search).

        Calls super() so the base class background TTS streaming task is started
        (if the TTS engine supports streaming input) and streaming_response_sent
        is managed correctly.

        If the caller passes ``active_skill``, we send a ``{"skill": name}``
        message to the browser whenever the skill changes, so the UI can show
        which skill is currently handling the conversation.
        """
        # Notify client of active skill if it has changed since the last turn.
        active_skill = kwargs.get("active_skill")
        if active_skill and active_skill != self._last_sent_skill:
            self._last_sent_skill = active_skill
            try:
                await self.voice_websocket.send(
                    json.dumps({"skill": active_skill})
                )
            except (WebsocketClosed, Exception) as e:
                logger.debug(
                    "websockets.skill_send_skipped",
                    recipient_id=recipient_id,
                    error=str(e),
                )

        # Send the JSON signal to the browser client first.
        try:
            await self.voice_websocket.send(
                json.dumps({"response_chunk_start": True})
            )
        except (WebsocketClosed, Exception) as e:
            logger.debug(
                "websockets.response_chunk_start_send_skipped",
                recipient_id=recipient_id,
                error=str(e),
            )
        # Call super() AFTER so the base-class streaming setup runs and
        # streaming_response_sent / audio_sender_task are initialised.
        await super().send_response_chunk_start(recipient_id, **kwargs)

    async def send_response_chunk(
        self, recipient_id: str, chunk: str, **kwargs: Any
    ) -> None:
        """Invoked for each generated chunk of a generative response."""
        if not chunk:
            return
        try:
            await self.voice_websocket.send(
                json.dumps({"response_chunk": chunk})
            )
        except (WebsocketClosed, Exception) as e:
            logger.debug(
                "websockets.response_chunk_send_skipped",
                recipient_id=recipient_id,
                error=str(e),
            )
        # Delegate to super() so the base class forwards the chunk to the TTS engine
        # when streaming input is supported.
        await super().send_response_chunk(recipient_id, chunk, **kwargs)

    async def send_response_chunk_end(self, recipient_id: str, **kwargs: Any) -> None:
        """Invoked once at the end of a generative response stream.

        Calls super() so the base class flushes the TTS engine, waits for the
        audio sender task and sets streaming_response_sent = True so that the
        subsequent send_text_message call correctly skips re-synthesizing TTS.
        """
        try:
            await self.voice_websocket.send(
                json.dumps({"response_chunk_end": True})
            )
        except (WebsocketClosed, Exception) as e:
            logger.debug(
                "websockets.response_chunk_end_send_skipped",
                recipient_id=recipient_id,
                error=str(e),
            )
        # Must call super() AFTER the JSON send so the base class end-marker and
        # streaming_response_sent flag are set correctly.
        await super().send_response_chunk_end(recipient_id, **kwargs)

    async def send_text_message(
        self, recipient_id: str, text: str, **kwargs: Any
    ) -> None:
        """Send full text to client for display.

        We send the JSON text payload unless the base class would skip it anyway
        (i.e. the text is a duplicate of what was already streamed chunk-by-chunk).
        The actual TTS synthesis / duplicate guard is handled by super().
        """
        if not self._is_duplicate_of_last_streamed_response(text):
            # Only send the JSON text payload when it hasn't already been
            # delivered via streaming chunks.
            try:
                await self.voice_websocket.send(json.dumps({"response": text}))
            except (WebsocketClosed, Exception) as e:
                logger.debug(
                    "websockets.send_text_message_skipped",
                    recipient_id=recipient_id,
                    error=str(e),
                )
        await super().send_text_message(recipient_id, text, **kwargs)

    async def hangup(self, recipient_id: str, **kwargs: Any) -> None:
        """Schedule a graceful hangup after the current audio finishes playing.

        Sets should_hangup so that map_input_message returns EndConversationAction
        when the browser echoes the final audio marker — i.e. after all queued
        TTS has played out. Sending {"hangup": True} immediately was cutting off
        the bot's final words by triggering client disconnect mid-playback.
        """
        call_state.should_hangup = True
        logger.info(
            "websockets.hangup_scheduled",
            recipient_id=recipient_id,
        )

    def rasa_audio_bytes_to_channel_bytes(
        self, rasa_audio_bytes: RasaAudioBytes
    ) -> bytes:
        """Extract raw bytes from RasaAudioBytes for WebSocket transmission."""
        return rasa_audio_bytes.data

    def channel_bytes_to_message(self, recipient_id: str, channel_bytes: bytes) -> str:
        """Wrap audio bytes in JSON with base64 encoding for WebSocket transmission."""
        return json.dumps({
            "audio": base64.b64encode(channel_bytes).decode("utf-8")
        })

    async def send_end_marker(self, marker_input: MarkerInput) -> None:
        """Tag the next marker as final before delegating to the base class.

        The base class send_end_marker calls create_marker_message then sends it.
        By setting _next_marker_is_end=True first, create_marker_message will
        include "final": true in the JSON so the client knows to defer echoing
        this marker until audio drains — keeping is_bot_speaking=True while
        the pre-buffered audio is still playing.
        """
        self._next_marker_is_end = True
        await super().send_end_marker(marker_input)

    async def _send_marker_message_via_websocket(
        self, mark_id: str, marker_message: str
    ) -> None:
        """Send the marker and record it on call_state for the input channel.

        map_input_message (on WebSocketsInputChannel) needs to know the most
        recently sent marker id to detect when the browser has finished
        playing the bot's audio, but it has no direct reference to this output
        channel instance — so the id is mirrored onto the shared call_state.
        """
        await super()._send_marker_message_via_websocket(mark_id, marker_message)
        call_state.latest_bot_audio_id = mark_id

    def create_marker_message(self, marker_input: MarkerInput) -> MarkerMessageOutput:
        """Create a marker message to signal audio boundaries and include latency metrics."""
        message_id = uuid.uuid4().hex
        marker_data = {"marker": message_id}

        # Include comprehensive latency information if available. Field names
        # here match CallState's actual attributes (rasa_processing_latency_ms,
        # tts_first_byte_latency_ms, tts_complete_latency_ms) — do not rename
        # without also updating the orb's expected JSON payload.
        latency_data = {
            "asr_latency_ms": call_state.asr_latency_ms,
            "response_generation_latency_ms": call_state.rasa_processing_latency_ms,
            "tts_first_byte_latency_ms": call_state.tts_first_byte_latency_ms,
            "tts_complete_latency_ms": call_state.tts_complete_latency_ms,
        }

        # Filter out None values from latency data
        latency_data = {k: v for k, v in latency_data.items() if v is not None}

        # Add latency data to marker if any metrics are available
        if latency_data:
            marker_data["latency"] = latency_data

        # Tag end markers so the client defers echoing until audio drains.
        # This keeps call_state.is_bot_speaking=True while pre-buffered audio plays,
        # allowing should_interrupt() to fire correctly during barge-in.
        if self._next_marker_is_end:
            marker_data["final"] = True
            self._next_marker_is_end = False

        return MarkerMessageOutput(message_id=message_id, message=json.dumps(marker_data))

class WebSocketsInputChannel(VoiceInputChannel):
    """Input channel for receiving audio from browser clients over WebSocket.
    
    This channel enables real-time voice communication with browser clients
    using WebSockets and WebSocket. It manages ASR/TTS engines and handles
    the bidirectional audio streaming.
    
    Features:
    - Dynamic sample_rate and language configuration from client
    - Audio recording capability for debugging
    - User speech state tracking
    - Proper hangup signal handling
    """

    requires_voice_license = False

    def __init__(
        self,
        server_url: str,
        asr_config: Dict[str, Any],
        tts_config: Dict[str, Any],
        recording: bool = False,
        interruptions: Optional[Dict[str, int]] = None,
        cfm: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize the browser WebSockets input channel.

        Args:
            server_url: Base URL for the channel server
            asr_config: Configuration dictionary for ASR engine
            tts_config: Configuration dictionary for TTS engine
            recording: Whether to record user audio for debugging
            interruptions: Configuration for handling user interruptions
            cfm: Legacy configuration accepted for backward compatibility.
                Rasa Pro 3.17 does not support it on VoiceInputChannel; use
                ``interruptions`` for barge-in.
        """
        super().__init__(server_url, asr_config, tts_config, interruptions)

        if cfm:
            logger.warning(
                "websockets.cfm_config_ignored",
                reason=(
                    "Rasa Pro 3.17 does not support cfm on VoiceInputChannel; "
                    "configure barge-in with interruptions instead."
                ),
            )
        self._recording_enabled = recording
        self._wav_file: Optional[wave.Wave_write] = None
        self._sample_rate = 16000  # Default sample rate, can be overridden by client
        self._is_user_speaking = False  # Track user speech state
        self._channel_websocket: Optional[Websocket] = None  # Store websocket for hangup signal
        # Resolved by collect_call_parameters(); overwritten with a key that
        # actually exists in the ASR/TTS `language_map` (credentials.yml).
        self.language: str = "en"

    def _start_recording(self, call_id: str, user_id: str, sample_rate: int = 16000) -> None:
        """Start recording user audio to a WAV file for debugging.
        
        Args:
            call_id: Unique call identifier
            user_id: User identifier
            sample_rate: Sample rate of the audio (default: 16000 Hz)
        """
        os.makedirs("recordings", exist_ok=True)
        filename = f"{user_id}_{call_id}.wav"
        file_path = os.path.join("recordings", filename)

        if not self._recording_enabled:
            return

        self._wav_file = wave.open(file_path, "wb")
        self._wav_file.setnchannels(1)  # Mono audio
        self._wav_file.setsampwidth(2)  # 16-bit audio (2 bytes) - matches Int16
        self._wav_file.setframerate(sample_rate)  # Use client-provided sample rate
        logger.info(
            "websockets.user_audio_recording.started",
            file_path=file_path,
            call_id=call_id,
            sample_rate=sample_rate,
        )

    def _append_audio_to_recording(self, audio_bytes: bytes) -> None:
        """Append audio chunk to the recording file.
        
        Args:
            audio_bytes: Audio data to record
        """
        if self._wav_file and self._recording_enabled:
            self._wav_file.writeframes(audio_bytes)

    def _stop_recording(self) -> None:
        """Close the recording file if it's open."""
        if self._wav_file:
            self._wav_file.close()
            self._wav_file = None
            logger.debug("websockets.user_audio_recording.stopped")

    @classmethod
    def name(cls) -> str:
        return "websockets"

    def channel_bytes_to_rasa_audio_bytes(self, input_bytes: bytes) -> RasaAudioBytes:
        """Convert browser audio bytes to Rasa format for ASR."""
        return RasaAudioBytes(data=input_bytes, format=self.audio_format)

    async def collect_call_parameters(
        self,
        channel_websocket: Websocket,
        request: Optional[Any] = None,
    ) -> Optional[CallParameters]:
        """Collect call parameters from the WebSocket connection.

        Waits for the initial JSON message from the browser containing
        ``sample_rate`` and ``language``, resolves ``language`` to a key
        configured in the ASR/TTS ``language_map`` (falling back to the
        bot's default language if the client asked for something unsupported),
        and sets ``self.audio_format`` so that ``_get_asr_and_tts_engines``
        (called by the base-class ``run_audio_streaming``) picks up the
        correct runtime values.
        """
        from rasa.core.channels.voice_stream.browser_audio import _SAMPLE_RATE_TO_FORMAT

        self._channel_websocket = channel_websocket

        sample_rate = 48000
        # call_state.current_language was just seeded from the model's default
        # language by _initialize_call_state(), so it's the right fallback here.
        requested_language = call_state.current_language

        try:
            init_message = await asyncio.wait_for(
                channel_websocket.recv(), timeout=5.0
            )
            init_data = json.loads(init_message)
            sample_rate = init_data.get("sample_rate", sample_rate)
            requested_language = init_data.get("language", requested_language)
        except asyncio.TimeoutError:
            logger.warning("websockets.call_parameters_timeout — using defaults")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("websockets.call_parameters_parse_error", error=str(e))

        call_id = f"WebSockets-{uuid.uuid4()}"

        # Update audio format so _get_asr_and_tts_engines uses the right format.
        self.audio_format = _SAMPLE_RATE_TO_FORMAT.get(
            sample_rate, _SAMPLE_RATE_TO_FORMAT[48000]
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
            sample_rate=sample_rate,
            language=self.language,
        )

    def _resolve_language_key(self, requested_language: Optional[str]) -> str:
        """Resolve the client-requested language to a configured language_map key.

        ``language_map`` in credentials.yml is keyed by whatever value the
        bot's config.yml uses for ``language``/``additional_languages`` (e.g.
        "en", or "en-US" if that's what the assistant is configured with) —
        not necessarily a vendor-specific ASR/TTS code. If the client's
        requested language isn't a configured key, fall back to the bot's
        default language so engine construction doesn't fail.
        """
        supported = set(self.asr_config.get("language_map") or {}) | set(
            self.tts_config.get("language_map") or {}
        )
        if requested_language and (not supported or requested_language in supported):
            return requested_language

        fallback = call_state.current_language
        logger.warning(
            "websockets.language_not_in_language_map",
            requested=requested_language,
            supported=sorted(supported),
            fallback=fallback,
        )
        return fallback

    def _get_asr_and_tts_engines(self, model_metadata: Optional["ModelMetadata"]):
        """Create ASR/TTS engines for the resolved call language.

        Signature must match VoiceInputChannel._get_asr_and_tts_engines since
        the base class's run_audio_streaming calls it with model_metadata.
        """
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

    @classmethod
    def from_credentials(
        cls,
        credentials: Optional[Dict[str, Any]],
    ) -> "WebSocketsInputChannel":
        """Create channel instance from credentials configuration.
        
        Args:
            credentials: Configuration dictionary containing:
                - server_url: Server base URL
                - asr: ASR engine configuration
                - tts: TTS engine configuration
                - recording: Optional boolean to enable audio recording
                - interruptions: Optional interruption configuration
                
        Returns:
            Configured BrowserWebSocketsInputChannel instance
        """
        cls.validate_basic_credentials(credentials)
        new_creds = repack_voice_credentials(credentials or {})
        return cls(**new_creds)

    async def handle_asr_event(
        self,
        asr_event: ASREvent,
        input_queue: "ConversationQueue",
        call_parameters: CallParameters,
    ) -> None:
        """Trace final transcripts to the browser, then delegate to the base handler.

        Interruption handling (should_interrupt / interrupt_playback /
        BargeInInputEvent) already happens generically in the base class's
        receive_asr_events before this is ever called, so this override only
        adds the transcript trace event for the browser's trace panel — the
        replacement for the old (now-removed) _dispatch_voice_agent hook.
        """
        if isinstance(asr_event, NewTranscript) and asr_event.text:
            await self._send_trace_event({"trace_event": "user_speaking_start"})
            await self._send_trace_event(
                {"trace_event": "transcript", "text": asr_event.text}
            )

        await super().handle_asr_event(asr_event, input_queue, call_parameters)

    async def _send_trace_event(self, payload: Dict[str, Any]) -> None:
        """Best-effort send of a trace payload to the connected browser."""
        if self._channel_websocket is None:
            return
        try:
            await self._channel_websocket.send(json.dumps(payload))
        except (WebsocketClosed, Exception) as e:
            logger.debug("websockets.trace_event_send_skipped", error=str(e))

    async def map_input_message(
        self,
        message: Any,
        ws: Websocket,
    ) -> VoiceChannelAction:
        """Map incoming WebSocket message to a voice channel action.
        
        Handles three types of messages:
        1. Audio chunks - convert and send to ASR
        2. Markers - track playback progress and manage state
        3. Control messages - handle call flow
        
        Args:
            message: Raw WebSocket message
            ws: WebSocket connection
            
        Returns:
            VoiceChannelAction indicating what to do next
        """
        data = json.loads(message)

        if "audio" in data:
            # Browser is sending audio data (base64 encoded Int16 PCM)
            
            # Handle empty audio payload gracefully (it should not crash the session)
            if not data["audio"]:
                logger.debug("websockets.audio_transmission_stopped")
                self._is_user_speaking = False
                # Return ContinueConversationAction to keep the session alive
                return ContinueConversationAction()
            
            # If we receive audio, the user is speaking.
            if not self._is_user_speaking:
                self._is_user_speaking = True
                logger.debug("websockets.user_started_speaking")

            try:
                channel_bytes = base64.b64decode(data["audio"])
                self._append_audio_to_recording(channel_bytes)
                
                # Audio is already in correct format from browser
                audio_bytes = self.channel_bytes_to_rasa_audio_bytes(channel_bytes)
                return NewAudioAction(audio_bytes)
            except Exception as e:
                logger.error(
                    "websockets.audio_decode_error",
                    error=str(e),
                )
                return ContinueConversationAction()

        elif "marker" in data:
            received_marker = data["marker"]
            latest_bot_audio_id = getattr(call_state, "latest_bot_audio_id", None)
            # Browser is acknowledging received audio marker
            if latest_bot_audio_id is not None and received_marker == latest_bot_audio_id:
                # Browser finished streaming the last audio bytes
                call_state.is_bot_speaking = False
                logger.debug(
                    "websockets.audio_playback_complete",
                    marker=latest_bot_audio_id,
                )

                if call_state.should_hangup:
                    logger.debug(
                        "websockets.hangup_requested",
                        marker=latest_bot_audio_id,
                    )
                    await ws.send(json.dumps({"hangup": True}))
                    return EndConversationAction()
            else:
                # Check for deferred "audio drained" echo: the client's onended
                # handler echoes lastMarker when activeSources reaches 0. If the
                # final marker was never received by the client, lastMarker will be
                # a non-final marker that was already echoed once immediately. A
                # second echo of the same marker ID is the audio-done signal.
                last_seen = getattr(call_state, "_last_received_marker", None)
                if received_marker == last_seen and call_state.should_hangup:
                    logger.debug(
                        "websockets.hangup_requested_deferred_echo",
                        marker=received_marker,
                    )
                    await ws.send(json.dumps({"hangup": True}))
                    return EndConversationAction()
                # Browser is receiving bot audio
                call_state.is_bot_speaking = True
                logger.debug(
                    "websockets.audio_playback_started",
                    marker=received_marker,
                )
            call_state._last_received_marker = received_marker

        return ContinueConversationAction()

    async def interrupt_playback(
        self, ws: Websocket, call_parameters: CallParameters
    ) -> None:
        """Interrupt the bot: stop client-side audio and cancel server-side TTS.

        Sends the browser signal first so audio stops immediately, then calls
        super() so the base class cancels the in-flight TTS streaming task and
        sets the interrupt flags that gate further response processing.
        Without the super() call the browser goes silent but the server keeps
        streaming, so the next user turn is queued rather than acted on.
        """
        logger.debug(
            "websockets.interrupt_playback",
            call_id=call_parameters.call_id,
        )
        try:
            await ws.send(json.dumps({"interruptPlayback": True}))
        except (WebsocketClosed, Exception) as e:
            logger.debug("websockets.interrupt_playback_send_skipped", error=str(e))
        await super().interrupt_playback(ws, call_parameters)

    def create_output_channel(
        self, voice_websocket: Websocket, tts_engine: TTSEngine
    ) -> VoiceOutputChannel:
        """Create an output channel for this voice input channel.
        
        Args:
            voice_websocket: WebSocket connection for output
            tts_engine: TTS engine for text-to-speech synthesis
            
        Returns:
            Configured BrowserWebSocketsOutputChannel instance
        """
        return WebsocketsOutputChannel(
            voice_websocket,
            tts_engine,
            self.tts_cache,
            tts_engine.audio_format,
            min_delay_between_bot_messages_seconds=0.2,
        )

    def conversation_blueprint(
        self,
        agent: RuntimeAgent,
    ) -> Blueprint:
        """Create a Sanic blueprint for the voice channel endpoints.

        VoiceInputChannel registration tries conversation_blueprint(agent) before
        falling back to blueprint(on_new_message); rasa-pro's built-in voice
        channels (e.g. browser_audio) implement this one so run_audio_streaming
        receives a real RuntimeAgent instead of a bare message callback.

        Provides:
        - GET /: Health check endpoint
        - WebSocket /websocket: Bidirectional audio streaming
        - POST /trace: Forward sub-agent tool-call trace events to the browser

        Args:
            agent: Runtime agent used to drive the conversation for each call.

        Returns:
            Configured Sanic Blueprint
        """
        blueprint = Blueprint("websockets", __name__)

        @blueprint.route("/", methods=["GET"])
        async def health(_: Request) -> HTTPResponse:
            """Health check endpoint."""
            return response.json({"status": "ok"})

        @blueprint.websocket("/websocket")
        async def handle_message(request: Request, ws: Websocket) -> None:
            """WebSocket handler for voice streaming."""
            _active_ws.append(ws)
            try:
                logger.info(
                    "websockets.websocket_connection_opened",
                    client=request.ip,
                )
                await self.run_audio_streaming(agent, ws, request)
            except Exception as e:
                logger.error(
                    "websockets.websocket_error",
                    error=str(e),
                    exc_info=True,
                )
            finally:
                try:
                    _active_ws.remove(ws)
                except ValueError:
                    pass
                logger.info("websockets.websocket_connection_closed")
                self._stop_recording()

        @blueprint.route("/trace", methods=["POST"])
        async def trace_endpoint(request: Request) -> HTTPResponse:
            """Forward sub-agent tool-call trace events to all active browser WebSockets."""
            try:
                data = request.json or {}
                data.pop("recipient_id", None)  # not needed in the browser message
                msg = json.dumps({"trace_event": "tool_call", **data})
                logger.info("websockets.trace_broadcast", active_ws_count=len(_active_ws), tool=data.get("tool"))
                for active_ws in list(_active_ws):
                    try:
                        await active_ws.send(msg)
                    except Exception as _e:
                        logger.warning("websockets.trace_ws_send_failed", error=str(_e))
            except Exception as _e:
                logger.warning("websockets.trace_endpoint_error", error=str(_e))
            return response.json({"ok": True})

        return blueprint