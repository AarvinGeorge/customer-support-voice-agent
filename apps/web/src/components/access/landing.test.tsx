// @vitest-environment jsdom
/**
 * What: behaviour tests for the landing page: access gate, persona list, copy, Start.
 * Use when: `pnpm test`.
 * Inputs: a fake fetch that follows doc 07 s2.2 shapes; MOCK_PERSONAS fixtures.
 * Output: vitest results.
 * Side effects: stubs global fetch and next/navigation per test.
 * Role: CS-E-04.S1.T1 acceptance tests (right code, 401, 429 countdown, Start, copy).
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MOCK_ACCESS_CODE, MOCK_PERSONAS } from "@/dev-mock/fixtures";

import { Landing } from "./landing";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

/**
 * What: builds a doc 07 s1.3 error Response.
 * Use when: the fake server answers a non-2xx.
 * Inputs: status, class, code, message, retry_after_s.
 * Output: a Response.
 * Side effects: none.
 * Role: test helper.
 */
function errorResponse(status: number, cls: string, code: string, message: string, retryAfter?: number): Response {
  const body = {
    error: {
      class: cls,
      code,
      message,
      retryable: status === 429,
      changed: false,
      retry_after_s: retryAfter ?? null,
      fault_injected: false,
      trace_id: null,
      details: {},
    },
  };
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/**
 * What: a fake agent for the four landing routes, with an optional 429 for /api/access.
 * Use when: each test installs it as global fetch.
 * Inputs: { accessRateLimitedFor?, sessionsRateLimitedFor?: seconds }.
 * Output: { fetch mock, calls list }.
 * Side effects: none outside the returned objects.
 * Role: test double for the lane B endpoints (CS-E-04.S1.T3) not yet built.
 */
function fakeAgent(opts: { accessRateLimitedFor?: number; sessionsRateLimitedFor?: number } = {}) {
  let authed = false;
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path, body });
    if (method === "POST" && path === "/api/access") {
      if (opts.accessRateLimitedFor !== undefined) {
        return errorResponse(429, "rate_limited", "ACCESS_ATTEMPTS", "Too many attempts.", opts.accessRateLimitedFor);
      }
      if ((body as { code: string }).code === MOCK_ACCESS_CODE) {
        authed = true;
        return new Response(null, { status: 204 });
      }
      return errorResponse(401, "access_denied", "ACCESS_DENIED", "That access code did not work. Check your invitation and try again.");
    }
    if (!authed) return errorResponse(401, "access_denied", "ACCESS_REQUIRED", "Enter the access code to continue.");
    if (method === "GET" && path === "/api/demo/personas") {
      return new Response(JSON.stringify(MOCK_PERSONAS), { status: 200 });
    }
    if (method === "POST" && path === "/api/sessions") {
      if (opts.sessionsRateLimitedFor !== undefined) {
        return errorResponse(429, "rate_limited", "IP_SESSIONS", "Too many new sessions.", opts.sessionsRateLimitedFor);
      }
      return new Response(JSON.stringify({ session_id: "0193a7c2-0000-7000-8000-000000000001", created_at: "2026-11-27T12:00:00Z" }), {
        status: 201,
      });
    }
    return errorResponse(404, "not_found", "NOT_FOUND", "Not found.");
  });
  return { fetchMock, calls };
}

