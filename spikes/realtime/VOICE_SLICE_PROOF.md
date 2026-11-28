# Thin voice slice proof (CS-E-08.S5.T2)

Run: Fri 27 Nov 2026, 03:37 to 03:39 EST, on main at 44cd7f1, local Compose (project cs-a3v, EDGE_PORT 3073, EDGE_SUBNET 172.30.73.0/24). Checkpoint row: doc 09 s4 "Sat 13:00 (thin voice slice)"; D-031; doc 04 items 2.1, 2.3.

Verdict: **PASS**. No code change was needed.

## Setup

- `docker compose up -d --build`, then `docker compose run --rm seed_core` (prints `seeded`) and one `docker compose run --rm ingest_knowledge` (`articles=10 chunks=81 embedded=81 embedding_calls=1`). `/api/readyz` answered `ready` with redis, postgres, core and knowledge `ok`.
- Spoken question: OpenAI TTS (`gpt-4o-mini-tts`, voice alloy) of "Did my refund go through, and when will I see it?" (3.8 s), padded with 6 s of silence before and 120 s after so Chrome's looping fake capture asks once.
- Chrome (system channel) driven by Playwright 1.49.1 with `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream --use-file-for-fake-audio-capture=<wav>`. An init script wraps `RTCPeerConnection.prototype.createDataChannel` to record every data channel event in and out (no app code touched).
- Persona: Natasha Romanoff (QS-10423), not verified.

## What happened (run 2; times are seconds since page load)

| t | Step |
|---|---|
| 2.04 | Mic clicked (`Start voice`) |
| 3.07 | `POST /api/sessions/{sid}/voice/token` 200 (the agent minted the client secret) |
| 4.14 | `POST https://api.openai.com/v1/realtime/calls` 201 (SDP answer) |
| 15.55 | `POST /api/sessions/{sid}/voice/ask` 200 (same agent, `mode: voice`) |
| 15.91 | Dock caption "You said: Did my refund go through, and when will I see it?" |
| 20.00 | Dock "Speaking", Agent line shows the say |
| 27.62 | Dock back to `listening` after the say's audio stopped |
| 27.75 | Back to typing ends the call; dock removed |

## Realtime events (trimmed; ms on the page clock)

```
3545  in  session.created {"model": "gpt-realtime-2.1"}
7470  in  input_audio_buffer.speech_started
13535 in  input_audio_buffer.committed
14048 in  conversation.item.input_audio_transcription.completed "Did my refund go through, and when will I see it?"
14155 in  output_audio_buffer.started            (preamble "One moment while I check that.")
14558 in  response.done status=completed output=[message, function_call ask_support_agent call_ChV3XnGtQLCLe18H
          arguments {"question":"Did my refund go through and when will I see it?"}]
15717 out conversation.item.create function_call_output call_ChV3XnGtQLCLe18H
          {"say":"To look at your account I need to verify you. Please tell me your account ID and your four-digit
           support PIN, or type them into the card on screen.","escalated":false,"awaiting":"verification"}
15717 out response.create {"metadata":{"qs_say":"say-1"}}
16325 in  output_audio_buffer.started            (the say)
18016 in  response.output_audio_transcript.done  (matches the say word for word)
18018 in  response.done status=completed metadata={"qs_say":"say-1"}
24683 in  output_audio_buffer.stopped
```

Agent side (`agent.turn_events` for the voice turn): `turn.started mode=voice`, model step `claude-sonnet-5-5` ok (1.5 s), tool `request_verification`, `verification.required`, `turn.completed` with the `say` above.

## Evidence files (outside git, in the project build-log)

- `build-log/reports/screens/voice-slice.webm` (Playwright video; video only, no audio track)
- `build-log/reports/screens/voice-slice-caption.png`, `voice-slice-say.png`, `voice-slice-console.png`, `voice-slice-end.png`
- `build-log/voice-slice/realtime-events.json` (full log), `driver-log.txt`, `driver-run.mjs`, `question.wav`
- `build-log/voice-slice/run1/` (first run, same result; its video ends with a 30 s idle tail from a wrong End selector in the driver)

## Observations (not slice failures)

- The spoken say ("Please tell me your account ID and your four-digit support PIN, or type them into the card on screen.") differs from the chat text ("enter ... in the secure card"). Both are the frozen doc 07 router texts (route `verify`; Rule V voice verification), so this is as specified; the spoken-PIN limitation is already documented in doc 07 s11.
- The browser logs one 401 (`GET /api/demo/personas` before the gate, expected) and one 404 (`/favicon.ico`) on the landing page.
