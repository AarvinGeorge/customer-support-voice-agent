"""What: the OpenAI Realtime session config (doc 07 s6.6 with the D-045 GA field names), the frozen realtime prompt
plus the code-built session summary, and the client secret minter (POST /v1/realtime/client_secrets, 5 s, 0 retries).
Use when: POST /api/sessions/{sid}/voice/token builds the session the browser connects to.
Inputs: the voice role of data/models.yaml, the session's LangGraph thread values, an httpx client and the
OpenAI API key.
Output: REALTIME_PROMPT, SessionSummary, summary_from_thread, render_instructions, build_session_config,
mint_body, OpenAIRealtimeMinter, MintedSecret, MintError.
Side effects: OpenAIRealtimeMinter.mint makes one HTTPS request; nothing else does I/O. The secret is never logged.
Role: the browser never sets instructions or tools; this module is the only place that does.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from contracts import limits
from contracts import redact as redaction
from contracts.tools import ASK_SUPPORT_AGENT_TOOL
from contracts.trust import escape_untrusted

from agent.models.config import RoleSpec

REALTIME_PROMPT = (Path(__file__).parent / "prompts" / "realtime.md").read_text(encoding="utf-8").strip()
REALTIME_PROMPT_SHA256 = "d8b24daa50d4cb68fc92481312d56356a973c9a14c8e31c65dafd46d882104ea"  # pinned by a test
CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
SUMMARY_MESSAGES = 8          # last 4 exchanges (doc 07 s6.6)
SUMMARY_TEXT_CHARS = 300      # each remembered line is cut to this length
AUDIO_FORMAT = {"type": "audio/pcm", "rate": 24000}
TURN_DETECTION = {"type": "semantic_vad", "eagerness": "auto", "interrupt_response": True, "create_response": True}
_CUSTOMER_RE = re.compile(r"<customer_message>\n?(.*?)\n?</customer_message>", re.DOTALL)
_SUMMARY_TAG_RE = re.compile(r"<(?=\s*/?\s*session_summary)", re.IGNORECASE)


@dataclass(frozen=True)
class SessionSummary:
    """What: the code-built facts the realtime model starts with. Use when: render_instructions.
    Inputs: verified display name (None: not verified), open read-back text, recent (speaker, text) pairs.
    Output: value. Side effects: none. Role: doc 07 s6.6 session summary."""

    verified_name: str | None = None
    readback_say: str | None = None
    exchanges: list[tuple[str, str]] = field(default_factory=list)


def _text_of(content: Any) -> str:
    """What: plain text of a message content (string or content blocks). Use when: summary_from_thread.
    Inputs: content. Output: text. Side effects: none. Role: helper."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def summary_from_thread(values: dict[str, Any] | None,
                        context: redaction.RedactContext | None = None) -> SessionSummary:
    """What: build the summary from the checkpointed SupportState values. Only what doc 07 s6.6 lists leaves:
    the verified name, the pending read-back, and the customer's own words (the <customer_message> block of a
    human message; human messages without one, such as merged <voice_transcript> blocks, are skipped, and no
    <account_context> or <turn_context> ever leaves) plus agent replies. Every line goes through contracts redact
    (emails masked, the s11.1 PIN scanner once CS-E-03.S7.T9 adds it).
    Use when: voice/token. Inputs: thread channel values or None for a new session; the session's RedactContext.
    Output: SessionSummary: verified name from claims_snapshot (or a neutral label), the last agent message as
    the read-back while state.pending is set, the last 8 customer and agent text messages.
    Side effects: none. Role: core stays the authority; this is display context for speech only (review PR #2 I2)."""
    ctx = context or redaction.RedactContext()
    if not values:
        return SessionSummary()
    name = None
    if values.get("verified"):
        name = (values.get("claims_snapshot") or {}).get("display_name") or "the verified customer"
    lines: list[tuple[str, str]] = []
    for m in values.get("messages") or []:
        kind = getattr(m, "type", None)
        if kind == "ai" and not getattr(m, "tool_calls", None):
            text = _text_of(m.content).strip()
            speaker = "agent"
        elif kind == "human":
            found = _CUSTOMER_RE.search(_text_of(m.content))
            if not found:
                continue
            text, speaker = found.group(1).strip(), "customer"
        else:
            continue
        if text:
            lines.append((speaker, redaction.redact(text, ctx)))
    readback = None
    if values.get("pending"):
        readback = next((t for s, t in reversed(lines) if s == "agent"), None)
    return SessionSummary(verified_name=name, readback_say=readback, exchanges=lines[-SUMMARY_MESSAGES:])


def _clean(text: str) -> str:
    """What: escape untrusted wrapper tags and the summary tag, collapse whitespace, cut to length.
    Use when: every string placed in the summary. Inputs: text. Output: text. Side effects: none. Role: helper."""
    one_line = " ".join(text.split())[:SUMMARY_TEXT_CHARS]
    return _SUMMARY_TAG_RE.sub("&lt;", escape_untrusted(one_line))


