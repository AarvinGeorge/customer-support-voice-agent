/**
 * What: the SSE turn transport: POST a turn, parse the doc 07 s6.1 stream, hand each event to a callback.
 * Use when: the console sends a chat turn (streamTurn) or, later, the voice relay posts voice/ask (streamSse).
 * Inputs: session id and TurnRequest (or a path and body), StreamOptions.
 * Output: a promise of the turn.completed event and turn id, or a typed rejection
 *   (ApiError, ContractViolationError, NetworkError).
 * Side effects: one network request; on 401 navigates to the access gate (same rule as apiFetch).
 * Role: the only place the browser reads the event stream; it never retries a turn (doc 07 s9).
 */
import { createParser, type EventSourceMessage } from "eventsource-parser";
import { EVENT_TYPES, LIMITS, type SseEvent, type TurnCompletedEvent, type TurnRequest } from "@contracts/contracts";
import {
  ContractViolationError,
  NetworkError,
  newRequestId,
  parseErrorResponse,
  redirectOn401,
  toNetworkError,
  type ApiDeps,
  type NetworkErrorReason,
} from "./client";

/**
 * What: the two demo faults of doc 07 s10.
 * Use when: arming the X-Demo-Fault header for one request.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: header value type.
 */
export type DemoFault = "tool_error" | "llm_429";

/**
 * What: options for one streamed turn.
 * Use when: calling streamTurn or streamSse.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: callback, abort, fault header, watchdog windows and test seams.
 */
export interface StreamOptions {
  /** Called once per parsed event, in wire order, up to and including turn.completed. */
  onEvent: (event: SseEvent) => void;
  signal?: AbortSignal;
  /** Sent as X-Demo-Fault on this request only; the store clears it after taking it. */
  fault?: DemoFault | null;
  /** No-bytes window in ms; default doc 07 s8.1 browser chat no bytes (20 s). */
  noBytesMs?: number;
  /** Whole-turn window in ms; default doc 07 s8.1 browser chat whole turn (50 s). */
  turnMs?: number;
  deps?: ApiDeps;
}

/**
 * What: the outcome of a stream that reached turn.completed.
 * Use when: the caller needs the final answer (voice returns final.say to Realtime).
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: resolved value of streamSse.
 */
export interface StreamResult {
  turnId: string;
  completed: TurnCompletedEvent;
}

/**
 * What: the event type names of doc 07 s6.2 as a set.
 * Use when: rejecting an unknown event type.
 * Inputs: none.
 * Output: a ReadonlySet of strings.
 * Side effects: none.
 * Role: constant.
 */
const KNOWN_TYPES: ReadonlySet<string> = new Set(EVENT_TYPES);

/**
 * What: checks one SSE message against the doc 07 s6.1 envelope and turns it into an SseEvent.
 * Use when: every message the parser emits.
 * Inputs: the parsed SSE message (event name, id, data).
 * Output: the SseEvent.
 * Side effects: none.
 * Role: runtime guard; throws ContractViolationError on bad JSON, v other than 1, an unknown type,
 *   a non-integer seq, an `event:` name that differs from data.type or an `id:` that differs from seq.
 *   Fields inside each type are trusted to the generated types (both sides import one contract).
 */
export function parseSseEvent(msg: EventSourceMessage): SseEvent {
  const fail = (reason: string) => new ContractViolationError(200, `SSE ${reason}`, msg.data);
  let data: unknown;
  try {
    data = JSON.parse(msg.data);
  } catch {
    throw fail("data is not JSON");
  }
  if (typeof data !== "object" || data === null) throw fail("data is not an object");
  const ev = data as Record<string, unknown>;
  if (ev.v !== 1) throw fail(`v is ${JSON.stringify(ev.v)}, expected 1`);
  if (typeof ev.type !== "string" || !KNOWN_TYPES.has(ev.type)) throw fail(`unknown type ${JSON.stringify(ev.type)}`);
  if (typeof ev.seq !== "number" || !Number.isInteger(ev.seq) || ev.seq < 1) throw fail("seq is not a positive integer");
  if (typeof ev.turn_id !== "string" || typeof ev.session_id !== "string") throw fail("turn_id or session_id missing");
  if (msg.event !== undefined && msg.event !== ev.type) throw fail(`event name ${msg.event} differs from type ${ev.type}`);
  if (msg.id !== undefined && msg.id !== String(ev.seq)) throw fail(`id ${msg.id} differs from seq ${ev.seq}`);
  return data as SseEvent;
}

/**
 * What: POSTs a JSON body to an SSE route and streams the turn's events.
 * Use when: any `/api` route that answers 200 `text/event-stream` (turns, voice/ask).
 * Inputs: path, JSON body, StreamOptions.
 * Output: StreamResult once turn.completed arrives.
 * Side effects: network request with X-Request-Id and, if set, X-Demo-Fault. Watchdogs: no bytes
 *   (keepalive comments count as bytes) and whole turn, both from the moment the request is sent.
 *   A non-2xx rejects with the parsed envelope error (ApiError or ContractViolationError) and
 *   applies the 401 redirect; a 2xx that is not an event stream rejects with ContractViolationError;
 *   a stall, timeout, drop, abort or fetch failure rejects with NetworkError (turnId set once
 *   turn.started was seen). After turn.completed the body is cancelled and later bytes are ignored.
 * Role: transport core; the reducer, not this function, decides what events mean.
 */
