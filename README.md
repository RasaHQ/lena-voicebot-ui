# Drop-in Voice Orb for Rasa Pro

A single-file, animated voice orb UI that talks to a **Rasa Pro** voice bot over
a WebSocket — with a live transcript, active-skill labels, and a real-time
pipeline debug panel. Point it at a bot on the same machine and it works; no
build step, no framework.

The orb reacts to the conversation in real time:

| State         | Meaning                                    | Colour  |
| ------------- | ------------------------------------------ | ------- |
| **Listening** | Mic is open; orb pulses with your voice    | Blue    |
| **Thinking**  | You've stopped; the bot is generating      | Purple  |
| **Speaking**  | Bot audio is playing; orb pulses with it   | Green   |
| **Idle**      | Not connected                              | Dim blue|

This is the **full-featured** build. It targets a **custom `websockets` voice
channel** (included in `channels/`) that streams the extra events the transcript
and debug panels need. That means the customer copies one Python file into their
bot and registers it in `credentials.yml`.

**Requires Rasa Pro 3.17+.** The channel uses the 3.17 `VoiceInputChannel` API
(`conversation_blueprint`, `language_map`, conversation-queue ASR events).

## What you get

- **Animated WebGL orb** driven by live mic/bot audio levels.
- **Live transcript** — user speech (from ASR) and bot replies, side by side.
- **Active-skill labels** — shows which skill/flow is handling the turn.
- **Debug panel** (toggle) — WebSocket & AudioContext health, message counts,
  audio-queue lag, per-session metrics, and a downloadable debug report.
- **Pipeline timeline** — a live Gantt chart of the MIC → ASR → LLM → API → TTS
  → OUT stages per turn, including barge-in markers.

---

## Files

| File                          | Purpose                                                            |
| ----------------------------- | ------------------------------------------------------------------ |
| `orb.html`                    | The entire UI. Open it in a browser.                               |
| `vendor/ogl.umd.js`           | Vendored WebGL library so the orb works fully offline.             |
| `channels/websockets.py`       | The custom Rasa voice channel. **Copy into your bot.**             |
| `channels/deepgram_tts.py`     | Custom Deepgram TTS routing Aura to v1 and Flux to v2.             |
| `channels/trace_utils.py`      | *Optional* helper so bot actions can feed the API/tool-call lane.  |
| `credentials.websockets.yml`  | Paste-in channel config for your bot's `credentials.yml`.          |

---

## Setup

### 1. Add the custom channel to your bot

Copy the channel file into your bot so it is importable, e.g.:

```bash
cp channels/websockets.py  <your-bot>/channels/websockets.py
cp channels/deepgram_tts.py <your-bot>/channels/deepgram_tts.py
touch <your-bot>/channels/__init__.py      # if channels/ isn't already a package
```

The Deepgram component uses the same dependencies and `DEEPGRAM_API_KEY` as
Rasa Pro's built-in Deepgram TTS engine.

### 2. Register it in credentials.yml

Copy the block from
[`credentials.websockets.yml`](./credentials.websockets.yml) into your bot's
`credentials.yml`. The key is the dotted import path to the class — adjust it if
you placed the file somewhere other than `channels/websockets.py`.

**Speech provider is project-specific — reuse what your bot already uses.** This
channel runs on the same `voice_stream` stack as Rasa Inspector's voice mode, so
if you can talk to your bot in Inspector today, that ASR/TTS config already
works. Copy your bot's existing `asr:` / `tts:` blocks (e.g. the ones under the
`inspector:` entry, or whatever voice channel you use) into this block, and set
that provider's API key. The Deepgram config in the file is only an example;
Azure and others work too (installed engines: ASR = deepgram, azure; TTS =
deepgram, azure, cartesia, rime).

For a bot that mixes Deepgram Flux and Aura voices, select the custom engine by
its dotted import path. A `flux-*` model automatically changes the configured
`/v1/speak` path to `/v2/speak`; Aura models use `/v1/speak`. An optional
per-language `endpoint` takes precedence when a proxy uses different routes:

