/**
 * What: tests for live voice transcripts and interruption in the relay: user and assistant items
 *   keyed by item_id in the shared thread, barge-in and Stop speaking (audio cleared within
 *   0.5 s, the bubble frozen at what was heard, last_assistant_interrupted on the next relay, the
 *   backend turn never cancelled), and POST /transcript of finished items.
 * Use when: `pnpm test`.
 * Inputs: Realtime server events as the spike recorded them (SPIKE_NOTE s3 to s5), the
 *   voice_turn fixture.
 * Output: Vitest results.
 * Side effects: none (fake timers and clock, no network).
 * Role: CS-E-05.S4.T3 Done when: interrupting a read-back stops audio within 0.5 s (timestamps
 *   from the mocked clock) and the next relay carries last_assistant_interrupted=true.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SseEvent, TranscriptPost, VoiceAskRequest } from "@contracts/contracts";
import { loadFixture, toWire } from "@/test-support/fixtures";
import { createTurnStore, type ThreadItem } from "@/lib/turns/store";

import { CHARS_PER_SECOND, createVoiceRelay, heardText, type RealtimeEvent, type RelayDeps } from "./relay";

const voice = loadFixture("voice_turn.jsonl");
const SID = voice[0]?.session_id as string;
const TURN_ID = voice[0]?.turn_id as string;
const READBACK = "I can refund ninety six dollars for the duplicate charge on the twenty first of November. Shall I go ahead?";

/**
 * What: a fake fetch that answers voice/ask with the fixture stream, or stalls until aborted.
 * Use when: each test.
 * Inputs: stall (true: turn.started then nothing).
 * Output: { fetchMock, signals } (the abort signal of every request).
 * Side effects: none.
 * Role: test helper.
 */
function fakeFetch(stall = false) {
  const signals: AbortSignal[] = [];
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.signal) signals.push(init.signal);
    if (!stall) return new Response(toWire(voice as SseEvent[]), { status: 200, headers: { "content-type": "text/event-stream" } });
    const body = new ReadableStream<Uint8Array>({
      start(ctrl) {
        ctrl.enqueue(new TextEncoder().encode(toWire(voice.slice(0, 1) as SseEvent[])));
        init?.signal?.addEventListener("abort", () => ctrl.error(new DOMException("aborted", "AbortError")));
      },
    });
    return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
  });
  return { fetchMock, signals };
}

/**
 * What: a relay over a fresh store that timestamps every data channel send with the fake clock.
 * Use when: each test.
 * Inputs: stall, overrides.
 * Output: { relay, store, sent, posts, agentLines, fetchMock, signals }.
 * Side effects: none.
 * Role: test harness (the mocked data channel).
 */
function harness(stall = false, over: Partial<RelayDeps> = {}) {
  const store = createTurnStore();
  const sent: Array<{ at: number; msg: Record<string, unknown> }> = [];
  const posts: TranscriptPost[] = [];
  const agentLines: Array<string | null> = [];
  const { fetchMock, signals } = fakeFetch(stall);
  const relay = createVoiceRelay({
    sessionId: SID,
    voiceSessionId: "vs-1",
    store,
    send: (msg) => sent.push({ at: Date.now(), msg }),
    onAgentLine: (t) => agentLines.push(t),
    postTranscript: async (body) => {
      posts.push(body);
    },
    deps: { fetch: fetchMock as unknown as typeof fetch },
    ...over,
  });
  return { relay, store, sent, posts, agentLines, fetchMock, signals };
}

/**
 * What: one spoken question and its completed function call response.
 * Use when: starting a relay turn.
 * Inputs: the user item id, response id, call id, the transcript.
 * Output: RealtimeEvent[].
 * Side effects: none.
 * Role: test data (SPIKE_NOTE s3, s4).
 */
function ask(item: string, response: string, callId: string, transcript = "Can you refund the duplicate charge?"): RealtimeEvent[] {
  return [
    { type: "input_audio_buffer.speech_started", item_id: item },
    { type: "input_audio_buffer.speech_stopped", item_id: item },
    { type: "input_audio_buffer.committed", item_id: item },
    { type: "conversation.item.input_audio_transcription.completed", item_id: item, transcript },
    { type: "response.created", response: { id: response, status: "in_progress", output: [] } },
    {
      type: "response.done",
      response: { id: response, status: "completed", output: [{ type: "function_call", id: `fc_${callId}`, name: "ask_support_agent", call_id: callId, arguments: JSON.stringify({ question: transcript }) }] },
    },
  ];
}

