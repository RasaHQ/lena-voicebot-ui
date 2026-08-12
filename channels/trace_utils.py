"""Fire-and-forget helper for sending tool-call trace events to the voice UI."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import aiohttp

TRACE_URL = os.environ.get(
    "RASA_TRACE_URL", "http://localhost:5005/webhooks/websockets/trace"
)

# Tool/action names to skip tracing (e.g. internal no-ops). Add your own.
_SKIP_TOOLS: set = {"wait_for_more_input"}

# Arg keys whose values are redacted before being sent to the browser.
# Matching is substring + case-insensitive (e.g. "user_password" matches "password").
_SENSITIVE_KEYS = {"password", "pin", "secret", "token", "ssn", "card", "cvv"}


def _sanitize(args: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: "***" if any(s in k.lower() for s in _SENSITIVE_KEYS) else v
        for k, v in (args or {}).items()
    }


async def post_tool_trace(
    recipient_id: Optional[str],
    tool_name: str,
    status: str,
    args: Optional[Dict[str, Any]] = None,
    duration_ms: Optional[int] = None,
    is_error: bool = False,
) -> None:
    """POST a tool-call trace event to the Rasa trace endpoint (fire-and-forget)."""
    if tool_name in _SKIP_TOOLS:
        return
    payload: Dict[str, Any] = {"recipient_id": recipient_id, "tool": tool_name, "status": status}
    if args is not None:
        payload["args"] = _sanitize(args)
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if is_error:
        payload["error"] = True
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                TRACE_URL, json=payload, timeout=aiohttp.ClientTimeout(total=1.0)
            )
    except Exception:
        pass
