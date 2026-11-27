# Spike note: OpenAI Realtime (doc 07 s14 rows 10 and 11)

Task CS-E-05.S3.T1, lane C, run Fri 27 Nov 2026 between 00:19 and 00:25 EST (local `date`). Harness: `harness_ws.py` (WebSocket, synthesized speech), `webrtc_sdp.py` (WebRTC via aiortc, the browser path), shared `common.py`. Raw event logs: `runs/run1.jsonl`, `runs/run2.jsonl`, `runs/webrtc1.jsonl` (audio deltas reduced to their length; no secrets).

## Verdicts

| Row | Question | Result |
|---|---|---|
| 11 | Realtime model id | **`gpt-realtime-2.1`**: listed by `GET /v1/models`, accepted by `client_secrets`, echoed in `session.created`. Doc 07 name confirmed. |
| 11 | Transcription model id | **`gpt-transcribe`**: accepted with `semantic_vad` in a `realtime` session, echoed in `session.created`, produced deltas and finals in every run. |
| 11 | Voice | **`marin`** accepted and echoed (also the docs' example voice). |
| 10 | Which event completes a function call | Act on **`response.done` with `response.status == "completed"`** and an output item of `type: "function_call"` (`call_id`, `name`, `arguments`, `id`). `response.function_call_arguments.done` arrives 4 ms earlier with the same fields, but the reference says it is "also emitted when a Response is interrupted, incomplete, or cancelled", so it is not a safe trigger. Dedupe by `call_id`. |
| 10 | Final transcript within 1.5 s of the function call | **Pass in both spoken runs**: the final arrived before the function call, by 0.204 s and 0.590 s. Not guaranteed by the docs ("may come before or after the Response events"), so the 1.5 s wait in the relay stays. |

Fallback in row 10 (treat transcript as late) is not needed but the code path stays, per s6.6.

## 1. Model ids (GET /v1/models, 1 call)

Realtime family listed: `gpt-realtime`, `gpt-realtime-1.5`, `gpt-realtime-2`, `gpt-realtime-2.1`, `gpt-realtime-2.1-mini`, `gpt-realtime-mini`, dated snapshots, `gpt-realtime-translate`, `gpt-realtime-whisper`. Transcription: `gpt-transcribe`, `gpt-live-transcribe`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `gpt-4o-transcribe-diarize`, `whisper-1`.

Why `gpt-transcribe`, not `gpt-live-transcribe`: the transcription guide recommends `gpt-live-transcribe` for transcription-only sessions but says it "doesn't support `server_vad` or `semantic_vad`"; our session needs `semantic_vad`. `gpt-transcribe` streams deltas and a final on commit. Untested alternative, not needed.

Price of `gpt-realtime-2.1` per 1M tokens (models page): text in 4.00, cached in 0.40, text out 24.00, audio in 32.00, audio out 64.00. Recorded as a comment in `data/models.yaml` for lane B to copy into `price`.

## 2. Mint and connect

Mint (server side, standard key): `POST https://api.openai.com/v1/realtime/client_secrets`, JSON body:

```json
{"expires_after": {"anchor": "created_at", "seconds": 600},
 "session": {"type": "realtime", "model": "gpt-realtime-2.1", "instructions": "...",
   "audio": {
     "input": {"format": {"type": "audio/pcm", "rate": 24000},
               "transcription": {"model": "gpt-transcribe"},
               "turn_detection": {"type": "semantic_vad", "eagerness": "auto",
                                  "interrupt_response": true, "create_response": true}},
     "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "marin"}},
   "tools": [{"type": "function", "name": "ask_support_agent", "description": "...",
              "parameters": {"type": "object", "additionalProperties": false, "required": ["question"],
                             "properties": {"question": {"type": "string", "maxLength": 2000}}}}],
   "tool_choice": "auto"}}
```

Reply: `value` (the `ek_...` secret), `expires_at`, `session` (full echo). GA field mapping for doc 07 s6.6: `input_audio_transcription` becomes `audio.input.transcription`; `turn_detection` becomes `audio.input.turn_detection`; `voice` becomes `audio.output.voice`; `expires` becomes `expires_after.seconds`. The strict schema with `maxLength` was accepted.

WebRTC (browser): `POST https://api.openai.com/v1/realtime/calls`, headers `Authorization: Bearer <ek_...>`, `Content-Type: application/sdp`, body = offer SDP. Reply **201** with the answer SDP as text and a `Location` header holding the call id. Data channel name `oai-events`; first message `session.created` echoing model, voice, tool, `semantic_vad`, `gpt-transcribe`. The same ephemeral key also authorizes `wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1`.

## 3. Function call and output

Observed order in one response (all 3 runs): assistant audio message (the holding phrase, for example "One moment while I check on that for you."), then a `function_call` item; `response.function_call_arguments.done` then `response.done` (status `completed`, output `[message, function_call]`). Arguments were the user's words: `{"question":"Hi, what is your refund policy if I cancel my annual plan?"}`.

Return path, sent back to back:

```json
{"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "call_...", "output": "{\"say\": \"...\", \"escalated\": false}"}}
{"type": "response.create"}
```

The server answered with `conversation.item.added` (`function_call_output`) within 0.1 s and a new response; run1 read the `say` text verbatim.

**Gap for T3 (`user_item_id`):** the function call event carries no user item id. Derive it in the browser as the `item_id` of the latest `input_audio_buffer.committed` before that response's `response.created` (equivalently, walk `previous_item_id` from the function call item: function_call, assistant holding message, user item).

## 4. Transcription timing (seconds from session start)

| Run | `committed` | transcript `.completed` | `function_call_arguments.done` | `response.done` | Transcript vs call |
|---|---|---|---|---|---|
| run1 | 6.297 | 7.320 | 7.524 | 7.528 | 0.204 s before |
| run2 | 6.090 | 6.548 | 7.138 | 7.142 | 0.590 s before |
| run2 barge-in turn | 15.624 | 16.074 | (no call) | 17.001 | arrived after `response.created` (15.632) |

Event names: `conversation.item.input_audio_transcription.delta` (fields `item_id`, `delta`), `conversation.item.input_audio_transcription.completed` (`item_id`, `content_index`, `transcript`, `usage`), `conversation.item.input_audio_transcription.failed`. Deltas start within 0.4 s of commit. Match finals by `item_id` (the guide says finals from different turns are not guaranteed in order). The third row shows a final arriving after response events, which is why the 1.5 s wait stays.

## 5. Interruption

- **Barge-in (VAD, run2 over WebSocket):** `input_audio_buffer.speech_started` at 8.161, then the in-progress reply ends with `response.done` status `cancelled`, `status_details {"type": "cancelled", "reason": "turn_detected"}` at 8.197 (36 ms); the assistant item finishes with status `incomplete`. Then the new user item goes through `speech_stopped`, `committed`, a new response.
- **Stop speaking (WebRTC):** client sends `output_audio_buffer.clear`; server replies `output_audio_buffer.cleared` in 62 ms, then `conversation.item.truncated` with `audio_end_ms: 1560` (what was actually played). No `response.cancel` needed: the response had already finished generating (`response.done` at 11.392 while audio played until 12.002). This is the "cancels audio only" behaviour of 06 s17.
- `output_audio_buffer.started`, `.stopped`, `.cleared` (fields `event_id`, `response_id`) exist only on WebRTC and SIP. `started` arrived once per response. No `stopped` arrived between the holding phrase and the reply (playback ran straight on), so "agent finished speaking" should be `stopped`, never `response.done`, and the UI should not expect one `stopped` per response.
- On WebSocket the server sends audio faster than real time (a 5 s reply finished generating in about 1.5 s) and the client owns playback and `conversation.item.truncate`; on WebRTC the server buffers and truncates itself. The browser uses WebRTC, so no client truncate is needed.
- `response.cancel` is for cancelling a response still generating (push-to-talk in the docs); not needed in our flow because `interrupt_response: true` cancels on barge-in.

## 6. Notes and limits of this spike

- Semantic VAD waited longer after an utterance that sounded unfinished ("can you stop for a second?"): `speech_stopped` came several seconds after the clip ended. Expected `eagerness: auto` behaviour; acceptable.
- aiortc took about 5 s to gather ICE before the SDP POST; a browser is faster. Not a product number.
- Spike 18 (spoken account ID and PIN transcripts) is not in this dispatch.
- Real OpenAI calls: 9 (1 models list, 2 TTS, 3 mints, 2 WebSocket sessions, 1 SDP exchange plus its WebRTC session).

## Sources (read 2026-11-27)

- WebRTC guide: https://developers.openai.com/api/docs/guides/realtime-webrtc (mint, `/v1/realtime/calls`, `data.value`)
- Conversations guide: https://developers.openai.com/api/docs/guides/realtime-conversations (function call flow, `function_call_output`, `response.create`, interruption, WebRTC vs WebSocket truncation)
- Server events reference: https://developers.openai.com/api/reference/resources/realtime/server-events (`response.function_call_arguments.done` also on cancel; transcription ordering; WebRTC-only buffer events)
- Client secrets reference: https://developers.openai.com/api/reference/resources/realtime/subresources/client_secrets/methods/create (body, `expires_after`, transcription model list, `semantic_vad` fields)
- Transcription guide: https://developers.openai.com/api/docs/guides/realtime-transcription (`gpt-live-transcribe` vs `gpt-transcribe`, finals matched by `item_id`)
- Model page: https://developers.openai.com/api/docs/models/gpt-realtime-2.1 (price, context)
