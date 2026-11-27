// @vitest-environment jsdom
/**
 * What: behaviour tests for CS-E-07.S1.T4: the demo fault toggle in the drawer's Demo controls,
 *   the 429 and 503 banners with a countdown that keep the draft, and the "Create ticket" button
 *   after both models were unavailable.
 * Use when: `pnpm test`.
 * Inputs: the S4 fixture (with its escalation turn rewritten to model_unavailable), fake fetch
 *   responses, fake timers.
 * Output: vitest results.
 * Side effects: stubs the global fetch per test (restored after each).
 * Role: Done when: the header goes on exactly one request; the toggle is hidden when the flag is
 *   false; a 429 shows the countdown and keeps the draft.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Me, SseEvent } from "@contracts/contracts";

import { loadFixture, toWire } from "@/test-support/fixtures";
import { createTurnStore } from "@/lib/turns/store";

import { ConsoleShell } from "./console-shell";
import { DemoControls } from "./demo-controls";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

const s1 = loadFixture("s1_chat.jsonl");
const s4 = loadFixture("s4_unsure.jsonl");
const SID = s1[0]?.session_id as string;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/**
 * What: a loader for GET /api/me with the given flag.
 * Use when: rendering DemoControls.
 * Inputs: the demo_faults_enabled value.
 * Output: an async function returning Me.
 * Side effects: none.
 * Role: test seam.
 */
const me = (flag: boolean) => async (): Promise<Me> => ({ visitor: true, operator: false, demo_faults_enabled: flag });

/**
 * What: a fetch stub that streams one S1 turn per call (moved to the given session) and records
 *   the request headers and bodies.
 * Use when: sending turns from the shell.
 * Inputs: the session id the events should carry (default the S1 session).
 * Output: the vi.fn.
 * Side effects: none.
 * Role: test seam.
 */
function streamingFetch(sessionId: string = SID) {
  const ids = [...new Set(s1.map((e) => e.turn_id))];
  let call = 0;
  return vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => {
    const id = ids[call++ % ids.length];
    const events = s1.filter((e) => e.turn_id === id).map((e) => ({ ...e, session_id: sessionId }));
    return new Response(toWire(events), { status: 200, headers: { "content-type": "text/event-stream" } });
  });
}

/**
 * What: a fetch stub that answers one error envelope.
 * Use when: 429 and 503 tests.
 * Inputs: status, code, class, retry_after_s, details.
 * Output: the vi.fn.
 * Side effects: none.
 * Role: test seam.
 */
function errorFetch(status: number, code: string, cls: string, retryAfter: number | null, details: Record<string, unknown> = {}) {
  const error = { class: cls, code, message: "m", retryable: true, changed: false, retry_after_s: retryAfter, fault_injected: false, details };
  return vi.fn(async () => new Response(JSON.stringify({ error }), { status, headers: { "content-type": "application/json" } }));
}

/**
 * What: types a message into the input bar and presses Send.
 * Use when: sending from the shell.
 * Inputs: the text.
 * Output: none.
 * Side effects: DOM events.
 * Role: test helper.
 */
