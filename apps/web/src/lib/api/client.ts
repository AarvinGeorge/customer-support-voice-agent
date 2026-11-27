/**
 * What: typed fetch wrapper for the agent public API (`/api`, same origin, doc 07 s2.2).
 * Use when: the web app calls any JSON `/api/*` route (not the SSE turn stream, which
 *   stream.ts owns, though it reuses parseErrorResponse, NetworkError and redirectOn401).
 * Inputs: a path, a method and JSON body, and optional injected fetch and redirect.
 * Output: the parsed 2xx body, or a thrown ApiError, ContractViolationError or NetworkError.
 * Side effects: network request; on 401 navigates to the access gate unless the caller opts out.
 * Role: the single place the browser turns doc 07 s1.3 error envelopes into typed errors.
 */
import { ERROR_CLASSES, type ErrorBody, type ErrorEnvelope } from "@contracts/contracts";

/**
 * What: compile-time proof that the runtime class list and the ErrorBody union agree.
 * Use when: never called; a class added to or removed from the contract fails `pnpm typecheck`.
 * Inputs: none.
 * Output: none.
 * Side effects: none.
 * Role: drift guard between ERROR_CLASSES and ErrorBody["class"].
 */
type ClassListMatchesUnion = [ErrorBody["class"]] extends [(typeof ERROR_CLASSES)[number]]
  ? [(typeof ERROR_CLASSES)[number]] extends [ErrorBody["class"]]
    ? true
    : never
  : never;
export const CLASS_LIST_MATCHES_UNION: ClassListMatchesUnion = true;

/**
 * What: where a 401 sends the visitor (the access gate is the home page, CS-E-04.S1).
 * Use when: redirecting on 401, or checking whether the visitor is already on the gate.
 * Inputs: none.
 * Output: the path "/".
 * Side effects: none.
 * Role: constant.
 */
export const ACCESS_GATE_PATH = "/";

/**
 * What: a non-2xx response whose body is a valid doc 07 ErrorEnvelope.
 * Use when: catching API failures to show the message, a countdown or a retry.
 * Inputs: HTTP status, the ErrorBody, retry delay in seconds, trace id.
 * Output: an Error subclass.
 * Side effects: none.
 * Role: typed error surface for UI code (429 countdown, 503 banner, 409 states).
 */
export class ApiError extends Error {
  readonly status: number;
  readonly body: ErrorBody;
  /** Seconds to wait before retrying: body retry_after_s, else the Retry-After header, else null. */
  readonly retryAfterS: number | null;
  readonly traceId: string | null;