```yaml
tts:
  name: channels.deepgram_tts.EndpointAwareDeepgramTTS
  endpoint: wss://proxy/Voicebot-deepgram/v1/speak
  language_map:
    en-GB:
      language: en
      model: flux-colin-en
    fr-BE:
      language: fr-fr
      model: aura-2-agathe-fr
    nl-BE:
      language: nl-nl
      model: aura-2-daphne-nl
      # endpoint: wss://another-proxy/deepgram/v1/speak
```

**Use `language_map` for ASR/TTS** (Rasa Pro 3.17). Keys must match your bot's
`language` / `additional_languages` in `config.yml` (e.g. `en` or `en-US`).
The orb's `lang` query param must be one of those keys.

**`sample_rate`** (same as Inspector): `8000`, `24000`, or `48000` (default).
The orb `?rate=` must match.

**Barge-in** uses the standard `interruptions:` block (`enabled`, `min_words`) —
same as Inspector and other voice channels. Do not add a `cfm:` block; it is
not part of the 3.17 channel API.

### 3. Run your bot

```bash
rasa run --enable-api
```

This serves on `http://localhost:5005`, exposing the channel at
`ws://localhost:5005/webhooks/websockets/websocket`.

### 4. Open the orb

The microphone requires a **secure context**, so serve the page over
`http://localhost`:

```bash
# from this directory
python3 -m http.server 8080
```

Open **<http://localhost:8080/orb.html>** and click **Connect**. Use the
**Transcript** and **Debug** buttons to reveal those panels.

> Opening `orb.html` via `file://` also works in Chrome/Firefox, but serving
> over `localhost` is the most reliable across browsers.

---

## Configuration

Configurable via URL query parameters — no file editing needed:

| Param      | Default          | Description                                                        |
| ---------- | ---------------- | ------------------------------------------------------------------ |
| `host`     | page's hostname  | Rasa server host.                                                  |
| `port`     | `5005`           | Rasa server port.                                                  |
| `channel`  | `websockets`     | Voice channel name in the WebSocket path.                          |
| `projectUrl` | —              | Base URL of the bot **including any reverse-proxy path prefix**. Same as Inspector's `projectUrl`. Overrides `host`/`port`. See below. |
| `ws`       | —                | Full WebSocket URL. Overrides `projectUrl`/`host`/`port`/`channel` when set. |
| `title`    | `Voice Assistant`| Text shown above the orb.                                          |
| `lang`     | `en-US`          | Language key sent on connect; must match a `language_map` entry.   |
| `rate`     | `48000`          | Must match credentials `sample_rate` (`8000`, `24000`, or `48000`). |

> **Sample rate note:** Set `sample_rate` on the channel in `credentials.yml`
> (same as Inspector). The orb wire format is always Linear-16 PCM; at
> `8000` the channel converts to/from μ-law for ASR/TTS. Keep
> `orb.html?rate=` equal to credentials `sample_rate`.

Examples:

```
orb.html?title=Acme%20Support
orb.html?host=192.168.1.50&port=5005
orb.html?ws=wss://voice.example.com/webhooks/websockets/websocket
```

To change defaults permanently, edit the `CONFIG` block near the top of the
`<script>` in `orb.html`.

### Running behind a reverse proxy (path prefix)

By default the orb targets **port 5005 on the page's hostname**, which is right
for local development but wrong on hosting platforms (Kubernetes ingress,
nginx, etc.) where the bot is reached on the page's *own* port under a
**path prefix** — there is no separate `:5005`. Symptom:

```
WebSocket connection to 'wss://my-host.example.com:5005/webhooks/websockets/websocket' failed
```

Use `projectUrl` for this — same name and meaning as Rasa Inspector. It is the
bot's base URL, prefix included. It may be relative to the page or absolute.

| Value                                        | Resulting WebSocket URL                                     |
| -------------------------------------------- | ----------------------------------------------------------- |
| `?projectUrl=.`                              | same origin + same directory as `orb.html` — usually correct |
| `?projectUrl=/my-prefix`                     | `wss://<page-host>/my-prefix/webhooks/websockets/websocket`  |
| `?projectUrl=https://my-host/my-prefix`      | `wss://my-host/my-prefix/webhooks/websockets/websocket`      |

`?projectUrl=.` is the one to try first: it reuses the page's scheme, host, port,
and directory, so it works on any path-prefix proxy without knowing the prefix.

