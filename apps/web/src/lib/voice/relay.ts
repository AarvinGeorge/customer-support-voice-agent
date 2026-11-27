/**
 * What: the voice relay: reads Realtime server events from the data channel, relays each
 *   completed `ask_support_agent` call to POST /api/sessions/{sid}/voice/ask through the turn
 *   store, and answers Realtime with `function_call_output` then `response.create`.
 * Use when: VoiceClient feeds every data channel message to `handle`.
 * Inputs: Realtime server events (GA names, D-045); RelayDeps.
 * Output: a VoiceRelay (handle, close, settled).
 * Side effects: network through store.sendVoiceAsk (one POST per call_id); data channel sends
 *   through deps.send; final user transcripts are added to the shared thread (mode voice); timers
 *   for the 1.5 s transcript wait.
 * Role: CS-E-05.S3.T3 relay (doc 07 s6.6, s2.3 VoiceAskRequest, s8.1 browser voice relay 25 s).
 *   Rules: act only on `response.done` with status completed and a function_call item
 *   (`function_call_arguments.done` also fires on cancel, SPIKE_NOTE s3); dedupe by call_id; the
 *   user item is the last `input_audio_buffer.committed` before that response's `response.created`;
 *   wait up to 1.5 s for its final transcript (order is not guaranteed); the 25 s relay window
 *   starts when the call arrives and includes that wait; on timeout or error answer the fixed line;
 *   interrupting never cancels the backend turn; a call that has not posted when the call ends
 *   never posts.
 *   CS-E-05.S4.T3 adds live transcripts and interruption: user finals and what the agent said
 *   go into the shared thread keyed by item_id (the say is linked to its voice/ask turn); a
 *   barge-in or Stop speaking while agent audio plays sends `output_audio_buffer.clear` at once
 *   (audio only, never response.cancel, SPIKE_NOTE s5) and freezes the bubble at what was heard
 *   (wall time since the response's audio started, then the server's `conversation.item.truncated`
 *   audio_end_ms); finished items are posted to POST /transcript in batches (doc 07 s2.3, s2.6).
 */
import { LIMITS, type TranscriptItemIn, type TranscriptPost, type TurnCompletedEvent, type VoiceAskRequest } from "@contracts/contracts";
import type { StoreApi } from "zustand";
import type { ApiDeps } from "@/lib/api/client";
import type { TurnStoreState } from "@/lib/turns/store";

import type { VoiceEvent } from "./machine";

/**
 * What: the line Realtime speaks when the relay times out or fails (doc 07 s6.6).
 * Use when: voice/ask did not reach turn.completed within 25 s, or answered an error.
 * Inputs: none.
 * Output: a string.
 * Side effects: none.
 * Role: constant; never "nothing was changed", because the server turn may still finish.
 */
export const RELAY_FAILURE_SAY = "I couldn't finish that just now. Please check the chat for the result before trying again.";

/**
 * What: the speaking rate used to estimate what was heard of an interrupted utterance.
 * Use when: heardText.
 * Inputs: none.
 * Output: characters per second.
 * Side effects: none.
 * Role: constant. On WebRTC the transcript text arrives faster than the audio plays and no event
 *   carries the heard text, only audio_end_ms, so the cut is the time heard times this rate
 *   (about 165 words a minute, a typical synthetic voice).
 */
export const CHARS_PER_SECOND = 15;

/**
 * What: the debounce before finished items are posted to /transcript.
 * Use when: queueing a finished item.
 * Inputs: none.
 * Output: ms.
 * Side effects: none.
 * Role: constant; batching keeps a 10-minute call well under the 120 posts per hour limit (doc
 *   07 s9) and lets a truncation event refine an interrupted item before it is sent.
 */
const TRANSCRIPT_FLUSH_MS = 1500;

/**
 * What: the part of an utterance heard in a given time, cut at a word boundary.
 * Use when: freezing an interrupted assistant bubble.
 * Inputs: the full text, the heard time in ms.
 * Output: the heard prefix (the whole text when the time covers it).
 * Side effects: none.
 * Role: pure helper.
 */
export function heardText(text: string, heardMs: number): string {
  const n = Math.floor((Math.max(0, heardMs) / 1000) * CHARS_PER_SECOND);
  if (n >= text.length) return text;
  let cut = text.slice(0, n);
  if (!/\s/.test(text.charAt(n))) cut = cut.replace(/\S*$/, "");
  return cut.trimEnd();
}