function typeAndSend(text: string) {
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

/**
 * What: waits until the message field holds the given text.
 * Use when: a refused draft is restored. The restore runs in a passive effect after the banner
 *   commits, so `findByRole` on the banner can resolve one tick before the field is filled.
 * Inputs: the expected text.
 * Output: a promise.
 * Side effects: none.
 * Role: test helper (removes a load-dependent race, CS-E-07.S1 flake fix).
 */
const fieldHolds = (text: string) =>
  waitFor(() => expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe(text));

describe("Demo controls: fault toggle", () => {
  it("is hidden when /api/me says demo_faults_enabled is false", async () => {
    const loadMe = vi.fn(me(false));
    const { container } = render(<DemoControls store={createTurnStore()} loadMe={loadMe} />);
    await waitFor(() => expect(loadMe).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("switch")).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("is hidden when /api/me fails", async () => {
    const loadMe = vi.fn(async () => {
      throw new Error("down");
    });
    render(<DemoControls store={createTurnStore()} loadMe={loadMe} />);
    await waitFor(() => expect(loadMe).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("shows two switches when the flag is true; only one fault can be armed at a time", async () => {
    const store = createTurnStore();
    render(<DemoControls store={store} loadMe={me(true)} />);
    const tool = await screen.findByRole("switch", { name: /Billing tool error on the next message/ });
    const llm = screen.getByRole("switch", { name: /Primary model rate limit \(429\) on the next message/ });
    expect(tool.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(tool);
    expect(store.getState().armedFault).toBe("tool_error");
    expect(tool.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(llm);
    expect(store.getState().armedFault).toBe("llm_429");
    expect(tool.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(llm);
    expect(store.getState().armedFault).toBeNull();
  });

  it("Done when: the header goes on exactly one request, then the switch visibly turns off", async () => {
    const fetchMock = streamingFetch();
    vi.stubGlobal("fetch", fetchMock);
    const store = createTurnStore();
    render(<ConsoleShell sessionId={SID} store={store} demoControls={<DemoControls store={store} loadMe={me(true)} />} />);
    fireEvent.click(screen.getByRole("button", { name: /inspector/i }));
    const drawer = screen.getByRole("complementary", { name: "Inspector" });
    fireEvent.click(await within(drawer).findByRole("switch", { name: /Billing tool error/ }));
    typeAndSend("first");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(store.getState().connection.phase).toBe("idle"));
    expect(within(drawer).getByRole("switch", { name: /Billing tool error/ }).getAttribute("aria-checked")).toBe("false");
    expect(within(drawer).getByText("Sent X-Demo-Fault: tool_error with your last message, then switched off.")).toBeTruthy();
    typeAndSend("second");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(store.getState().connection.phase).toBe("idle"));
    const headers = fetchMock.mock.calls.map((c) => new Headers((c[1] as RequestInit).headers).get("x-demo-fault"));
    expect(headers).toEqual(["tool_error", null]);
  });
});

describe("429 and 503 banners", () => {
  it("Done when: a 429 shows the countdown, keeps the draft and unlocks Send when it ends", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("fetch", errorFetch(429, "SESSION_TURNS", "rate_limited", 42, { limit: "10/minute", scope: "session" }));
    const store = createTurnStore();
    render(<ConsoleShell sessionId={SID} store={store} />);
    typeAndSend("Can you also email me the receipt?");
    const banner = await screen.findByRole("status", { name: "Sending paused" });
    expect(within(banner).getByText("You're sending messages quickly. You can send again shortly; your draft is kept.")).toBeTruthy();
    expect(within(banner).getByText("0:42")).toBeTruthy();
    await fieldHolds("Can you also email me the receipt?");
    expect(screen.getByRole("button", { name: "Send" }).getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByText("Limit: 10 messages per minute in this conversation. Send unlocks when the countdown ends.")).toBeTruthy();
    expect(screen.queryByText("Can you also email me the receipt?", { selector: "[aria-label='You said'] *" })).toBeNull();
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(within(banner).getByText("0:40")).toBeTruthy();
    await act(async () => {
      vi.advanceTimersByTime(41_000);
    });
    expect(screen.queryByRole("status", { name: "Sending paused" })).toBeNull();
    expect(screen.getByRole("button", { name: "Send" }).getAttribute("aria-disabled")).toBe("false");
    expect(store.getState().limit).toBeNull();
  });

  it("a 503 BUSY shows the busy banner with its short countdown and keeps the draft", async () => {
    vi.stubGlobal("fetch", errorFetch(503, "BUSY", "admission_rejected", 3));
    render(<ConsoleShell sessionId={SID} store={createTurnStore()} />);
    typeAndSend("Is my refund done?");
    const banner = await screen.findByRole("status", { name: "Sending paused" });
    expect(within(banner).getByText("We're handling a lot of conversations right now. You can send again shortly; your draft is kept.")).toBeTruthy();
    expect(within(banner).getByText("0:03")).toBeTruthy();
    await fieldHolds("Is my refund done?");
  });

  it("a draft the customer typed during the wait is not overwritten", async () => {
    vi.stubGlobal("fetch", errorFetch(429, "SESSION_TURNS", "rate_limited", 42));
    const store = createTurnStore();
    render(<ConsoleShell sessionId={SID} store={store} />);
    typeAndSend("first draft");
    await screen.findByRole("status", { name: "Sending paused" });
    await fieldHolds("first draft");
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "edited" } });
    act(() => store.setState({ thread: [...store.getState().thread] }));
    expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe("edited");
  });
});

describe("a refused message is never lost (fix round 1, I1)", () => {
  it("typed while in flight: the refused text shows as a draft chip and Restore keeps both", async () => {
    let release: (r: Response) => void = () => undefined;
    const refusal = errorFetch(503, "BUSY", "admission_rejected", 3);
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((res) => (release = res))));
    render(<ConsoleShell sessionId={SID} store={createTurnStore()} />);
    typeAndSend("FIRST message");
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "second" } });
    await act(async () => release(await refusal()));
    const chip = await screen.findByRole("group", { name: "Message not sent" });
    expect(within(chip).getByText("FIRST message")).toBeTruthy();
    expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe("second");
    fireEvent.click(within(chip).getByRole("button", { name: "Restore" }));
    expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe("FIRST message\nsecond");
    expect(screen.queryByRole("group", { name: "Message not sent" })).toBeNull();
  });

  it("Retry from an older row while the field holds text: the retried text lands in the chip", async () => {
    const fail = vi.fn(async () => new Response(JSON.stringify({ error: { class: "internal", code: "X", message: "boom", retryable: true, changed: false, retry_after_s: null, fault_injected: false, details: {} } }), { status: 500, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fail);
    render(<ConsoleShell sessionId={SID} store={createTurnStore()} />);
    typeAndSend("old");
    const retry = await screen.findByRole("button", { name: "Retry" });
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "hello" } });
    vi.stubGlobal("fetch", errorFetch(429, "SESSION_TURNS", "rate_limited", 30));
    fireEvent.click(retry);
    const chip = await screen.findByRole("group", { name: "Message not sent" });
    expect(within(chip).getByText("old")).toBeTruthy();
    expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe("hello");
  });

  it("an empty field gets the text back and no chip", async () => {
    vi.stubGlobal("fetch", errorFetch(429, "SESSION_TURNS", "rate_limited", 30));
    render(<ConsoleShell sessionId={SID} store={createTurnStore()} />);
    typeAndSend("plain");
    await screen.findByRole("status", { name: "Sending paused" });
    expect(screen.queryByRole("group", { name: "Message not sent" })).toBeNull();
    await fieldHolds("plain");
  });
});

