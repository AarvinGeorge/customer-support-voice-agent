"""What: ONE real mint through POST /voice/token and ONE WebSocket connect with the minted secret
(CS-E-05.S3.T2 Done when "a minted secret connects").
Use when: `uv run pytest -m real -s tests/test_real_voice_mint.py` only; deselected otherwise. Exactly 2 real OpenAI
calls (client_secrets, then one Realtime WebSocket session closed after session.created).
Inputs: OPENAI_API_KEY from the worktree .env (never printed); data/models.yaml voice role.
Output: pass when session.created echoes our model, voice, transcription model, semantic_vad and the single
ask_support_agent tool. Prints only those echoed fields, never the secret.
Side effects: two paid OpenAI requests (no audio sent, no response created).
Role: integration evidence that the GA session config (D-045) is accepted end to end.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import websockets
from dotenv import load_dotenv

from agent.models.config import load_models_config
from agent.realtime import OpenAIRealtimeMinter

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.real
async def test_minted_secret_connects_to_realtime() -> None:
    from test_voice_api import SID, World

    load_dotenv(ROOT / ".env", override=False)
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        pytest.fail("OPENAI_API_KEY missing: this test must not pass without making the calls")
    spec = load_models_config(ROOT / "data" / "models.yaml").roles["voice"]
    async with httpx.AsyncClient() as http:
        w = World(minter=OpenAIRealtimeMinter(http, api_key))  # fake stores and agent, real minter
        async with w.client() as c:
            r = await c.post(f"/api/sessions/{SID}/voice/token", json={})  # real call 1: client_secrets
    assert r.status_code == 200, r.status_code
    tok = r.json()
    secret = tok["client_secret"]
    assert secret.startswith("ek_") and tok["max_session_s"] == 600
    url = f"wss://api.openai.com/v1/realtime?model={tok['model']}"
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {secret}"},
                                  open_timeout=10) as ws:  # real call 2: one Realtime session
        first = json.loads(await ws.recv())
    s = first.get("session", {})
    audio_in = s.get("audio", {}).get("input", {})
    seen = {"type": first.get("type"), "model": s.get("model"), "voice": s.get("audio", {}).get("output", {}).get("voice"),
            "transcription": (audio_in.get("transcription") or {}).get("model"),
            "turn_detection": (audio_in.get("turn_detection") or {}).get("type"),
            "tools": [t.get("name") for t in s.get("tools", [])],
            "instructions_start": (s.get("instructions") or "")[:40]}
    print("session.created echo:", json.dumps(seen))
    assert seen == {"type": "session.created", "model": spec.model, "voice": spec.voice,
                    "transcription": spec.transcription_model, "turn_detection": "semantic_vad",
                    "tools": ["ask_support_agent"], "instructions_start": seen["instructions_start"]}
    assert seen["instructions_start"].startswith("You are the voice of Quillstack")