/**
 * What: one assistant utterance the relay tracks.
 * Use when: the relay's `spoken` map.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: state record. `ended` is true once its audio finished playing (output_audio_buffer.stopped).
 */
interface Utterance {
  responseId: string | null;
  text: string;
  done: boolean;
  interrupted: boolean;
  heardMs: number | null;
  ended: boolean;
}

/**
 * What: the Realtime tool name the relay answers.
 * Use when: filtering function calls.
 * Inputs: none.
 * Output: a string.
 * Side effects: none.
 * Role: constant (the only tool in the session, doc 07 s6.6).
 */
const TOOL_NAME = "ask_support_agent";

/**
 * What: one Realtime server event as it arrives on the data channel.
 * Use when: typing handle's input.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: loose type; fields are read defensively because the server owns the shape.
 */
export interface RealtimeEvent {
  type: string;
  [field: string]: unknown;
}

/**
 * What: the live caption: the current user utterance, interim (from deltas) or final.
 * Use when: the voice control shows what the user is saying.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view model.
 */
export interface Caption {
  itemId: string;
  text: string;
  final: boolean;
}

/**
 * What: the payload sent back to Realtime as function_call_output (doc 07 s6.6).
 * Use when: answering a call.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: wire type.
 */
export interface FunctionCallOutput {
  say: string;
  escalated: boolean;
  ticket?: { spoken: string; full_id: string };
  awaiting?: "confirm" | "verification";
}

/**
 * What: what the relay needs.
 * Use when: createVoiceRelay.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: dependencies and test seams (the wait and timeout default to doc 07 s8.1).
 */
export interface RelayDeps {
  sessionId: string;
  voiceSessionId: string;
  store: StoreApi<TurnStoreState>;
  /** Sends one client event on the data channel (the caller drops it when the channel is not open). */
  send: (msg: Record<string, unknown>) => void;
  onMachine?: (event: VoiceEvent) => void;
  onCaption?: (caption: Caption | null) => void;
  /** What the agent is saying now (null when its audio stops), for the dock. */
  onAgentLine?: (text: string | null) => void;
  /** Posts finished transcript items (POST /transcript); absent posts nothing. */
  postTranscript?: (body: TranscriptPost) => Promise<void>;
  deps?: ApiDeps;
  transcriptWaitMs?: number;
  relayTimeoutMs?: number;
}

/**
 * What: the relay's interface.
 * Use when: VoiceClient holds one per call.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: handle every server event; close at teardown; settled for tests.
 */
export interface VoiceRelay {
  handle: (event: RealtimeEvent) => void;
  /** Stop speaking: clears the agent's audio buffer (audio only; the backend turn goes on). */
  stopSpeaking: () => void;
  close: () => void;
  settled: () => Promise<void>;
}

/**
 * What: reads a string field.
 * Use when: parsing a server event.
 * Inputs: an object, a key.
 * Output: the string or null.
 * Side effects: none.
 * Role: helper.
 */
function str(obj: unknown, key: string): string | null {
  if (typeof obj !== "object" || obj === null) return null;
  const v = (obj as Record<string, unknown>)[key];
  return typeof v === "string" ? v : null;
}

/**
 * What: builds the function_call_output payload from a completed turn.
 * Use when: voice/ask reached turn.completed.
 * Inputs: the turn.completed event.
 * Output: FunctionCallOutput.
 * Side effects: none.
 * Role: mapping (doc 07 s6.6): say, escalated, ticket as {spoken, full_id}; awaiting
 *   "verification" when the turn asked for verification, "confirm" when a read-back is pending.
 */
export function toFunctionOutput(done: TurnCompletedEvent): FunctionCallOutput {
  const final = done.final;
  const out: FunctionCallOutput = { say: final.say || RELAY_FAILURE_SAY, escalated: final.escalated };
  if (final.ticket) out.ticket = { spoken: final.ticket.display, full_id: final.ticket.ticket_id };
  if (done.end_reason === "verification_requested") out.awaiting = "verification";
  else if (final.pending_action || done.end_reason === "readback") out.awaiting = "confirm";
  return out;
}

