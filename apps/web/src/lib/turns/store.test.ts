/**
 * What: tests for the Zustand turn store (reducer wiring, sendTurn, the one-shot demo fault).
 * Use when: `pnpm test`.
 * Inputs: a fake fetch that streams fixture turns.
 * Output: pass or fail per rule.
 * Side effects: none (no network).
 * Role: proves the store half of CS-E-04.S4.T2 and the doc 07 s10 "next request only" rule.
 */
import { describe, expect, it, vi } from "vitest";
import type { TurnRequest } from "@contracts/contracts";
import { loadFixture, toWire } from "@/test-support/fixtures";
import { ApiError, NetworkError } from "@/lib/api/client";
import { createTurnStore } from "./store";
import { replayEvents } from "./reducer";

const s1 = loadFixture("s1_chat.jsonl");
const turnIds = [...new Set(s1.map((e) => e.turn_id))];
const SID = s1[0]?.session_id as string;
/**
 * What: a text TurnRequest with a distinct client_turn_id.
 * Use when: sendTurn tests.
 * Inputs: n (0..9).
 * Output: a TurnRequest.
 * Side effects: none.
 * Role: test helper.
 */
const req = (n: number): TurnRequest => ({ client_turn_id: `6f1d2c1e-0000-4000-8000-00000000000${n}`, text: `message ${n}` });

/**
 * What: a fake fetch that streams the s1 fixture turns in order, one per call.
 * Use when: sendTurn tests.
 * Inputs: none.
 * Output: a vi.fn fetch.
 * Side effects: none.
 * Role: test helper.
 */
function fixtureFetch() {
  let call = 0;
  return vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => {
    const id = turnIds[call++];
    const events = s1.filter((e) => e.turn_id === id);
    return new Response(toWire(events), { status: 200, headers: { "content-type": "text/event-stream" } });
  });
}

/**
 * What: reads the X-Demo-Fault header of fetch call n.
 * Use when: one-shot fault tests.
 * Inputs: the fetch mock, n.
 * Output: the header value or null.
 * Side effects: none.
 * Role: test helper.
 */
function faultHeader(fetchMock: ReturnType<typeof fixtureFetch>, n: number): string | null {
  const init = fetchMock.mock.calls[n]?.[1] as RequestInit;
  return new Headers(init.headers).get("x-demo-fault");
}

