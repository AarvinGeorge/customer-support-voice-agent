/**
 * What: tests for the voice relay (Realtime function call to POST voice/ask and back) over a
 *   mocked data channel and a fake fetch.
 * Use when: `pnpm test`.
 * Inputs: Realtime server events as the spike recorded them (D-045), the voice_turn fixture.
 * Output: Vitest results.
 * Side effects: none (fake timers, no network).
 * Role: CS-E-05.S3.T3 Done when: a late transcript, a duplicate call_id, a 26 s turn gives the
 *   fixed line; plus the function_call_output then response.create rules and the shared store.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SseEvent, VoiceAskRequest } from "@contracts/contracts";
import { loadFixture, toWire } from "@/test-support/fixtures";
import { createTurnStore } from "@/lib/turns/store";

import { RELAY_FAILURE_SAY, createVoiceRelay, type RealtimeEvent, type RelayDeps } from "./relay";
import type { VoiceEvent } from "./machine";

const voice = loadFixture("voice_turn.jsonl");
const SID = voice[0]?.session_id as string;
const SAY = "Refunds reach your card in five to ten business days, so yours should arrive by the eleventh of December.";

/**
 * What: a fake fetch that answers every POST with the voice_turn fixture stream.
 * Use when: the agent answers normally.
 * Inputs: optional events to stream instead.
 * Output: a vi.fn fetch.
 * Side effects: none.
 * Role: test helper.
 */
function sseFetch(events: SseEvent[] = voice) {
  return vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
    new Response(toWire(events), { status: 200, headers: { "content-type": "text/event-stream" } }),
  );
}

/**
 * What: a fake fetch whose stream sends turn.started and then nothing until aborted.
 * Use when: the 26 s turn test.
 * Inputs: none.
 * Output: a vi.fn fetch.
 * Side effects: none.
 * Role: test helper.
 */
function stallingFetch() {
  return vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    const first = toWire(voice.slice(0, 1));
    const body = new ReadableStream<Uint8Array>({
      start(ctrl) {
        ctrl.enqueue(new TextEncoder().encode(first));
        init?.signal?.addEventListener("abort", () => ctrl.error(new DOMException("aborted", "AbortError")));
      },
    });
    return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
  });
}

/**
 * What: builds a relay over a fresh store, recording what it sends on the data channel.
 * Use when: each test.
 * Inputs: the fetch to use and optional overrides.
 * Output: { relay, store, sent, machine, captions, fetchMock }.
 * Side effects: none.
 * Role: test harness (the "mocked data channel").
 */
function harness(fetchMock = sseFetch(), over: Partial<RelayDeps> = {}) {
  const store = createTurnStore();
  const sent: Array<Record<string, unknown>> = [];
  const machine: VoiceEvent[] = [];
  const captions: Array<string | null> = [];
  const relay = createVoiceRelay({
    sessionId: SID,
    voiceSessionId: "vs-1",
    store,
    send: (msg) => sent.push(msg),
    onMachine: (e) => machine.push(e),
    onCaption: (c) => captions.push(c === null ? null : `${c.final ? "final" : "interim"}:${c.text}`),
    deps: { fetch: fetchMock as unknown as typeof fetch },
    ...over,
  });
  return { relay, store, sent, machine, captions, fetchMock };
}

/**
 * What: the events of one spoken question, up to the response that carries the function call.
 * Use when: driving the relay like the spike's run1.
 * Inputs: the user item id, the response id, the call id, and whether to include the final transcript.
 * Output: RealtimeEvent[].
 * Side effects: none.
 * Role: test data from SPIKE_NOTE s3 and s4 (order: committed, response.created, done).
 */
function question(item: string, response: string): RealtimeEvent[] {
  return [
    { type: "input_audio_buffer.speech_started", item_id: item },
    { type: "conversation.item.input_audio_transcription.delta", item_id: item, delta: "When will " },
    { type: "conversation.item.input_audio_transcription.delta", item_id: item, delta: "it arrive?" },
    { type: "input_audio_buffer.speech_stopped", item_id: item },
    { type: "input_audio_buffer.committed", item_id: item },
    { type: "response.created", response: { id: response, status: "in_progress", output: [] } },
  ];
}

/**
 * What: a response.done event with one ask_support_agent call.
 * Use when: the function call completes (D-045: act on response.done, not arguments.done).
 * Inputs: response id, call id, status, question.
 * Output: a RealtimeEvent.
 * Side effects: none.
 * Role: test data.
 */
