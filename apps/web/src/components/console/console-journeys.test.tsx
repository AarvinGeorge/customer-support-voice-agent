// @vitest-environment jsdom
/**
 * What: the two journeys through the real /console client entry (D-076): a tab opened without a
 *   session creates one; the signed-in hint (?as=<account id>) only pre-fills the verification card
 *   from the persona list; it never reaches any API call as identity.
 * Use when: `pnpm test`.
 * Inputs: a fake agent (personas, sessions, a turn stream from the S1 fixture) and mocked Next.js
 *   navigation with a configurable query string.
 * Output: vitest results.
 * Side effects: stubs global fetch; resets the app turn store.
 * Role: CS-E-03.S1.T9 (web half) acceptance tests.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SseEvent } from "@contracts/contracts";

import { MOCK_PERSONAS } from "@/dev-mock/fixtures";
import { turnStore } from "@/lib/turns/store";
import { loadFixture, toWire } from "@/test-support/fixtures";

import { ConsoleRoute } from "./console-route";

const nav = vi.hoisted(() => ({ query: "", replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: nav.replace }),
  useSearchParams: () => new URLSearchParams(nav.query),
}));

const c1 = loadFixture("s1_chat.jsonl").filter((e) => e.turn_id.endsWith("c1"));
const SID = c1[0]?.session_id as string;
const PRIYA = MOCK_PERSONAS.find((p) => p.account_id === "QS-10423")!;

/** One recorded request to the fake agent. */
interface Call {
  method: string;
  path: string;
  body: unknown;
  headers: Record<string, string>;
}

/**
 * What: a fake agent for the console: personas, sessions and turns.
 * Use when: each test installs it as global fetch.
 * Inputs: { sessionId, turn } (the session POST /api/sessions returns; the events each turn streams).
 * Output: { fetchMock, calls }.
 * Side effects: none outside the returned objects.
 * Role: test double.
 */
function fakeAgent(opts: { sessionId?: string; turn?: SseEvent[] } = {}) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path, body, headers: Object.fromEntries(new Headers(init?.headers).entries()) });
    if (method === "GET" && path === "/api/demo/personas") return new Response(JSON.stringify(MOCK_PERSONAS), { status: 200 });
    if (method === "POST" && path === "/api/sessions") {
      return new Response(JSON.stringify({ session_id: opts.sessionId ?? SID, created_at: "2026-11-27T12:00:00Z" }), { status: 201 });
    }
    if (method === "POST" && path.endsWith("/turns")) {
      return new Response(toWire(opts.turn ?? c1), { status: 200, headers: { "content-type": "text/event-stream" } });
    }
    const error = { class: "not_found", code: "NOT_FOUND", message: "Not found.", retryable: false, changed: false, retry_after_s: null, fault_injected: false, trace_id: null, details: {} };
    return new Response(JSON.stringify({ error }), { status: 404, headers: { "content-type": "application/json" } });
  });
  return { fetchMock, calls };
}

/**
 * What: sends one chat message through the input bar.
 * Use when: opening the verification card (the S1 c1 turn asks for it).
 * Inputs: the text.
 * Output: none.
 * Side effects: one POST .../turns.
 * Role: test helper.
 */
function sendMessage(text: string) {
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.keyDown(box, { key: "Enter" });
}

beforeEach(() => {
  nav.replace.mockReset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  turnStore.getState().reset();
});