Two things to confirm on the hosting side:

1. The **Rasa server is proxied under the same prefix** that serves `orb.html`.
   If the bot sits on a different route, pass that route explicitly
   (`?projectUrl=/bot-route`) or give the full URL with `?ws=`.
2. The proxy **forwards WebSocket upgrades** (the `Upgrade` and `Connection`
   headers). If the URL is right but the socket still fails to open, this is
   usually the cause.

Open the **Debug** panel and use **Download** to check the exact URL the orb
used — it is printed as `WS URL` at the top of the report.

---

## Optional: the tool-call / API timeline lane

The pipeline timeline's **MIC / ASR / LLM / TTS / OUT** lanes are populated
automatically from channel messages. The **API** lane (custom tool/action calls)
only lights up if your bot's actions report them to the channel's `/trace`
endpoint. To enable it:

1. Copy `channels/trace_utils.py` into your bot (needs `aiohttp`).
2. From a custom action, call it around your tool call:

   ```python
   from trace_utils import post_tool_trace

   await post_tool_trace(recipient_id, "get_orders", "start", args={...})
   # ... do the work ...
   await post_tool_trace(recipient_id, "get_orders", "end", duration_ms=42)
   ```

3. Set `RASA_TRACE_URL` if your bot isn't at `http://localhost:5005`.

Sensitive arg keys (`password`, `pin`, `secret`, …) are redacted automatically.
Everything else in the UI works without this.

---

## How it connects (for reference)

The orb speaks the custom `websockets` wire protocol over one WebSocket:

**Client → server**
- `{ "sample_rate": 48000, "language": "en-US" }` — sent once on connect; `sample_rate` must match credentials (channel is authoritative)
- `{ "audio": "<base64 Int16 PCM>" }` — microphone audio, streamed continuously
- `{ "marker": "<id>" }` — echoed once buffered bot audio finishes playing

**Server → client**
- `{ "audio": "<base64 Int16 PCM>" }` — bot speech to play
- `{ "marker": "<id>", "final": true, "latency": {...} }` — audio boundary markers
- `{ "response": "..." }` / `{ "response_chunk": "..." }` — bot text for transcript
- `{ "trace_event": "transcript", "text": "..." }` — user transcript
- `{ "trace_event": "tool_call", ... }` — tool-call events (API lane)
- `{ "skill": "..." }` — active skill/flow label
- `{ "interruptPlayback": true }` — barge-in; stop playing immediately
- `{ "hangup": true }` — end the session

---

## Troubleshooting

| Symptom                              | Fix                                                                                 |
| ------------------------------------ | ----------------------------------------------------------------------------------- |
| "WS error — is the bot running?"     | Confirm the bot is up and `channels.websockets.WebSocketsInputChannel` is in `credentials.yml`. |
| WS URL has a wrong `:5005` / wrong path | You're behind a reverse proxy. Add `?projectUrl=.` (see "Running behind a reverse proxy" above). |
| `ModuleNotFoundError` on bot start   | The dotted path in `credentials.yml` must match where you put `websockets.py`.      |
| Mic permission never prompts         | Serve over `http://localhost` (step 4), not a bare `file://` path.                  |
| Transcript/skill labels never appear | Those come from the custom channel — make sure you're on `/webhooks/websockets/`, not the built-in `browser_audio`. |
| API lane in timeline stays empty     | Expected unless you wire up `trace_utils.py` (see above).                           |
| Bot audio garbled / chipmunky        | Credentials `sample_rate` ≠ orb `?rate=`. Match them (channel logs a warning). Supported: `8000`, `24000`, `48000`. |
| `TypeError` / channel init failure   | Confirm you are on Rasa Pro **3.17+** and using this branch's `websockets.py`. |
| Language ignored / ASR wrong language | Orb `lang` is not a key in ASR/TTS `language_map`. Align `lang`, `config.yml`, and credentials. |
| Barge-in never fires                 | Set `interruptions.enabled: true` (and optionally `min_words`) in the channel credentials. |

---

## License

Apache-2.0 — see [`LICENSE`](./LICENSE) and [`NOTICE`](./NOTICE). The bundled
`vendor/ogl.umd.js` is MIT-licensed (OGL).
