# AGENTS.md — wiring the voice orb into a Rasa Pro bot

Instructions for an agent (or developer) integrating this voice orb into an
**existing Rasa Pro bot**. Read this before touching the bot. The human-facing
walkthrough is in [`README.md`](./README.md); this file is the operational
checklist plus the gotchas that actually break integrations.

## What this repo is

A self-contained voice UI ("the orb") plus the Rasa voice channel it needs:

| Path | Role | Goes where |
| ---- | ---- | ---------- |
| `orb.html` | The whole UI (WebGL orb, transcript, debug panel, pipeline timeline). No build step. | Served to the browser. Stays here or wherever you host static files. |
| `vendor/ogl.umd.js` | Vendored WebGL lib. Must sit next to `orb.html` (offline support). | Ships with `orb.html`. |
| `channels/websockets.py` | Custom Rasa voice channel. Streams audio + transcript + skill + trace events. | **Copied into the target bot.** |
| `channels/trace_utils.py` | *Optional* helper for the tool-call/API timeline lane. Needs `aiohttp`. | Copied into the bot only if you want the API lane. |
| `credentials.websockets.yml` | Paste-in `credentials.yml` block. | Merged into the bot's `credentials.yml`. |

The orb ↔ bot link is **one WebSocket** at
`ws://<host>:<port>/webhooks/websockets/websocket` (default `localhost:5005`).
It is **not** the built-in `browser_audio` channel — the transcript, skill
labels, and tool-call trace only exist because of `channels/websockets.py`. Do
not "simplify" by switching to `browser_audio`; you will silently lose those
features.

## Integration procedure

Do these in the **target bot's** repository, not this one.

1. **Place the channel file so it is importable.** The class must resolve at the
   dotted path used as the `credentials.yml` key. The default assumes a
   `channels` package at the bot root:
   ```bash
   mkdir -p channels
   cp <this-repo>/channels/websockets.py channels/websockets.py
   [ -f channels/__init__.py ] || touch channels/__init__.py   # make it a package
   ```
   → import path becomes `channels.websockets.WebSocketsInputChannel`.
   If you put the file elsewhere, the `credentials.yml` key **must** match that
   path exactly. `websockets.py` imports only `sanic`, `structlog`, `rasa`, and
   the stdlib — all of which Rasa Pro already provides. Do not add dependencies.

2. **Register the channel** by merging the block from
   `credentials.websockets.yml` into the bot's `credentials.yml`. Preserve the
   bot's existing channel entries; only add this one.

   **ASR/TTS is project-specific — do not assume Deepgram.** This channel uses
   the same `voice_stream` stack as Rasa Inspector's voice mode, so the bot
   already has a working `asr:`/`tts:` config if voice works in Inspector.
   **Reuse it:** copy the `asr:`/`tts:` blocks the bot already uses for voice
   (e.g. under the `inspector:` entry, or its current voice channel) into this
   block, and ensure that provider's API key is set. The Deepgram config in the
   file is only an example. Installed engines: ASR = deepgram, azure; TTS =
   deepgram, azure, cartesia, rime. AudioCodes is a *channel*, not a pluggable
   ASR/TTS engine — it cannot be named here.

3. **Start the bot** and confirm the channel registered:
   ```bash
   rasa run --enable-api
   ```
   The startup logs should list the `websockets` channel. Sanity-check the
   endpoint (should return `{"status": "ok"}`):
   ```bash
   curl -s http://localhost:5005/webhooks/websockets/
   ```

4. **Serve the orb over `http://localhost`** (microphone access needs a secure
   context — a bare `file://` path is unreliable):
   ```bash
   # from this repo
   python3 -m http.server 8080
   ```
   Open `http://localhost:8080/orb.html`, click **Connect**, allow the mic, and
   speak. Expect: orb turns blue (listening) → purple (thinking) → green
   (speaking), and text appears in the Transcript panel.

## Verifying it actually works

Do not report success until you have observed a real round trip. Checklist:

- Browser console has **no errors** (a missing `favicon.ico` 404 is harmless).
- After Connect, the orb enters **listening** and the mic prompt appeared.
- Speaking produces a **user transcript**; the bot replies with **audio + a bot
  transcript** and the orb goes **green** while it talks.
- The **Debug** panel shows `WS: OPEN`, `AudioCtx: running`, and rising RX/TX
  counts. The **pipeline timeline** draws bars per turn.

If you cannot drive a mic (e.g. headless), at minimum confirm the WebSocket URL
resolves to `.../webhooks/websockets/websocket` and the endpoint health check
above returns ok — and say clearly that the end-to-end audio path was not
exercised.

## Configuration (no file edits needed)

The orb reads URL query params; override without editing `orb.html`:

| Param | Default | Notes |
| ----- | ------- | ----- |
| `host` / `port` | page host / `5005` | Rasa server location. |
| `ws` | — | Full WS URL; overrides host/port/channel. Use for TLS/remote: `wss://…`. |
| `channel` | `websockets` | The path segment; leave as-is for this channel. |
| `title` | `Voice Assistant` | Heading above the orb. |
| `lang` | `en-US` | Sent to the bot on connect. |
| `rate` | `48000` | Must equal the bot channel's `sample_rate`. |

To change defaults permanently, edit the `CONFIG` block at the top of the
`<script>` in `orb.html`.

## Common failure modes

| Symptom | Cause / fix |
| ------- | ----------- |
| `ModuleNotFoundError` on bot start | Dotted path in `credentials.yml` ≠ where `websockets.py` lives, or missing `channels/__init__.py`. |
| "WS error — is the bot running?" | Bot down, wrong port, or channel not registered. Re-check step 3. |
| Mic never prompts | Page served from `file://`. Serve over `http://localhost`. |
| Connects but bot never replies | Missing/invalid ASR or TTS API key. Check bot logs. |
| Transcript/skill labels never appear | You're pointed at `browser_audio`, not `websockets`. Those events only come from this custom channel. |
| Bot audio garbled / chipmunky | `rate` ≠ bot channel `sample_rate`. Match them (console logs a warning). |
| API lane of timeline stays empty | Expected. It only fills if bot actions call `trace_utils.post_tool_trace` (see below). |

## Optional: the API / tool-call timeline lane

Populated only if the bot's custom actions report tool calls to the channel's
`/trace` endpoint. To enable:

1. Copy `channels/trace_utils.py` into the bot (adds an `aiohttp` dependency).
2. Wrap tool calls in a custom action:
   ```python
   from trace_utils import post_tool_trace
   await post_tool_trace(recipient_id, "get_orders", "start", args={...})
   # ... do the work ...
   await post_tool_trace(recipient_id, "get_orders", "end", duration_ms=42)
   ```
3. Set `RASA_TRACE_URL` if the bot isn't at `http://localhost:5005`.

Sensitive arg keys (`password`, `pin`, `secret`, …) are redacted automatically.
Everything else in the UI works without this.

## Boundaries — do not change without being asked

- **Don't alter the wire protocol** in `orb.html` or `channels/websockets.py`
  independently — the client and channel are matched. The message contract is
  documented in `README.md` under "How it connects".
- **Don't swap to `browser_audio`** to reduce setup; it drops transcript/skill/
  trace. That trade-off was already decided.
- The **Rasa logo** (inline SVG in `orb.html`, top-right) and the **color
  palette** (`COLORS` / `GLOW_CSS` objects; the look is driven by the GLSL
  `FRAG` shader) are safe to rebrand. The tool-label map (`_TOOL_LABELS`) is
  cosmetic and falls back to raw tool names.