export async function streamSse(path: string, body: unknown, opts: StreamOptions): Promise<StreamResult> {
  const deps = opts.deps ?? {};
  const doFetch = deps.fetch ?? fetch;
  const noBytesMs = opts.noBytesMs ?? LIMITS.BROWSER_CHAT_NO_BYTES_S * 1000;
  const turnMs = opts.turnMs ?? LIMITS.BROWSER_CHAT_TURN_S * 1000;

  const ctrl = new AbortController();
  let stopReason: NetworkErrorReason | null = null;
  let turnId: string | null = null;
  const stop = (reason: NetworkErrorReason) => {
    if (stopReason === null) stopReason = reason;
    ctrl.abort();
  };
  const onCallerAbort = () => stop("aborted");
  if (opts.signal?.aborted) stop("aborted");
  opts.signal?.addEventListener("abort", onCallerAbort);
  const stopped = new Promise<never>((_, reject) => {
    ctrl.signal.addEventListener("abort", () => reject(new NetworkError(stopReason ?? "aborted", turnId)));
  });
  stopped.catch(() => {});

  let noBytesTimer = setTimeout(() => stop("no_bytes_timeout"), noBytesMs);
  const turnTimer = setTimeout(() => stop("turn_timeout"), turnMs);
  const touch = () => {
    clearTimeout(noBytesTimer);
    noBytesTimer = setTimeout(() => stop("no_bytes_timeout"), noBytesMs);
  };
  const cleanup = () => {
    clearTimeout(noBytesTimer);
    clearTimeout(turnTimer);
    opts.signal?.removeEventListener("abort", onCallerAbort);
  };

  const headers = new Headers({ accept: "text/event-stream", "content-type": "application/json" });
  const requestId = newRequestId();
  if (requestId !== null) headers.set("x-request-id", requestId);
  if (opts.fault) headers.set("x-demo-fault", opts.fault);

  let reader: ReadableStreamDefaultReader<string> | null = null;
  try {
    let res: Response;
    try {
      res = await Promise.race([
        doFetch(path, { method: "POST", headers, body: JSON.stringify(body), credentials: "same-origin", signal: ctrl.signal }),
        stopped,
      ]);
    } catch (err) {
      throw stopReason !== null ? new NetworkError(stopReason, null, err) : toNetworkError(err);
    }
    if (!res.ok) {
      const err = await Promise.race([parseErrorResponse(res, (deps.now ?? Date.now)()), stopped]);
      redirectOn401(err, deps);
      throw err;
    }
    const contentType = res.headers.get("content-type") ?? "";
    if (!contentType.startsWith("text/event-stream") || res.body === null) {
      const raw = await res.text().catch(() => "");
      throw new ContractViolationError(res.status, `2xx turn response is ${contentType || "empty"}, not text/event-stream`, raw);
    }
    touch();

    let completed: TurnCompletedEvent | null = null;
    let violation: Error | null = null;
    const parser = createParser({
      onEvent(msg) {
        if (completed !== null || violation !== null) return;
        let ev: SseEvent;
        try {
          ev = parseSseEvent(msg);
        } catch (err) {
          violation = err as Error;
          return;
        }
        turnId ??= ev.turn_id;
        opts.onEvent(ev);
        if (ev.type === "turn.completed") completed = ev;
      },
    });

    reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    for (;;) {
      let chunk: ReadableStreamReadResult<string>;
      try {
        chunk = await Promise.race([reader.read(), stopped]);
      } catch (err) {
        throw err instanceof NetworkError ? err : new NetworkError(stopReason ?? "network", turnId, err);
      }
      if (chunk.done) break;
      touch();
      parser.feed(chunk.value);
      if (violation !== null) throw violation;
      if (completed !== null) return { turnId: (completed as TurnCompletedEvent).turn_id, completed };
    }
    throw new NetworkError("dropped", turnId);
  } finally {
    cleanup();
    if (reader !== null) reader.cancel().catch(() => {});
    if (!ctrl.signal.aborted) ctrl.abort();
  }
}

/**
 * What: sends one chat turn to `POST /api/sessions/{sid}/turns` and streams its events.
 * Use when: the console submits text, a confirm, a verification card or the ticket offer.
 * Inputs: session id, TurnRequest (exactly one input; the server answers 422 otherwise), StreamOptions.
 * Output: StreamResult, or the typed rejections of streamSse.
 * Side effects: those of streamSse.
 * Role: chat entry point of the transport.
 */
export function streamTurn(sessionId: string, request: TurnRequest, opts: StreamOptions): Promise<StreamResult> {
  return streamSse(`/api/sessions/${encodeURIComponent(sessionId)}/turns`, request, opts);
}