beforeEach(() => {
  push.mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Landing", () => {
  it("shows the access gate first, then the personas after the right code", async () => {
    const { fetchMock, calls } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Landing />);

    const field = await screen.findByLabelText("Access code");
    expect(screen.getByText(/This demo records conversations/)).toBeTruthy();
    await user.type(field, MOCK_ACCESS_CODE);
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(await screen.findByRole("heading", { name: "Choose who you are" })).toBeTruthy();
    const priya = screen.getByRole("listitem", { name: /Priya Shah/ });
    expect(within(priya).getByText("QS-10423")).toBeTruthy();
    expect(within(priya).getByText("4821")).toBeTruthy();
    expect(within(priya).getByText("Billing admin")).toBeTruthy();
    expect(within(priya).getByText(/Pro/)).toBeTruthy();
    expect(within(priya).getByText("S1, S2, S5")).toBeTruthy();
    expect(within(priya).getByText(MOCK_PERSONAS[0]!.description)).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(MOCK_PERSONAS.length);
    expect(screen.getByText(/This demo records conversations/)).toBeTruthy();
    expect(calls.find((c) => c.path === "/api/access")?.body).toEqual({ code: MOCK_ACCESS_CODE });
  });

  it("skips the gate when the access cookie is already valid", async () => {
    const { fetchMock } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const first = render(<Landing />);
    await user.type(await screen.findByLabelText("Access code"), MOCK_ACCESS_CODE);
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await screen.findByRole("heading", { name: "Choose who you are" });
    first.unmount();

    render(<Landing />);
    expect(await screen.findByRole("heading", { name: "Choose who you are" })).toBeTruthy();
    expect(screen.queryByLabelText("Access code")).toBeNull();
  });

  it("shows the 401 message for a wrong code and keeps the gate", async () => {
    const { fetchMock } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Landing />);

    await user.type(await screen.findByLabelText("Access code"), "wrong-code");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(await screen.findByText("That access code did not work. Check your invitation and try again.")).toBeTruthy();
    expect(screen.getByLabelText("Access code").getAttribute("aria-invalid")).toBe("true");
    expect(screen.queryByRole("heading", { name: "Choose who you are" })).toBeNull();
  });

  it("shows a countdown from retry_after_s on 429 and disables Continue until it ends", async () => {
    const { fetchMock } = fakeAgent({ accessRateLimitedFor: 3 });
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<Landing />);

    const field = await screen.findByLabelText("Access code");
    fireEvent.change(field, { target: { value: "anything" } });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));

    expect(await screen.findByText(/Try again in 3 s/)).toBeTruthy();
    expect((screen.getByRole("button", { name: /continue/i }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByText(/Try again in 2 s/)).toBeTruthy();
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.queryByText(/Try again in/)).toBeNull();
    expect((screen.getByRole("button", { name: /continue/i }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("Start creates a session, then opens the console with it", async () => {
    const { fetchMock, calls } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Landing />);
    await user.type(await screen.findByLabelText("Access code"), MOCK_ACCESS_CODE);
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await screen.findByRole("heading", { name: "Choose who you are" });

    const dana = screen.getByRole("listitem", { name: /Dana Okafor/ });
    await user.click(within(dana).getByRole("button", { name: /start/i }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/console?session=0193a7c2-0000-7000-8000-000000000001"));
    const post = calls.find((c) => c.path === "/api/sessions");
    expect(post).toEqual({ method: "POST", path: "/api/sessions", body: {} });
  });

  it("the copy button copies that persona's PIN", async () => {
    const { fetchMock } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Landing />);
    await user.type(await screen.findByLabelText("Access code"), MOCK_ACCESS_CODE);
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await screen.findByRole("heading", { name: "Choose who you are" });

    const marcus = screen.getByRole("listitem", { name: /Marcus Lee/ });
    await user.click(within(marcus).getByRole("button", { name: /copy pin/i }));

    expect(await navigator.clipboard.readText()).toBe("1937");
    expect(await within(marcus).findByText("Copied")).toBeTruthy();
  });

  it("moves focus to the persona heading after the code is accepted, not on a cold load", async () => {
    const { fetchMock } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const first = render(<Landing />);
    await user.type(await screen.findByLabelText("Access code"), MOCK_ACCESS_CODE);
    await user.click(screen.getByRole("button", { name: /continue/i }));
    const heading = await screen.findByRole("heading", { name: "Choose who you are" });
    await waitFor(() => expect(document.activeElement).toBe(heading));
    first.unmount();

    render(<Landing />);
    const again = await screen.findByRole("heading", { name: "Choose who you are" });
    expect(document.activeElement).not.toBe(again);
  });

  it("two Start clicks in one batch create one session", async () => {
    const { fetchMock, calls } = fakeAgent();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Landing />);
    await user.type(await screen.findByLabelText("Access code"), MOCK_ACCESS_CODE);
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await screen.findByRole("heading", { name: "Choose who you are" });

    const start = within(screen.getByRole("listitem", { name: /Sam Rivera/ })).getByRole("button", { name: /start/i });
    act(() => {
      start.click();
      start.click();
    });
    await waitFor(() => expect(push).toHaveBeenCalledTimes(1));
    expect(calls.filter((c) => c.path === "/api/sessions")).toHaveLength(1);
  });

  it("clears the Start rate-limit message when its countdown ends", async () => {
    const { fetchMock } = fakeAgent({ sessionsRateLimitedFor: 2 });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<Landing />);
    await user.type(await screen.findByLabelText("Access code"), MOCK_ACCESS_CODE);
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await screen.findByRole("heading", { name: "Choose who you are" });

    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(within(screen.getByRole("listitem", { name: /Dana Okafor/ })).getByRole("button", { name: /start/i }));
    expect(await screen.findByText(/Too many new sessions\. Try again in 2 s/)).toBeTruthy();
    expect(push).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.queryByText(/Too many new sessions/)).toBeNull();
  });

  it("shows long waits in minutes", async () => {
    const { fetchMock } = fakeAgent({ accessRateLimitedFor: 3540 });
    vi.stubGlobal("fetch", fetchMock);
    render(<Landing />);
    fireEvent.change(await screen.findByLabelText("Access code"), { target: { value: "anything" } });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    expect(await screen.findByText(/Try again in 59 min/)).toBeTruthy();
  });
});