describe("a console tab opened from the landing page", () => {
  it("without a session creates exactly one fresh session (empty body) and keeps the hint in the URL", async () => {
    nav.query = "as=QS-10423";
    const { fetchMock, calls } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleRoute />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`/console?session=${SID}&as=QS-10423`));
    const posts = calls.filter((c) => c.path === "/api/sessions");
    expect(posts).toEqual([{ method: "POST", path: "/api/sessions", body: {}, headers: expect.any(Object) }]);
  });

  it("the signed-out tab (no hint) creates a session and opens /console?session=<id> only", async () => {
    nav.query = "";
    const { fetchMock, calls } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleRoute />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`/console?session=${SID}`));
    expect(calls.filter((c) => c.path === "/api/sessions")).toHaveLength(1);
    expect(calls.some((c) => c.path === "/api/demo/personas")).toBe(false);
  });

  it("under Strict Mode a sessionless tab still makes exactly one session request (single-flight guard)", async () => {
    nav.query = "as=QS-10423";
    const { fetchMock, calls } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    render(
      <StrictMode>
        <ConsoleRoute />
      </StrictMode>,
    );
    await waitFor(() => expect(nav.replace).toHaveBeenCalled());
    await act(async () => {});
    expect(calls.filter((c) => c.path === "/api/sessions")).toHaveLength(1);
    expect(nav.replace).toHaveBeenCalledTimes(1);
  });

  it("a failed session start shows a lasting error with Try again, and Try again starts the session", async () => {
    nav.query = "";
    let refuse = true;
    const { fetchMock: ok, calls } = fakeAgent();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (refuse && String(input) === "/api/sessions") {
          const error = { class: "rate_limited", code: "IP_SESSIONS", message: "Too many new sessions.", retryable: true, changed: false, retry_after_s: 30, fault_injected: false, trace_id: null, details: {} };
          return new Response(JSON.stringify({ error }), { status: 429, headers: { "content-type": "application/json" } });
        }
        return ok(input, init);
      }),
    );
    render(<ConsoleRoute />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Couldn't start a conversation.");
    expect(alert.textContent).toContain("Too many new sessions.");
    expect(nav.replace).not.toHaveBeenCalled();
    refuse = false;
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Try again" })));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`/console?session=${SID}`));
    expect(calls.filter((c) => c.path === "/api/sessions")).toHaveLength(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a 401 while creating the session returns to the access page", async () => {
    nav.query = "";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        const error = { class: "access_denied", code: "ACCESS_REQUIRED", message: "Enter the access code to continue.", retryable: false, changed: false, retry_after_s: null, fault_injected: false, trace_id: null, details: {} };
        return new Response(JSON.stringify({ error }), { status: 401, headers: { "content-type": "application/json" } });
      }),
    );
    render(<ConsoleRoute />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/"));
  });
});