def render_instructions(summary: SessionSummary) -> str:
    """What: the frozen prompt followed by a <session_summary> block. Use when: voice/token.
    Inputs: SessionSummary. Output: instructions string. Side effects: none.
    Role: doc 07 s6.6 instructions (frozen text, no policy facts, plus the code-built summary)."""
    lines = [f"verified: {_clean(summary.verified_name) if summary.verified_name else 'not verified'}",
             f"pending_readback: {_clean(summary.readback_say) if summary.readback_say else 'none'}",
             "recent_exchanges:" if summary.exchanges else "recent_exchanges: none"]
    lines += [f"{who}: {_clean(text)}" for who, text in summary.exchanges[-SUMMARY_MESSAGES:]]
    return REALTIME_PROMPT + "\n\n<session_summary>\n" + "\n".join(lines) + "\n</session_summary>"


def build_session_config(spec: RoleSpec, instructions: str) -> dict[str, Any]:
    """What: the Realtime `session` object (GA names per D-045: audio.input.transcription,
    audio.input.turn_detection, audio.output.voice). Use when: mint_body.
    Inputs: the voice RoleSpec (model, voice, transcription_model), instructions.
    Output: dict. Side effects: none. Role: doc 07 s6.6 RealtimeSessionConfig."""
    if not spec.voice or not spec.transcription_model:
        raise ValueError("models.yaml roles.voice needs voice and transcription_model")
    return {
        "type": "realtime", "model": spec.model, "instructions": instructions,
        "audio": {
            "input": {"format": dict(AUDIO_FORMAT), "transcription": {"model": spec.transcription_model},
                      "turn_detection": dict(TURN_DETECTION)},
            "output": {"format": dict(AUDIO_FORMAT), "voice": spec.voice},
        },
        "tools": [ASK_SUPPORT_AGENT_TOOL], "tool_choice": "auto",
    }


def mint_body(session: dict[str, Any]) -> dict[str, Any]:
    """What: the client_secrets request body with a 600 s expiry. Use when: OpenAIRealtimeMinter.mint.
    Inputs: session config. Output: dict. Side effects: none. Role: doc 07 s6.6 `expires: 600 s`."""
    return {"expires_after": {"anchor": "created_at", "seconds": limits.VOICE_SESSION_MAX_S}, "session": session}


@dataclass(frozen=True)
class MintedSecret:
    """What: an ephemeral Realtime client secret and its expiry. Use when: building VoiceToken.
    Inputs: value (ek_...), expires_at. Output: value; repr hides the secret. Side effects: none. Role: result."""

    value: str = field(repr=False)
    expires_at: datetime


class MintError(Exception):
    """What: the mint failed (timeout, provider error, missing key). Use when: voice/token answers 503.
    Inputs: error class (doc 07 s1.3 provider_*), HTTP status or None. Output: exception. Side effects: none.
    Role: carries no provider body (it could echo request content)."""

    def __init__(self, class_: str, status: int | None = None) -> None:
        """What: keep the class and status. Use when: raised by mint. Inputs: class, status. Output: none.
        Side effects: none. Role: constructor."""
        super().__init__(f"{class_} status={status}")
        self.class_, self.status = class_, status


class OpenAIRealtimeMinter:
    """What: POST /v1/realtime/client_secrets with the standard API key, 5 s, no retry.
    Use when: production voice/token (tests pass an httpx MockTransport client or a fake minter).
    Inputs: httpx.AsyncClient, OpenAI API key. Output: mint(). Side effects: one HTTPS call per mint.
    Role: provider adapter (doc 07 s8.1 "Voice mint to OpenAI 5 s, 0 retries")."""

    def __init__(self, http: httpx.AsyncClient, api_key: str) -> None:
        """What: keep the client and key. Use when: app start. Inputs: client, key. Output: none.
        Side effects: none. Role: constructor."""
        self.http, self._key = http, api_key

    async def mint(self, session: dict[str, Any]) -> MintedSecret:
        """What: mint one client secret. Use when: voice/token. Inputs: session config.
        Output: MintedSecret. Side effects: one request. Role: raises MintError on any failure."""
        if not self._key:
            raise MintError("provider_auth")
        try:
            resp = await self.http.post(CLIENT_SECRETS_URL, json=mint_body(session),
                                        headers={"Authorization": f"Bearer {self._key}"},
                                        timeout=limits.VOICE_MINT_TIMEOUT_S)
        except httpx.TimeoutException as exc:
            raise MintError("provider_timeout") from exc
        except httpx.HTTPError as exc:
            raise MintError("provider_5xx") from exc
        if resp.status_code != 200:
            klass = {401: "provider_auth", 403: "provider_auth", 429: "provider_rate_limited"}.get(
                resp.status_code, "provider_5xx")
            raise MintError(klass, resp.status_code)
        data = resp.json()
        value, expires = data.get("value"), data.get("expires_at")
        if not isinstance(value, str) or not isinstance(expires, int):
            raise MintError("provider_5xx", resp.status_code)
        return MintedSecret(value=value, expires_at=datetime.fromtimestamp(expires, UTC))
