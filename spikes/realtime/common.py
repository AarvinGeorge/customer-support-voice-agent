"""Shared helpers for the throwaway Realtime spike (CS-E-05.S3.T1).

What: reads the OpenAI key from .env without printing it, builds the
    RealtimeSessionConfig of doc 07 s6.6, mints an ephemeral client secret.
Use when: running spikes/realtime/harness_ws.py or webrtc_sdp.py.
Inputs: the repo-root .env (OPENAI_API_KEY), constants below.
Output: helper functions; no module-level side effects.
Side effects: mint_client_secret makes one real HTTPS call to OpenAI.
Role: throwaway spike code, not imported by any service.
"""

from __future__ import annotations

import json
import pathlib
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]

REALTIME_MODEL = "gpt-realtime-2.1"
TRANSCRIPTION_MODEL = "gpt-transcribe"
VOICE = "marin"

INSTRUCTIONS = (
    "You are the Quillstack voice front end. Call ask_support_agent for anything about "
    "Quillstack, the account, policies or actions. Read the tool's say field verbatim. "
    "Never state a policy, price, amount or date yourself. While waiting, say one short "
    "holding phrase."
)

ASK_SUPPORT_AGENT = {
    "type": "function",
    "name": "ask_support_agent",
    "description": (
        "Ask the Quillstack support agent. Use for anything about Quillstack, the customer's "
        "account, billing, policies, tickets or actions, and whenever the customer answers a "
        "question the agent asked (yes, no, account ID, PIN)."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["question"],
        "properties": {
            "question": {
                "type": "string",
                "maxLength": 2000,
                "description": "What the customer said or asked, in their words",
            }
        },
    },
}


def openai_key() -> str:
    """Return OPENAI_API_KEY from the repo .env.

    What: parses .env for one key; never prints it.
    Use when: any spike call needs auth.
    Inputs: none (reads ROOT/.env).
    Output: the key string; raises SystemExit if missing.
    Side effects: file read.
    Role: secret access for the spike.
    """
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("OPENAI_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    raise SystemExit("OPENAI_API_KEY missing from .env")


def session_config() -> dict:
    """Build the GA session object for client_secrets (doc 07 s6.6 mapped to GA field names).

    What: model, voice, instructions, transcription, semantic_vad, the one tool.
    Use when: minting a client secret.
    Inputs: module constants.
    Output: dict for the `session` field.
    Side effects: none.
    Role: the shape lane C T2 will build from models.yaml.
    """
    return {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "instructions": INSTRUCTIONS,
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": TRANSCRIPTION_MODEL},
                "turn_detection": {
                    "type": "semantic_vad",
                    "eagerness": "auto",
                    "interrupt_response": True,
                    "create_response": True,
                },
            },
            "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": VOICE},
        },
        "tools": [ASK_SUPPORT_AGENT],
        "tool_choice": "auto",
    }


def post_json(url: str, body: dict, key: str, timeout: float = 15) -> dict:
    """POST JSON with bearer auth and return the parsed reply.

    What: tiny urllib wrapper that surfaces the error body on HTTP errors.
    Use when: mint or TTS-free JSON calls.
    Inputs: url, body, key, timeout seconds.
    Output: parsed JSON dict.
    Side effects: one network call.
    Role: transport helper.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        raise SystemExit(f"HTTP {err.code} from {url}: {err.read().decode()[:800]}")


def mint_client_secret(key: str) -> tuple[str, dict]:
    """Mint an ephemeral client secret with the full session config.

    What: POST /v1/realtime/client_secrets with expires_after 600 s.
    Use when: starting a WebRTC or ephemeral-key WebSocket session.
    Inputs: the standard API key.
    Output: (secret value, reply without the value) so the reply can be logged safely.
    Side effects: one real OpenAI call.
    Role: mirrors the future agent `voice/token` endpoint.
    """
    reply = post_json(
        "https://api.openai.com/v1/realtime/client_secrets",
        {"expires_after": {"anchor": "created_at", "seconds": 600}, "session": session_config()},
        key,
    )
    secret = reply.pop("value")
    return secret, reply