/**
 * What: the say response that reads the agent's answer back, with its audio transcript deltas.
 * Use when: after the relay answered.
 * Inputs: response id, assistant item id, the text, the say tag the relay put in response.create
 *   metadata (absent for speech that is not a say).
 * Output: RealtimeEvent[] (response.created, deltas by word, output_audio_buffer.started).
 * Side effects: none.
 * Role: test data (GA event names response.output_audio_transcript.delta / .done).
 */
function sayStarts(response: string, item: string, text: string, tag?: string): RealtimeEvent[] {
  const words = text.split(/(?<= )/);
  return [
    { type: "response.created", response: { id: response, status: "in_progress", output: [], ...(tag ? { metadata: { qs_say: tag } } : {}) } },
    ...words.map((w) => ({ type: "response.output_audio_transcript.delta", response_id: response, item_id: item, delta: w })),
    { type: "output_audio_buffer.started", response_id: response },
  ];
}

/**
 * What: the say tag of the last response.create the relay sent.
 * Use when: playing the say response back to the relay.
 * Inputs: the sent log.
 * Output: the tag, or undefined.
 * Side effects: none.
 * Role: helper (review PR #46 M2: the say is matched by response metadata).
 */
const tagOf = (sent: Array<{ msg: Record<string, unknown> }>) =>
  ((sent.filter((s) => s.msg.type === "response.create").at(-1)?.msg.response as { metadata?: { qs_say?: string } } | undefined)?.metadata?.qs_say);

/** What: voice/ask bodies. Use when: asserting relays. Inputs: fetch mock. Output: bodies. Side effects: none. Role: helper. */
const asks = (f: ReturnType<typeof fakeFetch>["fetchMock"]) => f.mock.calls.map(([, init]) => JSON.parse(String((init as RequestInit).body)) as VoiceAskRequest);

/** What: the assistant voice items of the thread. Use when: asserting bubbles. Inputs: thread. Output: items. Side effects: none. Role: helper. */
const agentItems = (thread: readonly ThreadItem[]) => thread.filter((i): i is Extract<ThreadItem, { kind: "agent-voice" }> => i.kind === "agent-voice");

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-11-27T15:00:00Z"));
});
afterEach(() => {
  vi.useRealTimers();
});

describe("heardText", () => {
  it("cuts at a word boundary after the words spoken in the heard time", () => {
    const t = "Your refund of $96.00 was issued on 26 November.";
    expect(heardText(t, 0)).toBe("");
    expect(heardText(t, 1000)).toBe(t.slice(0, CHARS_PER_SECOND).replace(/\s+\S*$/, ""));
    expect(heardText(t, 60_000)).toBe(t);
  });
});

describe("interruption of a read-back", () => {
  it("barge-in clears agent audio within 0.5 s, freezes the bubble at what was heard, and the next relay carries last_assistant_interrupted=true", async () => {
    const { relay, store, sent, fetchMock } = harness();
    for (const e of ask("item_u1", "resp_1", "call_1")) relay.handle(e);
    await relay.settled();
    for (const e of sayStarts("resp_say", "item_a1", READBACK, tagOf(sent))) relay.handle(e);
    await vi.advanceTimersByTimeAsync(2000);

    const bargeAt = Date.now();
    relay.handle({ type: "input_audio_buffer.speech_started", item_id: "item_u2" });
    const clear = sent.find((s) => s.msg.type === "output_audio_buffer.clear");
    expect(clear).toBeDefined();
    expect((clear as { at: number }).at - bargeAt).toBeLessThanOrEqual(500);

    const [bubble] = agentItems(store.getState().thread);
    expect(bubble).toMatchObject({ itemId: "item_a1", interrupted: true, turnId: TURN_ID, text: READBACK });
    expect(bubble?.heard).toBe(heardText(READBACK, 2000));
    expect(READBACK.startsWith(bubble?.heard ?? "x")).toBe(true);
    expect(bubble?.heard.length).toBeLessThan(READBACK.length);

    // The server's truncation names what was actually played; the bubble follows it.
    relay.handle({ type: "conversation.item.truncated", item_id: "item_a1", content_index: 0, audio_end_ms: 1560 });
    relay.handle({ type: "response.output_audio_transcript.done", response_id: "resp_say", item_id: "item_a1", transcript: READBACK });
    const [after] = agentItems(store.getState().thread);
    expect(after?.heard).toBe(heardText(READBACK, 1560));
    expect(after?.interrupted).toBe(true);

    for (const e of ask("item_u2", "resp_2", "call_2", "Actually, wait")) relay.handle(e);
    await relay.settled();
    expect(asks(fetchMock).map((b) => b.last_assistant_interrupted)).toEqual([false, true]);

  });

  it("Stop speaking clears audio only: an in-flight backend turn is not aborted, and the bubble freezes", async () => {
    const { relay, store, sent, signals } = harness(true);
    for (const e of ask("item_u1", "resp_1", "call_1")) relay.handle(e);
    for (const e of sayStarts("resp_hold", "item_h1", "One moment while I check that for you.")) relay.handle(e);
    await vi.advanceTimersByTimeAsync(800);
    expect(signals).toHaveLength(1);

    relay.stopSpeaking();
    expect(sent.map((s) => s.msg.type)).toEqual(["output_audio_buffer.clear"]);
    expect(signals[0]?.aborted).toBe(false);
    const [hold] = agentItems(store.getState().thread);
    expect(hold).toMatchObject({ itemId: "item_h1", interrupted: true, turnId: null });
    // A barge-in on the next holding phrase does not abort the backend turn either.
    for (const e of sayStarts("resp_hold2", "item_h2", "Still checking.")) relay.handle(e);
    relay.handle({ type: "input_audio_buffer.speech_started", item_id: "item_u2" });
    expect(sent.map((s) => s.msg.type)).toEqual(["output_audio_buffer.clear", "output_audio_buffer.clear"]);
    expect(signals[0]?.aborted).toBe(false);
    relay.close();
  });

  it("speech while the agent is silent sends no clear and freezes nothing", () => {
    const { relay, sent, store } = harness();
    relay.handle({ type: "input_audio_buffer.speech_started", item_id: "item_u1" });
    expect(sent).toEqual([]);
    expect(agentItems(store.getState().thread)).toEqual([]);
  });
});