function callDone(response: string, callId: string, status = "completed", q = "When will my refund arrive?"): RealtimeEvent {
  return {
    type: "response.done",
    response: {
      id: response,
      status,
      output: [
        { type: "message", id: "item_hold", role: "assistant" },
        { type: "function_call", id: "item_fc", name: "ask_support_agent", call_id: callId, arguments: JSON.stringify({ question: q }) },
      ],
    },
  };
}

/**
 * What: the voice/ask bodies the fake fetch received.
 * Use when: asserting what was posted.
 * Inputs: the fetch mock.
 * Output: { url, body }[].
 * Side effects: none.
 * Role: test helper.
 */
function posted(fetchMock: ReturnType<typeof sseFetch>): Array<{ url: string; body: VoiceAskRequest }> {
  return fetchMock.mock.calls.map(([url, init]) => ({ url: String(url), body: JSON.parse(String((init as RequestInit).body)) as VoiceAskRequest }));
}

/**
 * What: the function_call_output payload sent for a call id.
 * Use when: asserting what Realtime will speak.
 * Inputs: sent messages, call id.
 * Output: the parsed output object or undefined.
 * Side effects: none.
 * Role: test helper.
 */
function outputFor(sent: Array<Record<string, unknown>>, callId: string): Record<string, unknown> | undefined {
  const msg = sent.find((m) => m.type === "conversation.item.create" && (m.item as { call_id?: string }).call_id === callId);
  return msg === undefined ? undefined : (JSON.parse((msg.item as { output: string }).output) as Record<string, unknown>);
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("voice relay", () => {
  it("relays a function call to voice/ask with the final transcript, then sends function_call_output and response.create", async () => {
    const { relay, store, sent, fetchMock, machine } = harness();
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "When will it arrive?" });
    relay.handle({ type: "response.function_call_arguments.done", call_id: "call_1", name: "ask_support_agent", arguments: "{}" });
    relay.handle(callDone("resp_1", "call_1"));
    await relay.settled();

    const [post] = posted(fetchMock);
    expect(post?.url).toBe(`/api/sessions/${SID}/voice/ask`);
    expect(post?.body).toMatchObject({
      voice_session_id: "vs-1",
      realtime_call_id: "call_1",
      question: "When will my refund arrive?",
      user_item_id: "item_u1",
      user_transcript: "When will it arrive?",
      transcript_final: true,
      last_assistant_interrupted: false,
    });
    expect(post?.body.client_turn_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(sent.map((m) => m.type)).toEqual(["conversation.item.create", "response.create"]);
    expect(sent[0]?.item).toMatchObject({ type: "function_call_output", call_id: "call_1" });
    expect(outputFor(sent, "call_1")).toEqual({ say: SAY, escalated: false });
    expect(machine).toContain("tool_call");

    const st = store.getState();
    expect(st.thread.map((i) => (i.kind === "user" ? `user:${i.mode}:${i.text}` : i.kind))).toEqual(["user:voice:When will it arrive?", "turn"]);
    const turn = Object.values(st.conversation.turns)[0];
    expect(turn?.mode).toBe("voice");
    expect(turn?.phase).toBe("completed");
  });

  it("a late transcript (1.0 s after the call) is waited for and sent as final", async () => {
    const { relay, fetchMock } = harness();
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle(callDone("resp_1", "call_1"));
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchMock).not.toHaveBeenCalled();
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "When will it arrive?" });
    await relay.settled();
    expect(posted(fetchMock)[0]?.body).toMatchObject({ user_item_id: "item_u1", user_transcript: "When will it arrive?", transcript_final: true });
  });

  it("a transcript later than 1.5 s is not waited for: the post goes at 1.5 s with transcript_final false", async () => {
    const { relay, fetchMock } = harness();
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle(callDone("resp_1", "call_1"));
    await vi.advanceTimersByTimeAsync(1499);
    expect(fetchMock).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await relay.settled();
    expect(posted(fetchMock)[0]?.body).toMatchObject({ user_item_id: "item_u1", user_transcript: null, transcript_final: false });
  });

  it("a duplicate call_id (response.done seen twice) posts once and answers once", async () => {
    const { relay, sent, fetchMock } = harness();
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "When will it arrive?" });
    relay.handle(callDone("resp_1", "call_1"));
    relay.handle(callDone("resp_1", "call_1"));
    await relay.settled();
    relay.handle(callDone("resp_1", "call_1"));
    await relay.settled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(sent.filter((m) => m.type === "conversation.item.create")).toHaveLength(1);
    expect(sent.filter((m) => m.type === "response.create")).toHaveLength(1);
  });

  it("a 26 s turn yields the fixed line and the turn ends failed (failTurn), never a retry", async () => {
    const fetchMock = stallingFetch();
    const { relay, sent, store } = harness(fetchMock as unknown as ReturnType<typeof sseFetch>);
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "When will it arrive?" });
    relay.handle(callDone("resp_1", "call_1"));
    await vi.advanceTimersByTimeAsync(24_000);
    expect(outputFor(sent, "call_1")).toBeUndefined();
    await vi.advanceTimersByTimeAsync(2_000);
    await relay.settled();
    expect(RELAY_FAILURE_SAY).toBe("I couldn't finish that just now. Please check the chat for the result before trying again.");
    expect(outputFor(sent, "call_1")).toEqual({ say: RELAY_FAILURE_SAY, escalated: false });
    expect(sent.at(-1)).toEqual({ type: "response.create" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const turn = Object.values(store.getState().conversation.turns)[0];
    expect(turn?.phase).toBe("failed");
    expect(turn?.failure?.reason).toBe("turn_timeout");
  });

  it("an error answer (503) also yields the fixed line", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ error: { class: "provider_5xx", code: "VOICE_UNAVAILABLE", message: "Voice is unavailable.", retryable: true, fault_injected: false, changed: null, details: {} } }), {
        status: 503,
        headers: { "content-type": "application/json" },
      }),
    );
    const { relay, sent } = harness(fetchMock as unknown as ReturnType<typeof sseFetch>);
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "x" });
    relay.handle(callDone("resp_1", "call_1"));
    await relay.settled();
    expect(outputFor(sent, "call_1")).toEqual({ say: RELAY_FAILURE_SAY, escalated: false });
  });

  it("maps ticket and awaiting into function_call_output", async () => {
    const ticket = { ticket_id: "TCK-26118214", short_id: "8214", display: "ticket ending 8214", stage: "open", priority: "P3", queue: "account", target_label: "x", verified: true };
    const events = voice.map((e) =>
      e.type === "turn.completed" ? ({ ...e, end_reason: "verification_requested", final: { ...e.final, escalated: true, ticket } } as SseEvent) : e,
    );
    const { relay, sent } = harness(sseFetch(events));
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "x" });
    relay.handle(callDone("resp_1", "call_1"));
    await relay.settled();
    expect(outputFor(sent, "call_1")).toEqual({ say: SAY, escalated: true, ticket: { spoken: "ticket ending 8214", full_id: "TCK-26118214" }, awaiting: "verification" });
  });

  it("acts only on a completed response.done: a cancelled response's call is not relayed", async () => {
    const { relay, fetchMock, sent } = harness();
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle(callDone("resp_1", "call_1", "cancelled"));
    await vi.advanceTimersByTimeAsync(2000);
    await relay.settled();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it("two calls in one response: both outputs first, then a single response.create", async () => {
    const { relay, sent } = harness();
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "x" });
    const done = callDone("resp_1", "call_1");
    const out = (done.response as { output: unknown[] }).output;
    out.push({ type: "function_call", id: "item_fc2", name: "ask_support_agent", call_id: "call_2", arguments: JSON.stringify({ question: "And the invoice?" }) });
    relay.handle(done);
    await relay.settled();
    expect(sent.map((m) => m.type)).toEqual(["conversation.item.create", "conversation.item.create", "response.create"]);
  });

  it("barge-in on the agent's say sets last_assistant_interrupted on the next relay", async () => {
    const { relay, fetchMock } = harness();
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "x" });
    relay.handle(callDone("resp_1", "call_1"));
    await relay.settled();
    relay.handle({ type: "response.created", response: { id: "resp_say", status: "in_progress", output: [] } });
    relay.handle({ type: "output_audio_buffer.started", response_id: "resp_say" });
    for (const e of question("item_u2", "resp_2")) relay.handle(e);
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u2", transcript: "y" });
    relay.handle(callDone("resp_2", "call_2"));
    await relay.settled();
    expect(posted(fetchMock).map((p) => p.body.last_assistant_interrupted)).toEqual([false, true]);
  });

  it("shows an interim caption from transcription deltas and the final text on completion", () => {
    const { relay, captions } = harness();
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "When will it arrive?" });
    expect(captions).toEqual([null, "interim:When will ", "interim:When will it arrive?", "final:When will it arrive?"]);
  });

  it("after close nothing more is sent on the channel", async () => {
    const { relay, sent } = harness();
    for (const e of question("item_u1", "resp_1")) relay.handle(e);
    relay.handle({ type: "conversation.item.input_audio_transcription.completed", item_id: "item_u1", transcript: "x" });
    relay.handle(callDone("resp_1", "call_1"));
    relay.close();
    await relay.settled();
    expect(sent).toEqual([]);
  });
});
