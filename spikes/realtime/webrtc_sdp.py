"""WebRTC SDP exchange and data-channel event check (spike 10, CS-E-05.S3.T1).

What: does what the browser will do: mint a client secret, POST an SDP offer to
    /v1/realtime/calls with it, open the `oai-events` data channel, send one typed
    question, relay the ask_support_agent call, then send output_audio_buffer.clear
    (the Stop speaking button) once reply audio starts, logging every event.
Use when: verifying the WebRTC path and the WebRTC-only output_audio_buffer events.
Inputs: argv[1] run label (default "webrtc1"); .env OPENAI_API_KEY.
Output: spikes/realtime/runs/<label>.jsonl plus a printed summary.
Side effects: two real OpenAI calls (mint, SDP exchange) and one Realtime session.
Role: throwaway spike; aiortc stands in for the browser RTCPeerConnection.

Run: uv run --no-project --with aiortc python spikes/realtime/webrtc_sdp.py webrtc1
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time
import urllib.request

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaBlackhole
from aiortc.mediastreams import AudioStreamTrack

from common import mint_client_secret, openai_key

HERE = pathlib.Path(__file__).resolve().parent
SAY = ("Annual plans can be refunded in full within 30 days of purchase. After that, you keep "
       "access until the end of the term, and no further renewals are charged. If you paid by "
       "invoice, the credit goes back to the original payment method within five to ten "
       "business days.")


def post_sdp(offer_sdp: str, secret: str) -> tuple[int, str, str]:
    """POST the SDP offer with the ephemeral key.

    What: the browser's SDP exchange call.
    Use when: after setLocalDescription.
    Inputs: offer SDP text, client secret.
    Output: (status, Location header, answer SDP).
    Side effects: one real OpenAI call.
    Role: verifies the WebRTC endpoint and auth.
    """
    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/calls",
        data=offer_sdp.encode(),
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/sdp"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.headers.get("Location", ""), resp.read().decode()


async def run(label: str) -> None:
    """Connect over WebRTC and record the data-channel event sequence.

    What: offer, answer, data channel events until the post-clear response ends.
    Use when: main entry.
    Inputs: run label.
    Output: none (writes runs/<label>.jsonl, prints summary).
    Side effects: real OpenAI calls.
    Role: spike driver.
    """
    secret, _ = mint_client_secret(openai_key())
    t0 = time.monotonic()
    log: list[dict] = []
    done = asyncio.Event()
    state = {"relayed": False, "cleared": False}

    def stamp(**kw) -> None:
        log.append({"t": round(time.monotonic() - t0, 3), **kw})

    pc = RTCPeerConnection()
    pc.addTrack(AudioStreamTrack())  # silent mic
    sink = MediaBlackhole()

    @pc.on("track")
    def on_track(track):  # remote audio
        sink.addTrack(track)
        stamp(local="remote_track")

    dc = pc.createDataChannel("oai-events")

    def send(obj: dict) -> None:
        dc.send(json.dumps(obj))

    @dc.on("open")
    def on_open():
        stamp(local="datachannel_open")
        send({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": "What is your refund policy for annual plans?"}]}})
        send({"type": "response.create"})

    @dc.on("message")
    def on_message(raw):
        ev = json.loads(raw)
        typ = ev["type"]
        if typ.endswith(".delta"):
            stamp(event={"type": typ})
            return
        stamp(event=ev)
        if typ == "response.done":
            fcs = [o for o in ev["response"]["output"] if o["type"] == "function_call"]
            if fcs and not state["relayed"] and ev["response"]["status"] == "completed":
                state["relayed"] = True
                send({"type": "conversation.item.create", "item": {"type": "function_call_output",
                      "call_id": fcs[0]["call_id"],
                      "output": json.dumps({"say": SAY, "escalated": False})}})
                send({"type": "response.create"})
                stamp(local="sent_function_call_output_and_response_create")
        if typ == "output_audio_buffer.started" and state["relayed"] and not state["cleared"]:
            state["cleared"] = True

            async def clear_later():
                await asyncio.sleep(1.5)
                send({"type": "output_audio_buffer.clear"})
                stamp(local="sent_output_audio_buffer.clear")
            asyncio.ensure_future(clear_later())
        if typ in ("output_audio_buffer.cleared",) or (
                typ == "output_audio_buffer.stopped" and state["cleared"]):
            asyncio.get_event_loop().call_later(2.0, done.set)
        if typ == "error":
            print("server error:", ev.get("error"))

    await sink.start()
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    status, location, answer = post_sdp(pc.localDescription.sdp, secret)
    stamp(local="sdp_answer", status=status, location=location.split("/")[-1][:8] + "...")
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
    try:
        await asyncio.wait_for(done.wait(), timeout=45)
    except asyncio.TimeoutError:
        stamp(local="timeout")
    await pc.close()
    await sink.stop()

    out = HERE / "runs" / f"{label}.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in log) + "\n")
    for r in log:
        e = r.get("event")
        if e is None:
            print(f"{r['t']:7.3f} local {r.get('local')} {r.get('status', '')}")
        elif not e["type"].endswith(".delta"):
            extra = ""
            if e["type"] == "response.done":
                extra = f"status={e['response']['status']} {e['response'].get('status_details')}"
            if e["type"] == "response.output_audio_transcript.done":
                extra = e.get("transcript", "")[:90]
            if e["type"] == "response.function_call_arguments.done":
                extra = f"{e['name']} {e['call_id']} {e['arguments']}"
            print(f"{r['t']:7.3f} {e['type']} {extra}")
    print("log:", out)


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "webrtc1"))