describe("the verification card and the display-only hint", () => {
  it("with a known hint shows 'Verify as <Name>', pre-fills the account ID and leaves the PIN empty", async () => {
    nav.query = `session=${SID}&as=${PRIYA.account_id}`;
    const { fetchMock } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleRoute />);
    sendMessage("I was charged twice");
    const card = await screen.findByRole("region", { name: `Verify as ${PRIYA.name}` });
    expect((screen.getByLabelText("Account ID") as HTMLInputElement).value).toBe(PRIYA.account_id);
    expect((screen.getByLabelText("Support PIN") as HTMLInputElement).value).toBe("");
    expect(card).toBeTruthy();
  });

  it("without a hint the card says 'Verify your account' with empty fields", async () => {
    nav.query = `session=${SID}`;
    const { fetchMock } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleRoute />);
    sendMessage("I was charged twice");
    expect(await screen.findByRole("region", { name: "Verify your account" })).toBeTruthy();
    expect((screen.getByLabelText("Account ID") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Support PIN") as HTMLInputElement).value).toBe("");
  });

  it("a hint that matches no persona is ignored (no name, no pre-fill)", async () => {
    nav.query = `session=${SID}&as=QS-99999`;
    const { fetchMock } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleRoute />);
    sendMessage("I was charged twice");
    expect(await screen.findByRole("region", { name: "Verify your account" })).toBeTruthy();
    expect((screen.getByLabelText("Account ID") as HTMLInputElement).value).toBe("");
  });

  it("the hint never reaches an API call as identity: verification posts only the typed account ID and PIN", async () => {
    nav.query = `as=${PRIYA.account_id}`;
    const { fetchMock, calls } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = render(<ConsoleRoute />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`/console?session=${SID}&as=${PRIYA.account_id}`));
    nav.query = `session=${SID}&as=${PRIYA.account_id}`;
    rerender(<ConsoleRoute />);
    sendMessage("I was charged twice");
    await screen.findByRole("region", { name: `Verify as ${PRIYA.name}` });

    // Before the customer presses Verify, nothing sent so far carries the hint anywhere.
    const before = calls.map((c) => JSON.stringify(c));
    expect(before.length).toBeGreaterThan(0);
    for (const line of before) expect(line).not.toContain(PRIYA.account_id);
    const textTurn = calls.find((c) => c.path.endsWith("/turns"))?.body as Record<string, unknown>;
    expect(Object.keys(textTurn).sort()).toEqual(["client_turn_id", "text"]);

    // The customer edits the pre-filled account ID; the typed value, not the hint, is sent.
    fireEvent.change(screen.getByLabelText("Account ID"), { target: { value: "QS-20117" } });
    expect(screen.getByRole("region", { name: "Verify your account" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Support PIN"), { target: { value: "1234" } });
    const form = screen.getByRole("region", { name: "Verify your account" }).querySelector("form") as HTMLFormElement;
    await act(async () => fireEvent.submit(form));
    await waitFor(() => expect(calls.filter((c) => c.path.endsWith("/turns"))).toHaveLength(2));
    const verifyTurn = calls.filter((c) => c.path.endsWith("/turns"))[1]?.body as Record<string, unknown>;
    expect(Object.keys(verifyTurn).sort()).toEqual(["client_turn_id", "verification"]);
    expect(verifyTurn.verification).toEqual({ account_id: "QS-20117", pin: "1234" });
    for (const line of calls.map((c) => JSON.stringify(c))) expect(line).not.toContain(PRIYA.account_id);
  });
});

describe("the hint goes stale (review PR #81 M-2)", () => {
  it("a changed ?as= to an unknown id drops the old name in the same mount", async () => {
    nav.query = `session=${SID}&as=${PRIYA.account_id}`;
    const { fetchMock } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = render(<ConsoleRoute />);
    sendMessage("I was charged twice");
    await screen.findByRole("region", { name: `Verify as ${PRIYA.name}` });
    nav.query = `session=${SID}&as=QS-99999`;
    rerender(<ConsoleRoute />);
    // Gone at once (keyed on ?as=), and still gone after the persona list answered with no match.
    expect(screen.queryByRole("region", { name: `Verify as ${PRIYA.name}` })).toBeNull();
    await act(async () => {});
    expect(screen.queryByRole("region", { name: `Verify as ${PRIYA.name}` })).toBeNull();
  });

  it("once the session is verified the hint no longer names anyone on a later card", async () => {
    nav.query = `session=${SID}&as=${PRIYA.account_id}`;
    const { fetchMock } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleRoute />);
    sendMessage("I was charged twice");
    await screen.findByRole("region", { name: `Verify as ${PRIYA.name}` });
    const base = c1[0]!;
    const env = (seq: number) => ({ v: 1 as const, seq, session_id: SID, turn_id: "tv-verified", trace_id: base.trace_id, ts: base.ts, mode: "chat" as const });
    act(() => {
      turnStore.getState().apply({ ...env(1), type: "turn.started" } as SseEvent);
      turnStore.getState().apply({
        ...env(2),
        type: "verification.result",
        status: "verified",
        person: { display_name: "Dana Okafor", role: "owner", tier: "business", contract_status: "active", account_id: "QS-20117" },
      } as SseEvent);
      turnStore.getState().apply({ ...env(3), type: "verification.required", reason: "account_lookup", prompt_text: "p", attempts_left: 3, fields: ["account_id", "pin"] } as SseEvent);
    });
    expect(screen.queryByRole("region", { name: `Verify as ${PRIYA.name}` })).toBeNull();
  });
});

describe("New conversation in the console header (D-075)", () => {
  it("opens a fresh session in the same tab and drops the display-only hint", async () => {
    nav.query = `session=${SID}&as=${PRIYA.account_id}`;
    const NEW = "019a7c2e-9999-7000-8000-00000000b001";
    const { fetchMock, calls } = fakeAgent({ sessionId: NEW });
    vi.stubGlobal("fetch", fetchMock);
    render(<ConsoleRoute />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "New conversation" })));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`/console?session=${NEW}`));
    expect(calls.filter((c) => c.path === "/api/sessions")).toEqual([{ method: "POST", path: "/api/sessions", body: {}, headers: expect.any(Object) }]);
  });
});