describe("turn store", () => {
  it("sendTurn streams events through the reducer; state equals a direct replay", async () => {
    const store = createTurnStore();
    const fetchMock = fixtureFetch();
    for (let i = 0; i < turnIds.length; i++) {
      const out = await store.getState().sendTurn(SID, req(i), { deps: { fetch: fetchMock as unknown as typeof fetch } });
      expect(out.turnId).toBe(turnIds[i]);
    }
    const live = store.getState().conversation;
    const stamped = Object.values(live.turns).filter((t) => t.confirmationReceivedAt !== null);
    expect(stamped.map((t) => t.confirmation?.pending.pending_action_id)).toEqual(["PA-3F2A9C1B"]);
    expect(typeof stamped[0]?.confirmationReceivedAt).toBe("number");
    const unstamped = Object.fromEntries(Object.entries(live.turns).map(([id, t]) => [id, { ...t, confirmationReceivedAt: null }]));
    expect({ ...live, turns: unstamped }).toEqual(replayEvents(s1));
    expect(store.getState().connection).toEqual({ phase: "idle", error: null });
  });

  it("shows phase streaming while a turn is open", async () => {
    const store = createTurnStore();
    const phases: string[] = [];
    store.subscribe((st) => phases.push(st.connection.phase));
    await store.getState().sendTurn(SID, req(0), { deps: { fetch: fixtureFetch() as unknown as typeof fetch } });
    expect(phases[0]).toBe("streaming");
    expect(phases.at(-1)).toBe("idle");
  });

  it("sends an armed fault on exactly one request, then clears it", async () => {
    const store = createTurnStore();
    const fetchMock = fixtureFetch();
    store.getState().armFault("tool_error");
    expect(store.getState().armedFault).toBe("tool_error");
    await store.getState().sendTurn(SID, req(0), { deps: { fetch: fetchMock as unknown as typeof fetch } });
    expect(store.getState().armedFault).toBeNull();
    await store.getState().sendTurn(SID, req(1), { deps: { fetch: fetchMock as unknown as typeof fetch } });
    expect([faultHeader(fetchMock, 0), faultHeader(fetchMock, 1)]).toEqual(["tool_error", null]);
  });

  it("remembers which fault the last request carried, so the toggle can say it switched off", async () => {
    const store = createTurnStore();
    const fetchMock = fixtureFetch();
    store.getState().armFault("llm_429");
    await store.getState().sendTurn(SID, req(0), { deps: { fetch: fetchMock as unknown as typeof fetch } });
    expect(store.getState().lastFault).toBe("llm_429");
    await store.getState().sendTurn(SID, req(1), { deps: { fetch: fetchMock as unknown as typeof fetch } });
    expect(store.getState().lastFault).toBeNull();
  });

  it("clears the armed fault even when the request fails before the stream opens", async () => {
    const store = createTurnStore();
    store.getState().armFault("llm_429");
    const body = { error: { class: "rate_limited", code: "SESSION_TURNS", message: "m", retryable: true, changed: false, retry_after_s: 5, fault_injected: false, details: {} } };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 429, headers: { "content-type": "application/json" } }));
    await expect(store.getState().sendTurn(SID, req(0), { deps: { fetch: fetchMock as unknown as typeof fetch } })).rejects.toBeInstanceOf(ApiError);
    expect(store.getState().armedFault).toBeNull();
    expect(store.getState().connection.phase).toBe("idle");
    expect(store.getState().connection.error).toBeInstanceOf(ApiError);
  });

  it("keeps the events received before a drop and records the typed error", async () => {
    const store = createTurnStore();
    const partial = s1.filter((e) => e.turn_id === turnIds[0]).slice(0, 4);
    const fetchMock = vi.fn(async () => new Response(toWire(partial), { status: 200, headers: { "content-type": "text/event-stream" } }));
    await expect(store.getState().sendTurn(SID, req(0), { deps: { fetch: fetchMock as unknown as typeof fetch } })).rejects.toBeInstanceOf(NetworkError);
    const conv = store.getState().conversation;
    expect(conv.turns[turnIds[0] as string]?.lastSeq).toBe(4);
    expect(conv.turns[turnIds[0] as string]?.phase).toBe("failed");
    expect(conv.turns[turnIds[0] as string]?.failure).toEqual({ reason: "dropped", retryable: true, message: "The connection dropped before this reply finished." });
    expect((store.getState().connection.error as NetworkError).reason).toBe("dropped");
  });

  it("an aborted turn ends in the failed state, not streaming (review PR #16, I1)", async () => {
    const store = createTurnStore();
    const partial = s1.filter((e) => e.turn_id === turnIds[0]).slice(0, 3);
    const ctrl = new AbortController();
    const fetchMock = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      const enc = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({
        start(c) {
          c.enqueue(enc.encode(toWire(partial)));
          init?.signal?.addEventListener("abort", () => c.error(new DOMException("aborted", "AbortError")));
        },
      });
      return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
    });
    const p = store.getState().sendTurn(SID, req(0), { signal: ctrl.signal, deps: { fetch: fetchMock as unknown as typeof fetch } });
    await vi.waitFor(() => expect(store.getState().conversation.turns[turnIds[0] as string]?.lastSeq).toBe(3));
    ctrl.abort();
    await expect(p).rejects.toBeInstanceOf(NetworkError);
    const t = store.getState().conversation.turns[turnIds[0] as string];
    expect(t?.phase).toBe("failed");
    expect(t?.failure?.reason).toBe("aborted");
  });

  it("a watchdog expiry ends the turn in the failed state with its reason", async () => {
    const store = createTurnStore();
    const partial = s1.filter((e) => e.turn_id === turnIds[0]).slice(0, 2);
    const fetchMock = vi.fn(async () => {
      const enc = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({ start(c) { c.enqueue(enc.encode(toWire(partial))); } });
      return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
    });
    await expect(store.getState().sendTurn(SID, req(0), { noBytesMs: 30, deps: { fetch: fetchMock as unknown as typeof fetch } })).rejects.toBeInstanceOf(NetworkError);
    const t = store.getState().conversation.turns[turnIds[0] as string];
    expect(t?.phase).toBe("failed");
    expect(t?.failure).toEqual({ reason: "no_bytes_timeout", retryable: true, message: "The reply stopped arriving." });
  });

  it("the failed state survives the next sendTurn clearing connection.error", async () => {
    const store = createTurnStore();
    const partial = s1.filter((e) => e.turn_id === turnIds[0]).slice(0, 4);
    let call = 0;
    const fetchMock = vi.fn(async () => {
      const events = call++ === 0 ? partial : s1.filter((e) => e.turn_id === turnIds[1]);
      return new Response(toWire(events), { status: 200, headers: { "content-type": "text/event-stream" } });
    });
    const deps = { fetch: fetchMock as unknown as typeof fetch };
    await expect(store.getState().sendTurn(SID, req(0), { deps })).rejects.toBeInstanceOf(NetworkError);
    await store.getState().sendTurn(SID, req(1), { deps });
    expect(store.getState().connection.error).toBeNull();
    expect(store.getState().conversation.turns[turnIds[0] as string]?.phase).toBe("failed");
  });

  it("sendTurn with text adds a chat user item before the turn item in the thread", async () => {
    const store = createTurnStore();
    await store.getState().sendTurn(SID, req(0), { deps: { fetch: fixtureFetch() as unknown as typeof fetch } });
    const thread = store.getState().thread;
    expect(thread.map((i) => i.kind)).toEqual(["user", "turn"]);
    expect(thread[0]).toMatchObject({ kind: "user", text: "message 0", mode: "chat" });
    expect(thread[1]).toEqual({ kind: "turn", id: turnIds[0], turnId: turnIds[0] });
  });

  it("addUserMessage and apply build one unified thread for chat and voice", () => {
    const store = createTurnStore();
    const voice = loadFixture("voice_turn.jsonl").map((e) => ({ ...e, session_id: SID }));
    store.getState().addUserMessage("refund please", "chat");
    for (const e of s1.filter((e) => e.turn_id === turnIds[0])) store.getState().apply(e);
    store.getState().addUserMessage("when will it arrive", "voice");
    for (const e of voice) store.getState().apply(e);
    const thread = store.getState().thread;
    expect(thread.map((i) => (i.kind === "user" ? `user:${i.mode}` : "turn"))).toEqual(["user:chat", "turn", "user:voice", "turn"]);
    store.getState().reset();
    expect(store.getState().thread).toEqual([]);
  });

  it("reset returns to the initial conversation and keeps nothing armed", () => {
    const store = createTurnStore();
    for (const e of s1) store.getState().apply(e);
    store.getState().armFault("tool_error");
    store.getState().reset();
    expect(store.getState().conversation.turnOrder).toEqual([]);
    expect(store.getState().armedFault).toBeNull();
  });

  it("disarming with null leaves no fault", () => {
    const store = createTurnStore();
    store.getState().armFault("tool_error");
    store.getState().armFault(null);
    expect(store.getState().takeFault()).toBeNull();
  });
});

