"""Realtime event-sequence harness over WebSocket (spikes 10 and 11, CS-E-05.S3.T1).

What: speaks a synthesized question into a Realtime session minted with the doc 07
    s6.6 config, answers the ask_support_agent call, then barges in while the reply
    plays, and logs every server event with a monotonic timestamp.
Use when: re-measuring function call completion, transcript timing, interruption.
Inputs: argv[1] run label (default "run1"); .env OPENAI_API_KEY; cached TTS audio in
    spikes/realtime/audio/ (made on first run).
Output: spikes/realtime/runs/<label>.jsonl plus a printed timing summary.
Side effects: real OpenAI calls (TTS on first run only, one mint, one Realtime session).
Role: throwaway spike; findings live in SPIKE_NOTE.md.

Run: uv run --no-project --with websockets python spikes/realtime/harness_ws.py run1
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import sys
import time
import urllib.request

import websockets

from common import REALTIME_MODEL, mint_client_secret, openai_key

HERE = pathlib.Path(__file__).resolve().parent
AUDIO = HERE / "audio"
RATE = 24000
CHUNK_MS = 100
CHUNK_BYTES = RATE * 2 * CHUNK_MS // 1000
QUESTION = "Hi, what is your refund policy if I cancel my annual plan?"
BARGE_IN = "Wait, sorry, can you stop for a second?"
SAY = ("Annual plans can be refunded in full within 30 days of purchase. After that, you keep "
       "access until the end of the term, and no further renewals are charged. If you paid by "
       "invoice, the credit goes back to the original payment method within five to ten "
       "business days. Would you like me to start a cancellation request for your workspace?")


def tts_pcm(key: str, text: str, path: pathlib.Path) -> bytes:
    """Synthesize text to raw 24 kHz PCM16 mono, cached on disk.

    What: POST /v1/audio/speech with response_format pcm.
    Use when: the harness needs spoken input without a microphone.
    Inputs: key, text, cache path.
    Output: PCM bytes.
    Side effects: one TTS call if the cache file is missing; writes the cache.
    Role: synthetic microphone.
    """
    if path.exists():
        return path.read_bytes()
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps(
            {"model": "gpt-4o-mini-tts", "voice": "alloy", "input": text, "response_format": "pcm"}
        ).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return data


def summarize(event: dict) -> dict:
    """Drop bulky audio payloads from an event before logging.

    What: replaces base64 audio deltas with their length.
    Use when: writing the jsonl log.
    Inputs: a server event dict.
    Output: a smaller dict.
    Side effects: none.
    Role: keeps the committed log readable.
    """
    out = dict(event)
    if out.get("type") == "response.output_audio.delta":
        out["delta"] = f"<{len(out.get('delta', ''))} b64 chars>"
    return out


async def run(label: str) -> None:
    """Drive one session end to end and write the event log.

    What: mint, connect with the ephemeral key, stream question audio, relay the tool
        call, barge in on the first reply audio, stop after the post-barge-in turn.
    Use when: main entry.
    Inputs: run label.
    Output: none (writes runs/<label>.jsonl, prints summary).
    Side effects: real OpenAI calls.
    Role: spike driver.
    """
    key = openai_key()
    question = tts_pcm(key, QUESTION, AUDIO / "question.pcm")
    barge = tts_pcm(key, BARGE_IN, AUDIO / "barge_in.pcm")
    secret, minted = mint_client_secret(key)
    t0 = time.monotonic()
    log: list[dict] = [{"t": 0.0, "local": "minted", "session": minted["session"]}]
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    state = {"barged": False, "relayed": set(), "done_after_barge": False}

    def stamp(**kw) -> None:
        log.append({"t": round(time.monotonic() - t0, 3), **kw})

    url = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {secret}"}) as ws:

        async def pump() -> None:
            """Send queued speech, else silence, at real-time pace (a live mic)."""
            silence = b"\x00" * CHUNK_BYTES
            while True:
                try:
                    chunk = queue.get_nowait()
                except asyncio.QueueEmpty:
                    chunk = silence
                await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                          "audio": base64.b64encode(chunk).decode()}))
                await asyncio.sleep(CHUNK_MS / 1000)

        def speak(pcm: bytes, name: str) -> None:
            stamp(local=f"speak_start:{name}", ms=len(pcm) * 1000 // (RATE * 2))
            for i in range(0, len(pcm), CHUNK_BYTES):
                queue.put_nowait(pcm[i:i + CHUNK_BYTES].ljust(CHUNK_BYTES, b"\x00"))

        pump_task = asyncio.create_task(pump())
        speak(question, "question")
        deadline = time.monotonic() + 60
        try:
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
                except asyncio.TimeoutError:
                    break
                ev = json.loads(raw)
                stamp(event=summarize(ev))
                typ = ev["type"]
                if typ == "response.done":
                    for item in ev["response"].get("output", []):
                        if (item.get("type") == "function_call"
                                and ev["response"]["status"] == "completed"
                                and item["call_id"] not in state["relayed"]):
                            state["relayed"].add(item["call_id"])
                            output = {"say": SAY, "escalated": False}
                            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                                "type": "function_call_output", "call_id": item["call_id"],
                                "output": json.dumps(output)}}))
                            await ws.send(json.dumps({"type": "response.create"}))
                            stamp(local="sent_function_call_output_and_response_create",
                                  call_id=item["call_id"])
                    if state["barged"] and ev["response"]["status"] != "cancelled":
                        state["done_after_barge"] = True
                    if state["done_after_barge"]:
                        break
                if (typ == "response.output_audio.delta" and state["relayed"]
                        and not state["barged"]):
                    # Over WebSocket the server generates audio faster than real time, so
                    # barge in on the first reply delta while the response is in progress.
                    state["barged"] = True
                    speak(barge, "barge_in")
                if typ == "error":
                    print("server error:", ev.get("error"))
        finally:
            pump_task.cancel()

    out = HERE / "runs" / f"{label}.jsonl"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(json.dumps(r) for r in log) + "\n")
    print_summary(log)
    print("log:", out)


def print_summary(log: list[dict]) -> None:
    """Print first-seen times for the events the spike cares about.

    What: one line per key event type (first and count) plus derived deltas.
    Use when: after a run.
    Inputs: the in-memory log.
    Output: stdout lines.
    Side effects: prints.
    Role: evidence for SPIKE_NOTE.md.
    """
    seen: dict[str, list[float]] = {}
    for r in log:
        name = r.get("event", {}).get("type") or r.get("local")
        seen.setdefault(name, []).append(r["t"])
    for name, ts in seen.items():
        if name == "response.output_audio.delta" or name.endswith(".delta"):
            print(f"{name:60s} first={ts[0]:7.3f} n={len(ts)}")
        else:
            print(f"{name:60s} {', '.join(f'{t:.3f}' for t in ts)}")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "run1"))