/**
 * What: the question argument of a call.
 * Use when: building VoiceAskRequest.question.
 * Inputs: the call's arguments JSON string.
 * Output: the trimmed question, or "" when missing or unreadable.
 * Side effects: none.
 * Role: helper.
 */
function questionOf(args: string | null): string {
  if (args === null) return "";
  try {
    return (str(JSON.parse(args), "question") ?? "").trim();
  } catch {
    return "";
  }
}

/**
 * What: creates the relay for one voice call.
 * Use when: VoiceClient has minted a token and opened the data channel.
 * Inputs: RelayDeps.
 * Output: VoiceRelay.
 * Side effects: see module.
 * Role: see module. last_assistant_interrupted is true when the most recent say (the response
 *   whose echoed metadata carries the qs_say tag of our last response.create, review PR #46 M2) was cut by a barge-in, cleared by Stop speaking, or ended
 *   cancelled; it is read when a call arrives, then reset for the next say.
 */
export function createVoiceRelay(d: RelayDeps): VoiceRelay {
  const waitMs = d.transcriptWaitMs ?? LIMITS.BROWSER_TRANSCRIPT_WAIT_S * 1000;
  const relayMs = d.relayTimeoutMs ?? LIMITS.BROWSER_VOICE_RELAY_S * 1000;
  const machine = (e: VoiceEvent) => d.onMachine?.(e);
  let closed = false;
  const seenCalls = new Set<string>();
  const pending = new Set<Promise<unknown>>();
  const partial = new Map<string, string>();
  const finals = new Map<string, string>();
  const waiters = new Map<string, Array<(t: string) => void>>();
  const responseUser = new Map<string, string | null>();
  let lastCommitted: string | null = null;
  let sayTag: string | null = null;
  let saySeq = 0;
  let sayResponseId: string | null = null;
  let sayPlaying = false;
  let sayInterrupted = false;
  let lastTurnId: string | null = null;
  const sayTurn = new Map<string, string | null>();
  /** Assistant utterances by item id. */
  const spoken = new Map<string, Utterance>();
  let playing: { responseId: string; startedAt: number } | null = null;
  /** Responses whose audio has started playing. */
  const started = new Set<string>();
  /** Responses whose audio was cleared, with the heard time to give utterances that arrive later. */
  const wiped = new Map<string, number>();
  /** The say is created and its audio has not finished playing (review PR #55 I2). */
  let sayPending = false;
  const outbox = new Map<string, TranscriptItemIn>();
  let flushTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * What: sends every queued transcript item in one POST.
   * Use when: the debounce fires, or close.
   * Inputs: none.
   * Output: none.
   * Side effects: one postTranscript call (errors are dropped: the transcript is a best-effort
   *   copy; the live thread is unaffected).
   * Role: helper.
   */
  const flush = () => {
    if (flushTimer !== null) clearTimeout(flushTimer);
    flushTimer = null;
    if (outbox.size === 0 || d.postTranscript === undefined) return;
    const items = [...outbox.values()].slice(0, 50);
    for (const i of items) outbox.delete(i.realtime_item_id);
    void d.postTranscript({ voice_session_id: d.voiceSessionId, items }).catch(() => undefined);
    if (outbox.size > 0) flush();
  };

  /**
   * What: queues one finished item for /transcript (a later call for the same item replaces it).
   * Use when: a user final, an assistant utterance done or interrupted, a truncation.
   * Inputs: item id, role, text, interrupted.
   * Output: none.
   * Side effects: restarts the debounce timer.
   * Role: helper; the client time is when the item first finished.
   */
  const queue = (itemId: string, role: "user" | "assistant", text: string, interrupted: boolean) => {
    if (d.postTranscript === undefined || closed || text === "") return;
    const client_ts = outbox.get(itemId)?.client_ts ?? new Date(Date.now()).toISOString();
    outbox.set(itemId, { realtime_item_id: itemId, role, text: text.slice(0, 4000), interrupted, client_ts });
    if (flushTimer !== null) clearTimeout(flushTimer);
    flushTimer = setTimeout(flush, TRANSCRIPT_FLUSH_MS);
  };

  /**
   * What: the utterance record for an item, created on first sight.
   * Use when: a transcript delta or done.
   * Inputs: the item id and the event's response id.
   * Output: the record (interrupted from the start when its response was already cleared).
   * Side effects: inserts into `spoken`.
   * Role: helper.
   */
  const utterance = (itemId: string, responseId: string | null): Utterance => {
    const have = spoken.get(itemId);
    if (have !== undefined) return have;
    const cut = responseId === null ? undefined : wiped.get(responseId);
    const u: Utterance = { responseId, text: "", done: false, interrupted: cut !== undefined, heardMs: cut ?? null, ended: false };
    spoken.set(itemId, u);
    return u;
  };

  /**
   * What: writes an assistant utterance into the thread, and into the outbox once it is settled.
   * Use when: its transcript is done, its audio ended, it was interrupted, or a truncation refined it.
   * Inputs: the item id.
   * Output: none.
   * Side effects: store.upsertAgentVoice; queue only when the audio ended or was interrupted
   *   (the server keeps the first post of an item, so the first one must be final: review PR #55 I1).
   * Role: helper.
   */
  const publish = (itemId: string) => {
    const u = spoken.get(itemId);
    if (u === undefined || (!u.done && !u.interrupted)) return;
    const heard = u.interrupted ? heardText(u.text, u.heardMs ?? 0) : u.text;
    const turnId = u.responseId === null ? null : (sayTurn.get(u.responseId) ?? null);
    d.store.getState().upsertAgentVoice({ itemId, text: u.text, heard, interrupted: u.interrupted, turnId });
    if (u.interrupted || (u.ended && u.done)) queue(itemId, "assistant", heard, u.interrupted);
  };

  /**
   * What: marks every utterance of a response interrupted at a heard time.
   * Use when: its audio was cleared or the call ended while it played or waited to play.
   * Inputs: the response id, the heard ms.
   * Output: none.
   * Side effects: updates records, publish; remembers the response in `wiped`.
   * Role: helper.
   */
  const cutResponse = (responseId: string, heardMs: number) => {
    wiped.set(responseId, heardMs);
    for (const [itemId, u] of spoken) {
      if (u.responseId !== responseId || u.interrupted || u.ended) continue;
      u.interrupted = true;
      u.heardMs = heardMs;
      publish(itemId);
    }
  };

  /**
   * What: stops the agent audio that is playing or queued and freezes what was said so far.
   * Use when: a barge-in (speech_started), Stop speaking, a matching cleared event, or call end,
   *   while agent audio plays or the say waits to play.
   * Inputs: whether to send the clear (false for a cleared event and at call end).
   * Output: none.
   * Side effects: sends output_audio_buffer.clear (never response.cancel: the backend turn and
   *   any function call in flight go on); marks the playing response's utterances interrupted
   *   with the heard time, and a say that had not started with 0 ms heard (it sets
   *   last_assistant_interrupted, review PR #55 I2); the dock's agent line clears.
   * Role: helper (06 s17: agent audio stops within 0.5 s of barge-in).
   */
  const interruptPlaying = (send = true) => {
    if (playing === null && !sayPending) return;
    const now = Date.now();
    const current = playing;
    playing = null;
    if (send && !closed) d.send({ type: "output_audio_buffer.clear" });
    if (current !== null) {
      if (current.responseId === sayResponseId) sayInterrupted = true;
      cutResponse(current.responseId, now - current.startedAt);
    }
    if (sayPending && sayResponseId !== null) {
      sayInterrupted = true;
      if (current?.responseId !== sayResponseId) cutResponse(sayResponseId, 0);
    }
    sayPending = false;
    sayPlaying = false;
    d.onAgentLine?.(null);
  };

  /**
   * What: settles the utterances of every response that has played, when the audio stops.
   * Use when: output_audio_buffer.stopped.
   * Inputs: none.
   * Output: none.
   * Side effects: marks records ended and publishes them (queues them for /transcript).
   * Role: helper (no stopped arrives between back-to-back responses, SPIKE_NOTE s5).
   */
  const audioEnded = () => {
    for (const [itemId, u] of spoken) {
      if (u.responseId === null || !started.has(u.responseId) || u.ended || u.interrupted) continue;
      u.ended = true;
      publish(itemId);
    }
  };

  /**
   * What: waits for the final transcript of a user item, up to the wait window.
   * Use when: a call arrived.
   * Inputs: the user item id or null.
   * Output: the transcript, or null (no item, or the window passed).
   * Side effects: one timer.
   * Role: helper.
   */
  const waitFinal = (itemId: string | null): Promise<string | null> => {
    if (itemId === null) return Promise.resolve(null);
    const have = finals.get(itemId);
    if (have !== undefined) return Promise.resolve(have);
    return new Promise((resolve) => {
      let done = false;
      const timer = setTimeout(() => {
        if (done) return;
        done = true;
        resolve(null);
      }, waitMs);
      const list = waiters.get(itemId) ?? [];
      list.push((t) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(t);
      });
      waiters.set(itemId, list);
    });
  };

  /**
   * What: relays one call and returns what Realtime should say.
   * Use when: a new call_id in a completed response.
   * Inputs: call id, arguments, user item id, the interrupted flag read at arrival, the deadline (ms
   *   since epoch, call arrival plus 25 s).
   * Output: FunctionCallOutput (the fixed line on any failure), or null when the call ended before
   *   the post (review PR #46 M5).
   * Side effects: one POST voice/ask through the store (events reach the shared reducer).
   * Role: helper. An empty final transcript counts as none (review PR #46 M4).
   */
  const relayOne = async (callId: string, args: string | null, userItemId: string | null, interrupted: boolean, deadline: number): Promise<FunctionCallOutput | null> => {
    lastTurnId = null;
    const transcript = (await waitFinal(userItemId)) || null;
    if (closed) return null;
    const question = (questionOf(args) || transcript || "").slice(0, 2000);
    if (question === "") return { say: RELAY_FAILURE_SAY, escalated: false };
    const body: VoiceAskRequest = {
      client_turn_id: crypto.randomUUID(),
      voice_session_id: d.voiceSessionId,
      realtime_call_id: callId,
      question,
      user_item_id: userItemId,
      user_transcript: transcript,
      transcript_final: transcript !== null,
      last_assistant_interrupted: interrupted,
    };
    try {
      const left = Math.max(1, deadline - Date.now());
      const { completed } = await d.store.getState().sendVoiceAsk(d.sessionId, body, { deps: d.deps, turnMs: left, noBytesMs: left });
      lastTurnId = completed.turn_id;
      return toFunctionOutput(completed);
    } catch {
      return { say: RELAY_FAILURE_SAY, escalated: false };
    }
  };

  /**
   * What: handles a response.done.
   * Use when: the server finished a response.
   * Inputs: the event.
   * Output: none.
   * Side effects: starts relays; on the last output of the response sends response.create.
   * Role: helper (D-045 trigger).
   */
  const onResponseDone = (e: RealtimeEvent) => {
    const r = e.response as { id?: unknown; status?: unknown; output?: unknown } | undefined;
    const id = str(r, "id");
    const status = str(r, "status");
    if (status !== "completed") {
      if (id !== null && id === sayResponseId && status === "cancelled") sayInterrupted = true;
      return;
    }
    const output = Array.isArray(r?.output) ? (r.output as unknown[]) : [];
    const calls = output.filter((o) => str(o, "type") === "function_call" && str(o, "name") === TOOL_NAME && str(o, "call_id") !== null && !seenCalls.has(str(o, "call_id") as string));
    if (calls.length === 0) return;
    for (const c of calls) seenCalls.add(str(c, "call_id") as string);
    machine("tool_call");
    const userItemId = (id !== null ? responseUser.get(id) : undefined) ?? lastCommitted;
    const interrupted = sayInterrupted;
    const deadline = Date.now() + relayMs;
    let remaining = calls.length;
    for (const c of calls) {
      const callId = str(c, "call_id") as string;
      const p = relayOne(callId, str(c, "arguments"), userItemId, interrupted, deadline).then((out) => {
        if (!closed && out !== null) d.send({ type: "conversation.item.create", item: { type: "function_call_output", call_id: callId, output: JSON.stringify(out) } });
        remaining -= 1;
        if (remaining === 0 && !closed) {
          saySeq += 1;
          sayTag = `say-${saySeq}`;
          d.send({ type: "response.create", response: { metadata: { qs_say: sayTag } } });
        }
      });
      pending.add(p);
      void p.finally(() => pending.delete(p));
    }
  };

  /**
   * What: the event dispatcher.
   * Use when: every data channel message.
   * Inputs: the event.
   * Output: none.
   * Side effects: see module.
   * Role: maps Realtime events to machine events, captions, transcripts and relays.
   */
  const handle = (e: RealtimeEvent) => {
    const itemId = str(e, "item_id");
    switch (e.type) {
      case "session.created":
        machine("connected");
        return;
      case "input_audio_buffer.speech_started":
        if (sayPlaying) sayInterrupted = true;
        interruptPlaying();
        d.onCaption?.(null);
        machine("user_speech");
        return;
      case "input_audio_buffer.speech_stopped":
        machine("user_done");
        return;
      case "input_audio_buffer.committed":
        if (itemId !== null) lastCommitted = itemId;
        return;
      case "conversation.item.input_audio_transcription.delta": {
        if (itemId === null || finals.has(itemId)) return;
        const text = (partial.get(itemId) ?? "") + (str(e, "delta") ?? "");
        partial.set(itemId, text);
        d.onCaption?.({ itemId, text, final: false });
        return;
      }
      case "conversation.item.input_audio_transcription.completed": {
        if (itemId === null || finals.has(itemId)) return;
        const text = (str(e, "transcript") ?? "").trim();
        finals.set(itemId, text);
        partial.delete(itemId);
        d.onCaption?.({ itemId, text, final: true });
        if (text !== "") {
          d.store.getState().addUserMessage(text, "voice", itemId);
          queue(itemId, "user", text, false);
        }
        for (const w of waiters.get(itemId) ?? []) w(text);
        waiters.delete(itemId);
        return;
      }
      case "response.created": {
        const id = str(e.response, "id");
        if (id === null) return;
        responseUser.set(id, lastCommitted);
        const meta = (e.response as { metadata?: unknown } | undefined)?.metadata;
        if (sayTag !== null && str(meta, "qs_say") === sayTag) {
          sayTag = null;
          sayTurn.set(id, lastTurnId);
          sayResponseId = id;
          sayInterrupted = false;
          sayPending = true;
          machine("audio_started");
        }
        return;
      }
      case "response.done":
        onResponseDone(e);
        return;
      case "output_audio_buffer.started": {
        const rid = str(e, "response_id");
        if (rid === sayResponseId && sayResponseId !== null) sayPlaying = true;
        if (rid !== null) {
          playing = { responseId: rid, startedAt: Date.now() };
          started.add(rid);
        }
        machine("audio_started");
        return;
      }
      case "output_audio_buffer.stopped": {
        const rid = str(e, "response_id");
        if (sayResponseId !== null && (rid === sayResponseId || started.has(sayResponseId))) sayPending = false;
        sayPlaying = false;
        playing = null;
        audioEnded();
        d.onAgentLine?.(null);
        machine("audio_stopped");
        return;
      }
      case "output_audio_buffer.cleared": {
        const rid = str(e, "response_id");
        if (rid !== null && playing !== null && rid !== playing.responseId) return;
        if (sayPlaying) sayInterrupted = true;
        interruptPlaying(false);
        machine("audio_stopped");
        return;
      }
      case "response.output_audio_transcript.delta": {
        if (itemId === null) return;
        const u = utterance(itemId, str(e, "response_id"));
        u.text += str(e, "delta") ?? "";
        if (!u.interrupted) d.onAgentLine?.(u.text);
        else publish(itemId);
        return;
      }
      case "response.output_audio_transcript.done": {
        if (itemId === null) return;
        const u = utterance(itemId, str(e, "response_id"));
        u.text = str(e, "transcript") ?? u.text;
        u.done = true;
        publish(itemId);
        return;
      }
      case "conversation.item.truncated": {
        const u = itemId === null ? undefined : spoken.get(itemId);
        const end = typeof e.audio_end_ms === "number" ? e.audio_end_ms : null;
        if (u === undefined || itemId === null || end === null) return;
        u.interrupted = true;
        u.heardMs = end;
        publish(itemId);
        return;
      }
      default:
        return;
    }
  };

  return {
    handle,
    stopSpeaking: () => {
      if (playing !== null) interruptPlaying();
      else if (!closed) d.send({ type: "output_audio_buffer.clear" });
    },
    close: () => {
      if (!closed) interruptPlaying(false);
      flush();
      closed = true;
    },
    settled: async () => {
      while (pending.size > 0) await Promise.allSettled([...pending]);
    },
  };
}