/**
 * What: a fake fetch whose body the test feeds by hand, plus a flag for whether the request was aborted.
 * Use when: race tests that need events to land after a reset.
 * Inputs: none.
 * Output: { fetchMock, push(events), close(), aborted() }.
 * Side effects: none outside the returned objects.
 * Role: test helper.
 */
function manualStream() {
  const enc = new TextEncoder();
  let ctrl: ReadableStreamDefaultController<Uint8Array> | null = null;
  let signal: AbortSignal | null = null;
  const fetchMock = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
    signal = init?.signal ?? null;
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        ctrl = c;
        signal?.addEventListener("abort", () => c.error(new DOMException("aborted", "AbortError")));
      },
    });
    return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
  });
  return {
    fetchMock,
    push: (events: typeof s1) => {
      try {
        ctrl?.enqueue(enc.encode(toWire(events)));
      } catch {
        // the stream was aborted; a late chunk has nowhere to go
      }
    },
    aborted: () => (signal as AbortSignal | null)?.aborted ?? false,
  };
}

describe("session scoping and New conversation mid-reply (review PR #20, I1)", () => {
  it("reset aborts the in-flight send; its late events never enter the new conversation", async () => {
    const store = createTurnStore();
    const old = manualStream();
    const c2 = s1.filter((e) => e.turn_id === turnIds[1]);
    const p = store.getState().sendTurn(SID, req(0), { deps: { fetch: old.fetchMock as unknown as typeof fetch } });
    await vi.waitFor(() => expect(old.fetchMock).toHaveBeenCalled());
    old.push(c2.slice(0, 3));
    await vi.waitFor(() => expect(store.getState().conversation.turnOrder).toHaveLength(1));
    store.getState().reset();
    expect(old.aborted()).toBe(true);
    old.push(c2.slice(3));
    await p.catch(() => undefined);
    const st = store.getState();
    expect(st.conversation.turnOrder).toEqual([]);
    expect(st.conversation.sessionId).toBeNull();
    expect(st.thread).toEqual([]);
    expect(st.connection).toEqual({ phase: "idle", error: null });
  });

  it("after the reset a send in the new session renders its reply", async () => {
    const store = createTurnStore();
    const old = manualStream();
    const p = store.getState().sendTurn(SID, req(0), { deps: { fetch: old.fetchMock as unknown as typeof fetch } });
    await vi.waitFor(() => expect(old.fetchMock).toHaveBeenCalled());
    old.push(s1.filter((e) => e.turn_id === turnIds[1]).slice(0, 3));
    await vi.waitFor(() => expect(store.getState().conversation.turnOrder).toHaveLength(1));
    store.getState().reset();
    await p.catch(() => undefined);
    const NEW = "019a7c2e-9999-7000-8000-00000000a001";
    const fresh = s1.filter((e) => e.turn_id === turnIds[3]).map((e) => ({ ...e, session_id: NEW }));
    const fetchNew = vi.fn(async () => new Response(toWire(fresh), { status: 200, headers: { "content-type": "text/event-stream" } }));
    await store.getState().sendTurn(NEW, req(1), { deps: { fetch: fetchNew as unknown as typeof fetch } });
    const st = store.getState();
    expect(st.conversation.sessionId).toBe(NEW);
    expect(st.conversation.turns[turnIds[3] as string]?.phase).toBe("completed");
    expect(st.thread.map((i) => i.kind)).toEqual(["user", "turn"]);
  });

  it("drops events whose session_id is not the session the send was for", async () => {
    const store = createTurnStore();
    const foreign = s1.filter((e) => e.turn_id === turnIds[3]).map((e) => ({ ...e, session_id: "019a7c2e-0000-7000-8000-0000000bad00" }));
    const fetchMock = vi.fn(async () => new Response(toWire(foreign), { status: 200, headers: { "content-type": "text/event-stream" } }));
    await store.getState().sendTurn(SID, req(0), { deps: { fetch: fetchMock as unknown as typeof fetch } });
    expect(store.getState().conversation.turnOrder).toEqual([]);
    expect(store.getState().conversation.sessionId).toBeNull();
  });
});

