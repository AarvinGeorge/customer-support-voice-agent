/**
 * What: tests for streamTurn (doc 07 s2.2 turns route, s6.1 wire format, s8.1 browser limits, s10 fault header).
 * Use when: `pnpm test`.
 * Inputs: a fake fetch that returns SSE bodies built from the shared fixtures.
 * Output: pass or fail per rule: parsing across chunk splits, keepalive, watchdogs, pre-stream
 *   errors, drop, abort, contract violations, the X-Demo-Fault header.
 * Side effects: none (no network; short real timers).
 * Role: proves the transport half of CS-E-04.S4.T2.
 */
import { describe, expect, it, vi } from "vitest";
import type { SseEvent, TurnRequest } from "@contracts/contracts";
import { loadFixture, toWire } from "@/test-support/fixtures";
import { ApiError, ContractViolationError, NetworkError } from "./client";
import { streamTurn, type StreamOptions } from "./stream";

const s1 = loadFixture("s1_chat.jsonl");
const c4id = s1.find((e) => e.type === "turn.started" && e.turn_id.endsWith("c4"))?.turn_id as string;
const C4 = s1.filter((e) => e.turn_id === c4id);
const SID = C4[0]?.session_id as string;
const REQ: TurnRequest = { client_turn_id: "6f1d2c1e-0000-4000-8000-000000000001", text: "When will my refund arrive?" };

/**
 * What: one piece of a fake body: text to send, or a pause in ms.
 * Use when: describing a scripted SSE body.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: test type.
 */
type Piece = string | { wait: number };

/**
 * What: a fake SSE response that sends pieces with pauses.
 * Use when: a test needs a scripted stream.
 * Inputs: pieces; options hang (never close), status, contentType.
 * Output: the Response and a wasCancelled probe.
 * Side effects: runs timers while streaming.
 * Role: test helper.
 */
function sseResponse(pieces: Piece[], opts: { hang?: boolean; status?: number; contentType?: string } = {}) {
  const enc = new TextEncoder();
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    async start(ctrl) {
      for (const p of pieces) {
        if (cancelled) return;
        if (typeof p === "string") ctrl.enqueue(enc.encode(p));
        else await new Promise((r) => setTimeout(r, p.wait));
      }
      if (!opts.hang && !cancelled) ctrl.close();
    },
    cancel() {
      cancelled = true;
    },
  });
  const res = new Response(body, {
    status: opts.status ?? 200,
    headers: { "content-type": opts.contentType ?? "text/event-stream; charset=utf-8" },
  });
  return { res, wasCancelled: () => cancelled };
}

/**
 * What: splits text into chunks of n characters.
 * Use when: making events straddle chunk boundaries.
 * Inputs: the text, n.
 * Output: string chunks.
 * Side effects: none.
 * Role: test helper.
 */
function chunked(text: string, n: number): string[] {
  const out: string[] = [];
  for (let i = 0; i < text.length; i += n) out.push(text.slice(i, i + n));
  return out;
}

/**
 * What: runs streamTurn against one canned response and collects events.
 * Use when: most stream tests.
 * Inputs: the Response, extra StreamOptions.
 * Output: the result promise, the event list, the fetch mock.
 * Side effects: none.
 * Role: test helper.
 */
async function run(res: Response, extra: Partial<StreamOptions> = {}) {
  const events: SseEvent[] = [];
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => res);
  const result = streamTurn(SID, REQ, {
    onEvent: (e) => events.push(e),
    deps: { fetch: fetchMock as unknown as typeof fetch, redirect: vi.fn(), pathname: () => "/console" },
    ...extra,
  });
  return { result, events, fetchMock };
}

/**
 * What: settles a promise into its value or its rejection.
 * Use when: a test inspects a rejection.
 * Inputs: the promise.
 * Output: {ok, value} or {ok, error}.
 * Side effects: none.
 * Role: test helper.
 */
async function settle<T>(p: Promise<T>): Promise<{ ok: true; value: T } | { ok: false; error: unknown }> {
  try {
    return { ok: true, value: await p };
  } catch (error) {
    return { ok: false, error };
  }
}