describe("the wait is announced once (fix round 1, I2)", () => {
  /** What: text of every polite live region. Use when: announcement tests. Inputs: none. Output: strings. Side effects: none. Role: test helper. */
  const live = () => [...document.querySelectorAll("[aria-live='polite'], [role='status']")].filter((n) => !n.closest("[aria-hidden='true']")).map((n) => {
    const c = n.cloneNode(true) as Element;
    c.querySelectorAll("[aria-hidden='true']").forEach((h) => h.remove());
    return c.textContent ?? "";
  }).join(" | ");

  it("announces the wait length when the banner appears and again when sending unlocks, not every second", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("fetch", errorFetch(429, "SESSION_TURNS", "rate_limited", 42));
    render(<ConsoleShell sessionId={SID} store={createTurnStore()} />);
    typeAndSend("hi");
    await screen.findByRole("status", { name: "Sending paused" });
    await waitFor(() => expect(live()).toContain("You can send again in about 42 seconds."));
    const seen = new Set<string>();
    for (let i = 0; i < 5; i++) {
      await act(async () => void vi.advanceTimersByTime(1000));
      seen.add(live());
    }
    expect(seen.size).toBe(1);
    await act(async () => void vi.advanceTimersByTime(40_000));
    expect(live()).toContain("You can send again.");
    expect(live()).not.toContain("about 42 seconds");
  });
});

