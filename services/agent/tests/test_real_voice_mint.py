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

from agent.access import sign_cookie
from agent.models.config import load_models_config
from agent.realtime import OpenAIRealtimeMinter, SessionSummary
from agent.settings import Settings
from agent.voice import VoiceDeps

ROOT = Path(__file__).resolve().parents[3]
KEY = "k" * 32
SID = "019a7c2e-5b10-7000-8000-00000000a001"


@pytest.mark.real
async def test_minted_secret_connects_to_realtime() -> None:
    from agent.ingress import SessionRow
    from agent.main import create_app
    from test_voice_api import Sessions
    from voice_fakes import MemoryVoiceStore

    load_dotenv(ROOT / ".env", override=False)
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        pytest.fail("OPENAI_API_KEY missing: this test must not pass without making the calls")
    spec = load_models_config(ROOT / "data" / "models.yaml").roles["voice"]

    async def summary(_: str) -> SessionSummary:
        """What: a new session's summary. Use when: this test. Inputs: sid. Output: empty summary.
        Side effects: none. Role: fake thread read."""
        return SessionSummary()

    async with httpx.AsyncClient() as http:
        deps = VoiceDeps(store=MemoryVoiceStore(), minter=OpenAIRealtimeMinter(http, api_key), voice_spec=spec,
                         summary=summary)
        cfg = Settings(served_by="agent_a", agent_db_url="postgresql://x", redis_url="redis://x",
                       core_url="http://core:8000", knowledge_url="http://knowledge:8000", demo_faults_enabled=False,
                       site_origin="https://test", session_signing_key=KEY, demo_access_code="c")

        async def ok() -> str:
            """What: healthy probe. Use when: app build. Inputs: none. Output: ok. Side effects: none. Role: fake."""
            return "ok"

        app = create_app(cfg, probes={k: ok for k in ("redis", "postgres", "core", "knowledge")},
                         session_store=Sessions(SessionRow(session_id=SID, visitor_id="v")), voice=deps)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as c:
            c.cookies.set("qs_access", sign_cookie(KEY, visitor_id="v"))
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