describe("streamTurn request", () => {
  it("POSTs the TurnRequest to the session turns route as an SSE request", async () => {
    const { res } = sseResponse([toWire(C4)]);
    const { result, fetchMock } = await run(res);
    await result;
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/sessions/${SID}/turns`);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(JSON.parse(init.body as string)).toEqual(REQ);
    const h = new Headers(init.headers);
    expect(h.get("accept")).toBe("text/event-stream");
    expect(h.get("content-type")).toBe("application/json");
    expect(h.get("x-request-id")).toMatch(/^[0-9a-f-]{36}$/);
    expect(h.has("x-demo-fault")).toBe(false);
  });

  it("sends X-Demo-Fault only when a fault is given", async () => {
    const { res } = sseResponse([toWire(C4)]);
    const { result, fetchMock } = await run(res, { fault: "llm_429" });
    await result;
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).get("x-demo-fault")).toBe("llm_429");
  });
});

describe("streamTurn parsing", () => {
  it("delivers every event in order across awkward chunk splits and keepalives, then resolves", async () => {
    const { res } = sseResponse(chunked(toWire(C4, true), 7));
    const { result, events } = await run(res);
    const out = await result;
    expect(events.map((e) => e.seq)).toEqual(C4.map((e) => e.seq));
    expect(events).toEqual(C4);
    expect(out.turnId).toBe(c4id);
    expect(out.completed.type).toBe("turn.completed");
  });

  it("stops at turn.completed: later bytes are not delivered and the body is cancelled", async () => {
    const extra = { ...C4[1], seq: 99 } as SseEvent;
    const { res, wasCancelled } = sseResponse([toWire(C4), { wait: 5 }, toWire([extra])], { hang: true });
    const { result, events } = await run(res);
    await result;
    await new Promise((r) => setTimeout(r, 20));
    expect(events.at(-1)?.type).toBe("turn.completed");
    expect(events).toHaveLength(C4.length);
    expect(wasCancelled()).toBe(true);
  });

  it("fails loudly on an event whose data is not JSON", async () => {
    const { res } = sseResponse([toWire(C4.slice(0, 1)), "event: token\nid: 2\ndata: {not json\n\n"]);
    const { result } = await run(res);
    const r = await settle(result);
    expect(r.ok).toBe(false);
    expect((r as { error: unknown }).error).toBeInstanceOf(ContractViolationError);
  });

  it("fails loudly when the SSE event name or id disagrees with the data", async () => {
    const ev = C4[1] as SseEvent;
    const { res } = sseResponse([`event: token\nid: ${ev.seq}\ndata: ${JSON.stringify(ev)}\n\n`]);
    const r = await settle((await run(res)).result);
    expect((r as { error: unknown }).error).toBeInstanceOf(ContractViolationError);
    const { res: res2 } = sseResponse([`event: ${ev.type}\nid: 77\ndata: ${JSON.stringify(ev)}\n\n`]);
    const r2 = await settle((await run(res2)).result);
    expect((r2 as { error: unknown }).error).toBeInstanceOf(ContractViolationError);
  });

  it("fails loudly on an unknown event type or a wrong envelope version", async () => {
    const ev = { ...(C4[1] as SseEvent), type: "message.reset" };
    const { res } = sseResponse([`event: message.reset\nid: ${ev.seq}\ndata: ${JSON.stringify(ev)}\n\n`]);
    expect((await settle((await run(res)).result) as { error: unknown }).error).toBeInstanceOf(ContractViolationError);
    const v2 = { ...(C4[1] as SseEvent), v: 2 };
    const { res: res2 } = sseResponse([`event: ${v2.type}\nid: ${v2.seq}\ndata: ${JSON.stringify(v2)}\n\n`]);
    expect((await settle((await run(res2)).result) as { error: unknown }).error).toBeInstanceOf(ContractViolationError);
  });
});

describe("streamTurn pre-stream errors (envelope before the stream opens)", () => {
  /**
   * What: a JSON error response.
   * Use when: pre-stream error tests.
   * Inputs: status, body (object or raw string), headers.
   * Output: a Response.
   * Side effects: none.
   * Role: test helper.
   */
  function errorResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
    return new Response(typeof body === "string" ? body : JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json", ...headers },
    });
  }
  /**
   * What: a valid ErrorEnvelope body.
   * Use when: pre-stream error tests.
   * Inputs: class, code, extra ErrorBody fields.
   * Output: {error: ErrorBody}.
   * Side effects: none.
   * Role: test helper.
   */
  const envelope = (cls: string, code: string, extra: Record<string, unknown> = {}) => ({
    error: { class: cls, code, message: "m", retryable: false, changed: false, fault_injected: false, details: {}, ...extra },
  });

  it("409 TURN_IN_PROGRESS becomes an ApiError", async () => {
    const r = await settle((await run(errorResponse(409, envelope("core_conflict", "TURN_IN_PROGRESS")))).result);
    const err = (r as { error: unknown }).error as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.body.code]).toEqual([409, "TURN_IN_PROGRESS"]);
  });

  it("429 SESSION_TURNS carries the retry delay for the countdown", async () => {
    const res = errorResponse(429, envelope("rate_limited", "SESSION_TURNS", { retryable: true, retry_after_s: 7 }), { "retry-after": "7" });
    const err = (await settle((await run(res)).result) as { error: unknown }).error as ApiError;
    expect([err.status, err.body.code, err.retryAfterS]).toEqual([429, "SESSION_TURNS", 7]);
  });

  it("503 BUSY falls back to the Retry-After header", async () => {
    const res = errorResponse(503, envelope("admission_rejected", "BUSY", { retryable: true }), { "retry-after": "3" });
    const err = (await settle((await run(res)).result) as { error: unknown }).error as ApiError;
    expect(err.retryAfterS).toBe(3);
  });

  it("401 redirects to the access gate and rejects", async () => {
    const redirect = vi.fn();
    const res = errorResponse(401, envelope("access_denied", "ACCESS_DENIED"));
    const fetchMock = vi.fn(async () => res);
    const r = await settle(
      streamTurn(SID, REQ, { onEvent: () => {}, deps: { fetch: fetchMock as unknown as typeof fetch, redirect, pathname: () => "/console" } }),
    );
    expect((r as { error: unknown }).error).toBeInstanceOf(ApiError);
    expect(redirect).toHaveBeenCalledWith("/");
  });

  it("a non-envelope error page becomes a ContractViolationError", async () => {
    const res = new Response("<html>Bad gateway</html>", { status: 502, headers: { "content-type": "text/html" } });
    expect((await settle((await run(res)).result) as { error: unknown }).error).toBeInstanceOf(ContractViolationError);
  });

  it("a 2xx that is not text/event-stream is a ContractViolationError", async () => {
    const res = new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } });
    expect((await settle((await run(res)).result) as { error: unknown }).error).toBeInstanceOf(ContractViolationError);
  });
});

describe("streamTurn watchdogs, drops and aborts (typed NetworkError)", () => {
  it("keepalive comments keep the no-bytes watchdog from firing", async () => {
    const keepalives: Piece[] = Array.from({ length: 6 }, () => [": keepalive\n\n", { wait: 15 }] as Piece[]).flat();
    const { res } = sseResponse([toWire(C4.slice(0, 1)), ...keepalives, toWire(C4.slice(1))]);
    const { result, events } = await run(res, { noBytesMs: 40, turnMs: 5000 });
    await result;
    expect(events).toHaveLength(C4.length);
  });

  it("no bytes for the no-bytes window gives reason no_bytes_timeout with the turn id", async () => {
    const { res, wasCancelled } = sseResponse([toWire(C4.slice(0, 3))], { hang: true });
    const { result, events } = await run(res, { noBytesMs: 30, turnMs: 5000 });
    const err = (await settle(result) as { error: unknown }).error as NetworkError;
    expect(err).toBeInstanceOf(NetworkError);
    expect([err.reason, err.turnId]).toEqual(["no_bytes_timeout", c4id]);
    expect(events).toHaveLength(3);
    expect(wasCancelled()).toBe(true);
  });

  it("a turn longer than the whole-turn window gives reason turn_timeout even with keepalives", async () => {
    const keepalives: Piece[] = Array.from({ length: 40 }, () => [": keepalive\n\n", { wait: 10 }] as Piece[]).flat();
    const { res } = sseResponse([toWire(C4.slice(0, 1)), ...keepalives], { hang: true });
    const err = (await settle((await run(res, { noBytesMs: 50, turnMs: 80 })).result) as { error: unknown }).error as NetworkError;
    expect(err.reason).toBe("turn_timeout");
  });

  it("a stream that closes before turn.completed gives reason dropped", async () => {
    const { res } = sseResponse([toWire(C4.slice(0, 5))]);
    const err = (await settle((await run(res)).result) as { error: unknown }).error as NetworkError;
    expect([err.reason, err.turnId]).toEqual(["dropped", c4id]);
  });

  it("the caller's abort gives reason aborted", async () => {
    const ctrl = new AbortController();
    const { res } = sseResponse([toWire(C4.slice(0, 2))], { hang: true });
    const { result, events } = await run(res, { signal: ctrl.signal, noBytesMs: 5000 });
    await vi.waitFor(() => expect(events).toHaveLength(2));
    ctrl.abort();
    const err = (await settle(result) as { error: unknown }).error as NetworkError;
    expect(err.reason).toBe("aborted");
  });

  it("a fetch that rejects (offline, DNS, reset) gives reason network with the cause kept", async () => {
    const cause = new TypeError("fetch failed");
    const fetchMock = vi.fn(async () => {
      throw cause;
    });
    const r = await settle(streamTurn(SID, REQ, { onEvent: () => {}, deps: { fetch: fetchMock as unknown as typeof fetch } }));
    const err = (r as { error: unknown }).error as NetworkError;
    expect([err.reason, err.turnId, err.cause]).toEqual(["network", null, cause]);
  });

  it("no response headers within the no-bytes window also times out", async () => {
    const fetchMock = vi.fn(() => new Promise<Response>(() => {}));
    const r = await settle(
      streamTurn(SID, REQ, { onEvent: () => {}, noBytesMs: 30, deps: { fetch: fetchMock as unknown as typeof fetch } }),
    );
    expect(((r as { error: unknown }).error as NetworkError).reason).toBe("no_bytes_timeout");
  });
});