describe("minors (fix round 1)", () => {
  it("M1: Retry on an error row is not offered during the countdown", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { class: "internal", code: "X", message: "boom", retryable: true, changed: false, retry_after_s: null, fault_injected: false, details: {} } }), { status: 500, headers: { "content-type": "application/json" } })));
    const store = createTurnStore();
    render(<ConsoleShell sessionId={SID} store={store} />);
    typeAndSend("old");
    await screen.findByRole("button", { name: "Retry" });
    vi.stubGlobal("fetch", errorFetch(429, "SESSION_TURNS", "rate_limited", 30));
    typeAndSend("again");
    await screen.findByRole("status", { name: "Sending paused" });
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("M3: a refused card press does not claim a draft is kept", async () => {
    vi.stubGlobal("fetch", errorFetch(429, "SESSION_TURNS", "rate_limited", 30));
    const store = createTurnStore();
    render(<ConsoleShell sessionId={SID} store={store} />);
    await act(async () => void store.getState().sendTurn(SID, { client_turn_id: "t1", ticket_offer: { accept: true } } as never).catch(() => undefined));
    const banner = await screen.findByRole("status", { name: "Sending paused" });
    expect(within(banner).queryByText(/draft is kept/)).toBeNull();
    expect(within(banner).getByText(/Try that again when the countdown ends/)).toBeTruthy();
  });

  it("M4: the first render of the countdown already shows the wait, never 0:00", async () => {
    vi.stubGlobal("fetch", errorFetch(429, "SESSION_TURNS", "rate_limited", 42));
    const seen: string[] = [];
    const obs = new MutationObserver(() => seen.push(document.body.textContent ?? ""));
    obs.observe(document.body, { subtree: true, childList: true, characterData: true });
    render(<ConsoleShell sessionId={SID} store={createTurnStore()} />);
    typeAndSend("hi");
    await screen.findByRole("status", { name: "Sending paused" });
    obs.disconnect();
    expect(seen.some((t) => t.includes("0:00"))).toBe(false);
  });
});

describe("Create ticket after both models were unavailable (doc 07 s6.7)", () => {
  /**
   * What: S4 with its escalation turn rewritten to a model_unavailable apology with the button.
   * Use when: the ticket offer tests.
   * Inputs: none.
   * Output: events.
   * Side effects: none.
   * Role: fixture variant (the contracts fixtures have no model_unavailable turn).
   */
  function unavailable(): SseEvent[] {
    const last = s4.at(-1)?.turn_id;
    return s4.map((e) => {
      if (e.turn_id !== last) return e;
      if (e.type === "escalated") return { ...e, reason: "model_unavailable", ticket: null, origin: "code", offer_ticket_button: true };
      if (e.type === "turn.completed") {
        return { ...e, final: { ...e.final, text: "Our assistant is having trouble right now, so I haven't changed anything. Would you like me to open a ticket for our team?", ticket: null } };
      }
      return e;
    });
  }

  it("shows Create ticket on the offering turn and posts ticket_offer {accept: true} once", async () => {
    const sid = s4[0]?.session_id as string;
    const fetchMock = streamingFetch(sid);
    vi.stubGlobal("fetch", fetchMock);
    const store = createTurnStore();
    for (const e of unavailable()) store.getState().apply(e);
    render(<ConsoleShell sessionId={sid} store={store} />);
    expect(screen.getByText("No ticket is created unless you ask.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create ticket" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const body = JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body));
    expect(body.ticket_offer).toEqual({ accept: true });
    expect(body.text).toBeUndefined();
    await waitFor(() => expect(store.getState().connection.phase).toBe("idle"));
    expect(screen.queryByRole("button", { name: "Create ticket" })).toBeNull();
  });

  it("shows no button for an escalation that already opened a ticket", () => {
    const store = createTurnStore();
    for (const e of s4) store.getState().apply(e);
    render(<ConsoleShell sessionId={SID} store={store} />);
    expect(screen.queryByRole("button", { name: "Create ticket" })).toBeNull();
  });
});