describe("live transcripts", () => {
  it("a finished say becomes one assistant item keyed by item_id and linked to its turn; the dock gets the live line", async () => {
    const { relay, store, agentLines, sent } = harness();
    for (const e of ask("item_u1", "resp_1", "call_1")) relay.handle(e);
    await relay.settled();
    for (const e of sayStarts("resp_say", "item_a1", "Refunds reach your card in five to ten business days.", tagOf(sent))) relay.handle(e);
    expect(agentLines.at(-1)).toBe("Refunds reach your card in five to ten business days.");
    relay.handle({ type: "response.output_audio_transcript.done", response_id: "resp_say", item_id: "item_a1", transcript: "Refunds reach your card in five to ten business days." });
    relay.handle({ type: "output_audio_buffer.stopped", response_id: "resp_say" });
    expect(agentLines.at(-1)).toBeNull();
    const thread = store.getState().thread;
    expect(thread.map((i) => i.kind)).toEqual(["user", "turn", "agent-voice"]);
    expect(thread[0]).toMatchObject({ kind: "user", id: "voice-item_u1", mode: "voice" });
    expect(agentItems(thread)[0]).toMatchObject({ id: "voice-item_a1", interrupted: false, turnId: TURN_ID, heard: "Refunds reach your card in five to ten business days." });
  });

  it("a user final seen twice is one bubble", () => {
    const { relay, store } = harness();
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "Hello" });
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "Hello" });
    expect(store.getState().thread).toHaveLength(1);
  });

  it("posts finished items to /transcript in one batch (user final, assistant heard text with interrupted) and flushes on close", async () => {
    const { relay, posts } = harness();
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "Hello" });
    for (const e of sayStarts("resp_h", "item_h1", "Hi, how can I help with your Quillstack account today?")) relay.handle(e);
    await vi.advanceTimersByTimeAsync(1000);
    relay.handle({ type: "input_audio_buffer.speech_started", item_id: "item_u2" });
    relay.handle({ type: "conversation.item.truncated", item_id: "item_h1", audio_end_ms: 1000 });
    expect(posts).toEqual([]);
    await vi.advanceTimersByTimeAsync(1500);
    expect(posts).toHaveLength(1);
    expect(posts[0]?.voice_session_id).toBe("vs-1");
    expect(posts[0]?.items.map(({ client_ts: _ts, ...rest }) => rest)).toEqual([
      { realtime_item_id: "item_u1", role: "user", text: "Hello", interrupted: false },
      { realtime_item_id: "item_h1", role: "assistant", text: heardText("Hi, how can I help with your Quillstack account today?", 1000), interrupted: true },
    ]);
    expect(posts[0]?.items[0]?.client_ts).toBe("2026-11-27T15:00:00.000Z");

    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u3", transcript: "Bye" });
    relay.close();
    expect(posts).toHaveLength(2);
    expect(posts[1]?.items.map((i) => i.realtime_item_id)).toEqual(["item_u3"]);
  });
});
