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
    expect(store.getState().conversation).toEqual(replayEvents(s1));
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
    expect(conv.turns[turnIds[0] as string]?.phase).toBe("streaming");
    expect((store.getState().connection.error as NetworkError).reason).toBe("dropped");
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
