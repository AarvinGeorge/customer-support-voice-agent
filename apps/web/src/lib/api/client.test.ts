/**
 * What: unit tests for the typed API client (doc 07 s1.3 envelope, s2.2 errors).
 * Use when: `pnpm test`.
 * Inputs: fake fetch responses and a fake redirect.
 * Output: pass or fail per case.
 * Side effects: none (no network).
 * Role: proves the client parses ErrorEnvelope, honours retry_after_s and
 *   Retry-After on 429, redirects to the access gate on 401, and fails loudly
 *   on an error body that is not the contract shape.
 */
import { describe, expect, it, vi } from "vitest";

import { ACCESS_GATE_PATH, ApiError, ContractViolationError, apiFetch, parseRetryAfter } from "./client";

/**
 * What: builds a fake fetch that returns one canned response.
 * Use when: a test needs a deterministic server reply.
 * Inputs: status, body (object is JSON encoded, string is sent raw), headers.
 * Output: a vi.fn fetch and the Response it returns.
 * Side effects: none.
 * Role: test helper.
 */
function fakeFetch(status: number, body: unknown, headers: Record<string, string> = {}) {
  const text = body === undefined ? null : typeof body === "string" ? body : JSON.stringify(body);
  const contentType = typeof body === "string" ? "text/html" : "application/json";
  return vi.fn(async () => new Response(text, { status, headers: { "content-type": contentType, ...headers } }));
}

/**
 * What: a valid ErrorEnvelope body with overrides.
 * Use when: a test needs a contract-shaped error.
 * Inputs: partial ErrorBody fields.
 * Output: { error: ErrorBody }.
 * Side effects: none.
 * Role: test helper.
 */
function envelope(over: Record<string, unknown> = {}) {
  return {
    error: {
      class: "rate_limited",
      code: "RATE_LIMITED",
      message: "Too many requests. Try again shortly.",
      retryable: true,
      changed: false,
      retry_after_s: null,
      fault_injected: false,
      trace_id: "0192f0c4-0000-7000-8000-000000000001",
      details: {},
      ...over,
    },
  };
}

/**
 * What: runs apiFetch and returns the thrown error.
 * Use when: a test expects a rejection and wants to inspect it.
 * Inputs: the promise.
 * Output: the rejection value.
 * Side effects: none.
 * Role: test helper; fails the test if the promise resolves.
 */
async function rejection(p: Promise<unknown>): Promise<unknown> {
  try {
    await p;
  } catch (err) {
    return err;
  }
  throw new Error("expected the call to reject");
}

describe("apiFetch success", () => {
  it("returns parsed JSON on 2xx", async () => {
    const fetch = fakeFetch(201, { session_id: "s1", created_at: "2026-11-27T00:00:00Z" });
    const data = await apiFetch<{ session_id: string }>("/api/sessions", { method: "POST", body: {} }, { fetch });
    expect(data.session_id).toBe("s1");
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/sessions");
    expect(init.method).toBe("POST");
    expect(init.body).toBe("{}");
    const headers = new Headers(init.headers);
    expect(headers.get("content-type")).toBe("application/json");
    expect(headers.get("x-request-id")).toMatch(/^[0-9a-f-]{36}$/);
    expect(init.credentials).toBe("same-origin");
  });

  it("returns undefined on 204", async () => {
    const fetch = fakeFetch(204, undefined);
    await expect(apiFetch("/api/access", { method: "POST", body: { code: "x" } }, { fetch })).resolves.toBeUndefined();
  });
});

describe("apiFetch 429", () => {
  it("parses the envelope and uses retry_after_s from the body", async () => {
    const fetch = fakeFetch(429, envelope({ code: "ACCESS_ATTEMPTS", retry_after_s: 12 }), { "retry-after": "30" });
    const err = await rejection(apiFetch("/api/access", { method: "POST", body: { code: "x" } }, { fetch, on401: "throw" }));
    expect(err).toBeInstanceOf(ApiError);
    const e = err as ApiError;
    expect(e.status).toBe(429);
    expect(e.body.code).toBe("ACCESS_ATTEMPTS");
    expect(e.body.class).toBe("rate_limited");
    expect(e.retryAfterS).toBe(12);
    expect(e.traceId).toBe("0192f0c4-0000-7000-8000-000000000001");
  });

  it("falls back to the Retry-After header when retry_after_s is null", async () => {
    const fetch = fakeFetch(429, envelope({ retry_after_s: null }), { "retry-after": "7" });
    const e = (await rejection(apiFetch("/api/sessions", { method: "POST", body: {} }, { fetch }))) as ApiError;
    expect(e.retryAfterS).toBe(7);
  });

  it("reads an HTTP-date Retry-After relative to now", () => {
    const now = Date.parse("2026-11-27T10:00:00Z");
    expect(parseRetryAfter("Fri, 27 Nov 2026 10:00:09 GMT", now)).toBe(9);
    expect(parseRetryAfter("garbage", now)).toBeNull();
    expect(parseRetryAfter(null, now)).toBeNull();
  });
});

describe("apiFetch 401", () => {
  it("redirects to the access gate and still rejects", async () => {
    const fetch = fakeFetch(401, envelope({ class: "access_denied", code: "ACCESS_DENIED", retryable: false }));
    const redirect = vi.fn();
    const err = await rejection(apiFetch("/api/me", {}, { fetch, redirect }));
    expect(redirect).toHaveBeenCalledWith(ACCESS_GATE_PATH);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
  });

  it("does not redirect when the caller handles 401 itself (the access form)", async () => {
    const fetch = fakeFetch(401, envelope({ class: "access_denied", code: "ACCESS_DENIED", retryable: false }));
    const redirect = vi.fn();
    const e = (await rejection(apiFetch("/api/access", { method: "POST", body: { code: "x" } }, { fetch, redirect, on401: "throw" }))) as ApiError;
    expect(redirect).not.toHaveBeenCalled();
    expect(e.body.code).toBe("ACCESS_DENIED");
  });
});

describe("apiFetch unknown error shapes fail loudly", () => {
  it("rejects a FastAPI default error body", async () => {
    const fetch = fakeFetch(404, { detail: "Not Found" });
    const err = await rejection(apiFetch("/api/sessions/x", {}, { fetch }));
    expect(err).toBeInstanceOf(ContractViolationError);
    expect(String((err as Error).message)).toContain("404");
  });

  it("rejects a non-JSON proxy page", async () => {
    const fetch = fakeFetch(502, "<html>Bad Gateway</html>");
    const err = await rejection(apiFetch("/api/me", {}, { fetch }));
    expect(err).toBeInstanceOf(ContractViolationError);
    expect((err as ContractViolationError).status).toBe(502);
  });

  it("rejects an envelope with an unknown error class", async () => {
    const fetch = fakeFetch(500, envelope({ class: "something_new" }));
    const err = await rejection(apiFetch("/api/me", {}, { fetch }));
    expect(err).toBeInstanceOf(ContractViolationError);
  });

  it("rejects a 401 with an unknown shape without redirecting", async () => {
    const fetch = fakeFetch(401, { detail: "nope" });
    const redirect = vi.fn();
    const err = await rejection(apiFetch("/api/me", {}, { fetch, redirect }));
    expect(err).toBeInstanceOf(ContractViolationError);
    expect(redirect).not.toHaveBeenCalled();
  });
});