  /**
   * What: builds an ApiError.
   * Use when: parseErrorResponse found a valid envelope.
   * Inputs: status, body, retryAfterS, traceId.
   * Output: the instance.
   * Side effects: none.
   * Role: constructor.
   */
  constructor(status: number, body: ErrorBody, retryAfterS: number | null, traceId: string | null) {
    super(`${status} ${body.code}: ${body.message}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.retryAfterS = retryAfterS;
    this.traceId = traceId;
  }
}

/**
 * What: a response whose body breaks the doc 07 contract shape (a non-2xx body that is not an
 *   ErrorEnvelope, or a 2xx body that is not JSON).
 * Use when: never caught to hide; it means a proxy page, a framework default error or contract drift.
 * Inputs: HTTP status, a reason, the first part of the raw body.
 * Output: an Error subclass.
 * Side effects: none.
 * Role: fail loudly on an unknown error shape instead of guessing.
 */
export class ContractViolationError extends Error {
  readonly status: number;
  readonly raw: string;

  /**
   * What: builds a ContractViolationError.
   * Use when: a body breaks the contract shape.
   * Inputs: status, reason, raw body text.
   * Output: the instance.
   * Side effects: none.
   * Role: constructor.
   */
  constructor(status: number, reason: string, raw: string) {
    super(`HTTP ${status}: body breaks the doc 07 contract (${reason}): ${raw.slice(0, 200)}`);
    this.name = "ContractViolationError";
    this.status = status;
    this.raw = raw;
  }
}

/**
 * What: why a request produced no contract-shaped answer at the transport level.
 * Use when: switching on NetworkError.reason (UI copy, the drop path of CS-E-04.S4.T3).
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: "network": fetch or a body read failed (offline, DNS, reset); "aborted": the caller
 *   aborted; "no_bytes_timeout": no byte for 20 s (doc 07 s8.1 browser chat no bytes);
 *   "turn_timeout": the whole turn passed 50 s; "dropped": the stream closed before turn.completed.
 */
export type NetworkErrorReason = "network" | "aborted" | "no_bytes_timeout" | "turn_timeout" | "dropped";

/**
 * What: a transport failure: no response, an aborted request, a stalled or dropped stream.
 * Use when: catching API or stream failures that carry no ErrorEnvelope.
 * Inputs: reason, the turn id if the stream had started (turn.started seen), the cause.
 * Output: an Error subclass.
 * Side effects: none.
 * Role: typed network error (scaffold review M2); a stream error keeps turnId so the UI can
 *   look the turn up instead of guessing, because a turn is not idempotent (doc 07 s9).
 */
export class NetworkError extends Error {
  readonly reason: NetworkErrorReason;
  readonly turnId: string | null;

  /**
   * What: builds a NetworkError.
   * Use when: fetch rejects, a body read fails, a watchdog fires or a stream drops.
   * Inputs: reason, turnId (null outside a started turn), cause (the original error, if any).
   * Output: the instance.
   * Side effects: none.
   * Role: constructor.
   */
  constructor(reason: NetworkErrorReason, turnId: string | null = null, cause?: unknown) {
    super(`network: ${reason}${turnId ? ` (turn ${turnId})` : ""}`, cause === undefined ? undefined : { cause });
    this.name = "NetworkError";
    this.reason = reason;
    this.turnId = turnId;
  }
}

/**
 * What: maps a thrown fetch or body-read error to a NetworkError.
 * Use when: wrapping a fetch call or a body read.
 * Inputs: the caught value, the request's abort signal.
 * Output: NetworkError with reason "aborted" if the signal fired, else "network".
 * Side effects: none.
 * Role: one place that decides abort versus network failure.
 */
export function toNetworkError(err: unknown, signal?: AbortSignal | null): NetworkError {
  if (err instanceof NetworkError) return err;
  return new NetworkError(signal?.aborted ? "aborted" : "network", null, err);
}

/**
 * What: smallest retry delay in seconds (doc 07 s9 minimum).
 * Use when: clamping retry delays.
 * Inputs: none.
 * Output: 1.
 * Side effects: none.
 * Role: constant.
 */
const RETRY_AFTER_MIN_S = 1;
/**
 * What: largest retry delay in seconds, a sanity cap.
 * Use when: clamping retry delays.
 * Inputs: none.
 * Output: 3600.
 * Side effects: none.
 * Role: constant.
 */
const RETRY_AFTER_MAX_S = 3600;

/**
 * What: per-call request options for apiFetch.
 * Use when: describing one call (method, JSON body, extra headers, abort signal).
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: request description.
 */
export interface ApiRequest {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  /** JSON-serialisable body; sent with Content-Type application/json. */
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

/**
 * What: injectable dependencies and the 401 policy for apiFetch.
 * Use when: tests inject fetch, redirect, pathname and clock; the access form sets on401 "throw".
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: test seams and the access-form opt out.
 */
export interface ApiDeps {
  fetch?: typeof fetch;
  redirect?: (path: string) => void;
  /** "redirect" (default) sends the visitor to the access gate; "throw" leaves 401 to the caller. */
  on401?: "redirect" | "throw";
  now?: () => number;
  /** Current route path; defaults to window.location.pathname (null on the server). */
  pathname?: () => string | null;
}

/**
 * What: clamps a retry delay to RETRY_AFTER_MIN_S..RETRY_AFTER_MAX_S whole seconds.
 * Use when: any retry delay is about to reach the UI countdown.
 * Inputs: seconds (any finite number).
 * Output: an integer in 1..3600.
 * Side effects: none.
 * Role: helper for 429 and 503 handling.
 */
function clampRetry(seconds: number): number {
  return Math.min(RETRY_AFTER_MAX_S, Math.max(RETRY_AFTER_MIN_S, Math.ceil(seconds)));
}

/**
 * What: parses a Retry-After header value (non-negative integer seconds or HTTP date).
 * Use when: the error body has no retry_after_s.
 * Inputs: the header value, the current time in ms.
 * Output: seconds to wait clamped to 1..3600, or null if absent or unparseable
 *   (decimals and negatives are rejected; a date must contain letters).
 * Side effects: none.
 * Role: helper for 429 and 503 handling.
 */
export function parseRetryAfter(value: string | null, nowMs: number): number | null {
  if (value === null) return null;
  const v = value.trim();
  if (/^\d+$/.test(v)) return clampRetry(Number(v));
  if (!/[a-z]/i.test(v)) return null;
  const at = Date.parse(v);
  if (Number.isNaN(at)) return null;
  return clampRetry((at - nowMs) / 1000);
}

/**
 * What: checks that an unknown JSON value is a doc 07 ErrorEnvelope.
 * Use when: parsing any non-2xx body.
 * Inputs: the decoded JSON value.
 * Output: null if valid, else a short reason string.
 * Side effects: none.
 * Role: runtime guard; the TS types alone cannot reject a bad server body.
 */
function envelopeProblem(value: unknown): string | null {
  if (typeof value !== "object" || value === null) return "not an object";
  const err = (value as { error?: unknown }).error;
  if (typeof err !== "object" || err === null) return "missing error object";
  const e = err as Record<string, unknown>;
  if (!(ERROR_CLASSES as readonly unknown[]).includes(e.class)) return `unknown class ${JSON.stringify(e.class)}`;
  if (typeof e.code !== "string" || typeof e.message !== "string") return "code or message not a string";
  if (typeof e.retryable !== "boolean" || typeof e.fault_injected !== "boolean") return "retryable or fault_injected not a boolean";
  if (!(e.changed === null || typeof e.changed === "boolean")) return "changed not boolean or null";
  if (!(e.retry_after_s == null || typeof e.retry_after_s === "number")) return "retry_after_s not a number";
  if (!(e.trace_id == null || typeof e.trace_id === "string")) return "trace_id not a string";
  if (typeof e.details !== "object" || e.details === null || Array.isArray(e.details)) return "details not an object";
  return null;
}

/**
 * What: turns a non-2xx Response into ApiError or ContractViolationError.
 * Use when: any fetch to `/api/*` (JSON or the SSE turn route before the stream opens) is not ok.
 * Inputs: the Response, the current time in ms.
 * Output: the error to throw (it does not throw itself); NetworkError if the body read fails.
 * Side effects: consumes the response body.
 * Role: shared error parser for apiFetch and streamTurn.
 */
export async function parseErrorResponse(res: Response, nowMs: number): Promise<ApiError | ContractViolationError | NetworkError> {
  let raw: string;
  try {
    raw = await res.text();
  } catch (err) {
    return toNetworkError(err);
  }
  let json: unknown;
  try {
    json = JSON.parse(raw);
  } catch {
    return new ContractViolationError(res.status, "not JSON", raw);
  }
  const problem = envelopeProblem(json);
  if (problem !== null) return new ContractViolationError(res.status, problem, raw);
  const body = (json as ErrorEnvelope).error;
  const retryAfterS =
    body.retry_after_s != null ? clampRetry(body.retry_after_s) : parseRetryAfter(res.headers.get("retry-after"), nowMs);
  const traceId = body.trace_id ?? res.headers.get("x-trace-id");
  return new ApiError(res.status, body, retryAfterS, traceId);
}

/**
 * What: default redirect, a full navigation so server components re-check the cookie.
 * Use when: a 401 reaches apiFetch in the browser.
 * Inputs: the target path.
 * Output: none.
 * Side effects: navigates the window; no-op on the server.
 * Role: default ApiDeps.redirect.
 */
function browserRedirect(path: string): void {
  if (typeof window !== "undefined") window.location.assign(path);
}

/**
 * What: default pathname reader.
 * Use when: apiFetch decides whether a 401 redirect would reload the gate itself.
 * Inputs: none.
 * Output: window.location.pathname, or null on the server.
 * Side effects: none.
 * Role: default ApiDeps.pathname.
 */
function browserPathname(): string | null {
  return typeof window !== "undefined" ? window.location.pathname : null;
}

/**
 * What: applies the 401 policy to a parsed error.
 * Use when: apiFetch or streamTurn got a non-2xx response.
 * Inputs: the parsed error, ApiDeps (on401, redirect, pathname).
 * Output: none.
 * Side effects: on a contract-shaped 401, navigates to ACCESS_GATE_PATH unless on401 is "throw"
 *   or the visitor is already on the gate (no reload loop).
 * Role: shared 401 handling for JSON calls and the turn stream.
 */
export function redirectOn401(err: Error, deps: ApiDeps): void {
  const onGate = (deps.pathname ?? browserPathname)() === ACCESS_GATE_PATH;
  if (err instanceof ApiError && err.status === 401 && (deps.on401 ?? "redirect") === "redirect" && !onGate) {
    (deps.redirect ?? browserRedirect)(ACCESS_GATE_PATH);
  }
}

/**
 * What: a request id for X-Request-Id (doc 07 s1.2, optional header).
 * Use when: the caller did not supply one.
 * Inputs: none.
 * Output: a uuid4 string, or null where crypto.randomUUID is missing (non-secure http context).
 * Side effects: none.
 * Role: helper; the agent mints an id when the header is absent.
 */
export function newRequestId(): string | null {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function" ? crypto.randomUUID() : null;
}

/**
 * What: calls a JSON `/api/*` route and returns its typed body.
 * Use when: every non-streaming call from the web app to the agent.
 * Inputs: path, ApiRequest, ApiDeps.
 * Output: the parsed 2xx JSON as T (undefined for 204 or an empty body).
 * Side effects: network request with a fresh X-Request-Id; on a contract-shaped 401
 *   redirects to ACCESS_GATE_PATH unless on401 is "throw" or the visitor is already on the
 *   gate (no reload loop). A non-JSON 2xx body throws ContractViolationError; a rejected fetch or
 *   body read throws NetworkError ("aborted" if req.signal fired). It never retries by itself:
 *   ApiError.retryAfterS tells the UI how long to count down (doc 07 s9; turns are not idempotent).
 * Role: the API client entry point.
 */
export async function apiFetch<T = unknown>(path: string, req: ApiRequest = {}, deps: ApiDeps = {}): Promise<T> {
  const doFetch = deps.fetch ?? fetch;
  const headers = new Headers(req.headers);
  const requestId = headers.get("x-request-id") ?? newRequestId();
  if (requestId !== null) headers.set("x-request-id", requestId);
  headers.set("accept", "application/json");
  let body: string | undefined;
  if (req.body !== undefined) {
    headers.set("content-type", "application/json");
    body = JSON.stringify(req.body);
  }
  let res: Response;
  try {
    res = await doFetch(path, {
      method: req.method ?? "GET",
      headers,
      body,
      credentials: "same-origin",
      signal: req.signal,
    });
  } catch (err) {
    throw toNetworkError(err, req.signal);
  }
  if (res.ok) {
    let text: string;
    try {
      text = await res.text();
    } catch (err) {
      throw toNetworkError(err, req.signal);
    }
    if (text === "") return undefined as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new ContractViolationError(res.status, "2xx body is not JSON", text);
    }
  }
  const err = await parseErrorResponse(res, (deps.now ?? Date.now)());
  redirectOn401(err, deps);
  throw err;
}