describe("a send that fails before the first event (review PR #20, I2)", () => {
  it("a 429 shows no bubble or error row: it records a limit notice that keeps the draft (CS-E-07.S1.T4)", async () => {
    const store = createTurnStore();
    const body = { error: { class: "rate_limited", code: "SESSION_TURNS", message: "You're sending messages quickly. Please wait a moment.", retryable: true, changed: false, retry_after_s: 42, fault_injected: false, details: { limit: "10/minute", scope: "session" } } };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 429, headers: { "content-type": "application/json" } }));
    const before = Date.now();
    await expect(store.getState().sendTurn(SID, req(0), { deps: { fetch: fetchMock as unknown as typeof fetch } })).rejects.toBeInstanceOf(ApiError);
    expect(store.getState().thread).toEqual([]);
    const notice = store.getState().limit;
    expect(notice).toMatchObject({ status: 429, code: "SESSION_TURNS", retryAfterS: 42, limit: "10/minute", scope: "session", draft: "message 0" });
    expect(notice?.untilMs).toBeGreaterThanOrEqual(before + 42_000);
    store.getState().clearLimit();
    expect(store.getState().limit).toBeNull();
  });

  it("a 503 BUSY records a notice with its Retry-After, and the next accepted send clears it", async () => {
    const store = createTurnStore();
    const body = { error: { class: "admission_rejected", code: "BUSY", message: "We're busy right now.", retryable: true, changed: false, retry_after_s: 3, fault_injected: false, details: {} } };
    const busy = vi.fn(async () => new Response(JSON.stringify(body), { status: 503, headers: { "content-type": "application/json", "retry-after": "3" } }));
    await store.getState().sendTurn(SID, req(0), { deps: { fetch: busy as unknown as typeof fetch } }).catch(() => undefined);
    expect(store.getState().limit).toMatchObject({ status: 503, code: "BUSY", retryAfterS: 3, draft: "message 0" });
    await store.getState().sendTurn(SID, req(1), { deps: { fetch: fixtureFetch() as unknown as typeof fetch } });
    expect(store.getState().limit).toBeNull();
  });

  it("other pre-stream API errors still add a retryable error row after the user item", async () => {
    const store = createTurnStore();
    const body = { error: { class: "core_conflict", code: "TURN_IN_PROGRESS", message: "A reply is still being written.", retryable: true, changed: false, retry_after_s: null, fault_injected: false, details: {} } };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 409, headers: { "content-type": "application/json" } }));
    await expect(store.getState().sendTurn(SID, req(0), { deps: { fetch: fetchMock as unknown as typeof fetch } })).rejects.toBeInstanceOf(ApiError);
    expect(store.getState().thread.map((i) => i.kind)).toEqual(["user", "error"]);
    expect(store.getState().limit).toBeNull();
  });

  it("a network failure and an empty stream each add an error item in plain words", async () => {
    const store = createTurnStore();
    const down = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(store.getState().sendTurn(SID, req(0), { deps: { fetch: down as unknown as typeof fetch } })).rejects.toBeInstanceOf(NetworkError);
    const empty = vi.fn(async () => new Response("", { status: 200, headers: { "content-type": "text/event-stream" } }));
    await expect(store.getState().sendTurn(SID, req(1), { deps: { fetch: empty as unknown as typeof fetch } })).rejects.toBeInstanceOf(NetworkError);
    const errors = store.getState().thread.filter((i) => i.kind === "error");
    expect(errors.map((e) => (e.kind === "error" ? [e.message, e.retryable] : null))).toEqual([
      ["The connection was lost before this reply finished.", true],
      ["The connection dropped before this reply finished.", true],
    ]);
  });

  it("retry removes the error and its user item, then sends the same text once", async () => {
    const store = createTurnStore();
    const down = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    await store.getState().sendTurn(SID, req(0), { deps: { fetch: down as unknown as typeof fetch } }).catch(() => undefined);
    const err = store.getState().thread.find((i) => i.kind === "error");
    const fetchMock = fixtureFetch();
    await store.getState().retry(err?.id as string, SID, { deps: { fetch: fetchMock as unknown as typeof fetch } });
    const thread = store.getState().thread;
    expect(thread.map((i) => i.kind)).toEqual(["user", "turn"]);
    expect(thread[0]).toMatchObject({ kind: "user", text: "message 0" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse((fetchMock.mock.calls[0]?.[1] as RequestInit).body as string).text).toBe("message 0");
  });
});
