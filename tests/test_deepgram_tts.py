"""Tests for the endpoint-aware custom Deepgram TTS engine."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import WSMsgType

from channels.deepgram_tts import EndpointAwareDeepgramTTS
from rasa.core.channels.voice_stream.audio_bytes import MULAW_8KHZ, AudioFormat
from rasa.shared.constants import DEEPGRAM_API_KEY_ENV_VAR

_PROXY_V1_ENDPOINT = "wss://proxy/Voicebot-deepgram/v1/speak"
_MIXED_LANGUAGE_MAP = {
    "en-GB": {"language": "en", "model": "flux-colin-en"},
    "fr-BE": {"language": "fr-fr", "model": "aura-2-agathe-fr"},
}


@pytest.fixture(autouse=True)
def _set_deepgram_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEEPGRAM_API_KEY_ENV_VAR, "test-key")


def _create_engine(
    language_map: dict,
    rasa_language: str,
    endpoint: str = _PROXY_V1_ENDPOINT,
    format: AudioFormat = MULAW_8KHZ,
) -> EndpointAwareDeepgramTTS:
    additional_languages = [
        language for language in language_map if language != rasa_language
    ]
    return EndpointAwareDeepgramTTS.from_config_dict(
        {
            "endpoint": endpoint,
            "language_map": language_map,
        },
        format=format,
        rasa_language=rasa_language,
        additional_languages=additional_languages,
    )


def test_default_aura_model_uses_v1_speak() -> None:
    engine = EndpointAwareDeepgramTTS("en", MULAW_8KHZ)

    assert "/v1/speak" in engine.get_websocket_url(engine.config)


def test_flux_model_rewrites_proxy_endpoint_to_v2() -> None:
    engine = _create_engine(
        {"en-GB": {"language": "en", "model": "flux-colin-en"}},
        "en-GB",
    )

    url = engine.get_websocket_url(engine.config)

    assert url.startswith("wss://proxy/Voicebot-deepgram/v2/speak?")
    assert "model=flux-colin-en" in url


def test_aura_model_rewrites_v2_endpoint_to_v1() -> None:
    engine = _create_engine(
        {"fr-BE": {"language": "fr-fr", "model": "aura-2-agathe-fr"}},
        "fr-BE",
        endpoint="wss://proxy/Voicebot-deepgram/v2/speak",
    )

    url = engine.get_websocket_url(engine.config)

    assert url.startswith("wss://proxy/Voicebot-deepgram/v1/speak?")
    assert "model=aura-2-agathe-fr" in url


def test_language_endpoint_overrides_model_based_rewrite() -> None:
    custom_endpoint = "wss://custom.example/tts"
    engine = _create_engine(
        {
            "en-GB": {
                "language": "en",
                "model": "flux-colin-en",
                "endpoint": custom_endpoint,
            }
        },
        "en-GB",
    )

    assert engine.get_websocket_url(engine.config).startswith(f"{custom_endpoint}?")


async def test_language_switch_reconnects_to_matching_endpoint() -> None:
    engine = _create_engine(_MIXED_LANGUAGE_MAP, "en-GB")
    assert "/v2/speak" in engine.get_websocket_url(engine.config)

    old_websocket = AsyncMock()
    old_websocket.closed = False
    engine.ws = old_websocket

    new_websocket = AsyncMock()
    new_websocket.closed = False
    session = MagicMock()
    session.closed = False
    session.ws_connect = AsyncMock(return_value=new_websocket)
    engine.session = session

    await engine.set_language("fr-BE")

    connected_url = session.ws_connect.await_args.args[0]
    assert "/v1/speak" in connected_url
    assert "model=aura-2-agathe-fr" in connected_url
    assert engine.ws is new_websocket


@pytest.mark.parametrize(
    "model, expected_message",
    [
        ("flux-colin-en", {"type": "Interrupt"}),
        ("aura-2-agathe-fr", {"type": "Clear"}),
    ],
)
async def test_interrupt_message_matches_model_family(
    model: str, expected_message: dict
) -> None:
    engine = _create_engine(
        {"language": {"model": model}},
        "language",
    )
    websocket = AsyncMock()
    websocket.closed = False
    engine.ws = websocket

    await engine.signal_interrupt()

    websocket.send_json.assert_awaited_once_with(expected_message)


@pytest.mark.parametrize(
    "control_message",
    ["Flushed", "Cleared", "SpeechMetadata", "SpeechInterrupted"],
)
async def test_stream_audio_stops_on_aura_and_flux_control_messages(
    control_message: str,
) -> None:
    engine = _create_engine(
        {"en-GB": {"language": "en", "model": "flux-colin-en"}},
        "en-GB",
    )
    websocket = _MockWebSocket(control_message)
    engine.ws = websocket

    chunks = [chunk async for chunk in engine.stream_audio()]

    assert len(chunks) == 1


class _MockWebSocket:
    def __init__(self, control_message: str) -> None:
        self.closed = False
        self._control_message = control_message

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        binary_message = MagicMock()
        binary_message.type = WSMsgType.BINARY
        binary_message.data = b"\x00\x01"
        yield binary_message

        control_message = MagicMock()
        control_message.type = WSMsgType.TEXT
        control_message.data = f'{{"type": "{self._control_message}"}}'.encode()
        yield control_message

        trailing_message = MagicMock()
        trailing_message.type = WSMsgType.BINARY
        trailing_message.data = b"\x02\x03"
        yield trailing_message
