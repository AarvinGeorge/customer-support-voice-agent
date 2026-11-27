// @vitest-environment jsdom
/**
 * What: behaviour tests for the verification and confirmation cards inside the console: they open
 *   on their events, send the doc 07 s2.3 bodies, keep the PIN out of the DOM and the store after
 *   send, send one request per press, and close into a receipt per outcome.
 * Use when: `pnpm test`.
 * Inputs: the S1 fixture plus small hand-built events, replayed into a fresh turn store; fetch is
 *   stubbed where the real sendTurn runs.
 * Output: vitest results.
 * Side effects: none (no network).
 * Role: CS-E-04.S6.T1 Done when: one test per verification result and per outcome; a double
 *   click sends one request; the DOM never holds the PIN after submit.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ActionResultEvent, ConfirmationRequiredEvent, SseEvent, TurnRequest, VerificationResultEvent } from "@contracts/contracts";
import { loadFixture, toWire } from "@/test-support/fixtures";
import { createTurnStore } from "@/lib/turns/store";

import { ConsoleShell } from "./console-shell";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

const s1 = loadFixture("s1_chat.jsonl");
const c1 = s1.filter((e) => e.turn_id.endsWith("c1"));
const c2 = s1.filter((e) => e.turn_id.endsWith("c2"));
const SID = c1[0]?.session_id as string;
const CONF = c2.find((e) => e.type === "confirmation.required") as ConfirmationRequiredEvent;
const PIN = "4821";
const TICKET = {
  ticket_id: "TCK-26118213",
  short_id: "8213",
  display: "ticket ending 8213",
  stage: "open" as const,
  priority: "P3" as const,
  queue: "billing" as const,
  target_label: "charge ending 0413",
  verified: true,
};

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/**
 * What: a fresh store with the events applied.
 * Use when: rendering the console with replayed data.
 * Inputs: events.
 * Output: a StoreApi.
 * Side effects: none.
 * Role: test helper.
 */
function storeWith(events: SseEvent[]) {
  const store = createTurnStore();
  for (const e of events) store.getState().apply(e);
  return store;
}

/**
 * What: the envelope of a hand-built event in a new turn of the S1 session.
 * Use when: building result turns.
 * Inputs: the turn id and seq.
 * Output: envelope fields.
 * Side effects: none.
 * Role: test builder.
 */
const env = (turnId: string, seq: number) => ({
  v: 1 as const,
  session_id: SID,
  turn_id: turnId,
  trace_id: turnId,
  seq,
  ts: "2026-11-27T13:01:00Z",
  mode: "chat" as const,
});

/**
 * What: a fetch stub that answers every POST with the given events as an SSE stream.
 * Use when: the real sendTurn must run.
 * Inputs: events.
 * Output: a vi.fn fetch.
 * Side effects: none.
 * Role: test double.
 */
function sseFetch(events: SseEvent[]) {
  return vi.fn(async () => new Response(toWire(events), { status: 200, headers: { "content-type": "text/event-stream" } }));
}

/**
 * What: everything the DOM holds as text: the serialized markup plus every input's live value
 *   (a controlled input's value is a property, not always an attribute).
 * Use when: proving the PIN is not in the DOM.
 * Inputs: none.
 * Output: a string.
 * Side effects: none.
 * Role: test probe; asserted to contain the PIN before submit, so its "not found" means something.
 */
function domText(): string {
  const values = [...document.querySelectorAll("input, textarea")].map((el) => (el as HTMLInputElement).value);
  return `${document.body.innerHTML}\n${values.join("\n")}`;
}

describe("verification card", () => {
  it("opens on verification.required with the account ID field, a masked PIN field and attempts left", () => {
    render(<ConsoleShell sessionId="s" store={storeWith(c1)} />);
    const card = screen.getByRole("region", { name: "Verify your account" });
    const account = within(card).getByLabelText("Account ID") as HTMLInputElement;
    const pin = within(card).getByLabelText("Support PIN") as HTMLInputElement;
    expect(account.type).toBe("text");
    expect(pin.type).toBe("password");
    expect(pin.inputMode).toBe("numeric");
    expect(pin.autocomplete).toBe("off");
    expect(within(card).getByText("3 attempts left")).toBeTruthy();
    expect((within(card).getByRole("button", { name: "Verify" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("sends the verification body (not a chat message) and the PIN is in neither the DOM nor the store after submit", async () => {
    const fetchMock = sseFetch(c2);
    vi.stubGlobal("fetch", fetchMock);
    const store = storeWith(c1);
    render(<ConsoleShell sessionId={SID} store={store} />);
    fireEvent.change(screen.getByLabelText("Account ID"), { target: { value: "qs-10423" } });
    fireEvent.change(screen.getByLabelText("Support PIN"), { target: { value: PIN } });
    expect(domText()).toContain(PIN);
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect((screen.getByLabelText("Support PIN") as HTMLInputElement).value).toBe("");
    expect(domText()).not.toContain(PIN);
    await act(async () => {
      await vi.waitFor(() => expect(store.getState().connection.phase).toBe("idle"));
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe(`/api/sessions/${SID}/turns`);
    const body = JSON.parse(init.body as string) as TurnRequest;
    expect(Object.keys(body).sort()).toEqual(["client_turn_id", "verification"]);
    expect(body.verification).toEqual({ account_id: "QS-10423", pin: PIN });
    expect(store.getState().thread.filter((t) => t.kind === "user")).toHaveLength(0);
    expect(screen.getByText("Verified as Priya Raman, owner, Pro plan")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Verify your account" })).toBeNull();
    expect(domText()).not.toContain(PIN);
    expect(JSON.stringify(store.getState())).not.toContain(PIN);
  });

  it("a verification send that fails before any event keeps no PIN in the store or DOM, and offers no retry of it", async () => {
    const body = { class: "unavailable", code: "UPSTREAM_UNAVAILABLE", message: "Our service is busy.", retryable: true, changed: false };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 503, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const store = storeWith(c1);
    render(<ConsoleShell sessionId={SID} store={store} />);
    fireEvent.change(screen.getByLabelText("Account ID"), { target: { value: "QS-10423" } });
    fireEvent.change(screen.getByLabelText("Support PIN"), { target: { value: PIN } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await act(async () => {
      await vi.waitFor(() => expect(store.getState().connection.phase).toBe("idle"));
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(store.getState())).not.toContain(PIN);
    expect(domText()).not.toContain(PIN);
    expect(screen.queryByRole("button", { name: /Retry/ })).toBeNull();
    expect(screen.getByRole("region", { name: "Verify your account" })).toBeTruthy();
  });

  it("strips non-digits from the PIN and keeps at most 4", () => {
    render(<ConsoleShell sessionId="s" store={storeWith(c1)} />);
    const pin = screen.getByLabelText("Support PIN") as HTMLInputElement;
    fireEvent.change(pin, { target: { value: "4a8-21" } });
    expect(pin.value).toBe("4821");
    expect(pin.maxLength).toBe(4);
  });

  it("a double press on Verify sends one request", () => {
    const store = storeWith(c1);
    const sendTurn = vi.fn(() => new Promise<never>(() => undefined));
    store.setState({ sendTurn });
    render(<ConsoleShell sessionId={SID} store={store} />);
    fireEvent.change(screen.getByLabelText("Account ID"), { target: { value: "QS-10423" } });
    fireEvent.change(screen.getByLabelText("Support PIN"), { target: { value: PIN } });
    const form = screen.getByRole("region", { name: "Verify your account" }).querySelector("form") as HTMLFormElement;
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(sendTurn).toHaveBeenCalledTimes(1);
  });

  const results: [VerificationResultEvent["status"], Partial<VerificationResultEvent>, string][] = [
    [
      "verified",
      { person: { display_name: "Priya Shah", role: "billing_admin", tier: "pro", contract_status: "active", account_id: "QS-10423" } },
      "Verified as Priya Shah, billing admin, Pro plan",
    ],
    ["failed", { attempts_left: 2 }, "That account ID and PIN didn't match. 2 attempts left."],
    ["locked", { ticket: TICKET }, "For your security, verification is locked for this conversation. Our team will follow up on ticket ending 8213."],
    ["conflict", {}, "This conversation is already verified as someone else. Please start a new conversation to verify as another person."],
    ["unavailable", {}, "We couldn't reach our account system, so you aren't verified yet. Please try again in a moment."],
  ];
  it.each(results)("verification.result %s renders its customer-safe line", (status, fields, text) => {
    const ev: SseEvent = { ...env("tv", 1), type: "verification.result", status, ...fields };
    render(<ConsoleShell sessionId="s" store={storeWith([...c1, ev])} />);
    expect(screen.getByText(text)).toBeTruthy();
  });

  it("after a failure the retry card shows the attempts left; a lock shows the locked card", () => {
    const retry: SseEvent[] = [
      { ...env("tr", 1), type: "verification.result", status: "failed", attempts_left: 2 },
      { ...env("tr", 2), type: "verification.required", reason: "retry", prompt_text: "p", attempts_left: 2, fields: ["account_id", "pin"] },
    ];
    const store = storeWith([...c1, ...retry]);
    render(<ConsoleShell sessionId="s" store={store} />);
    expect(screen.getAllByRole("region", { name: "Verify your account" })).toHaveLength(1);
    expect(screen.getByText("2 attempts left")).toBeTruthy();
    act(() => store.getState().apply({ ...env("tl", 1), type: "verification.result", status: "locked", ticket: TICKET }));
    const card = screen.getByRole("region", { name: "Verify your account" });
    expect(within(card).queryByLabelText("Support PIN")).toBeNull();
    expect(within(card).getByText("Verification is locked for this conversation.")).toBeTruthy();
  });
});

describe("confirmation card", () => {
  const upToC2 = [...c1, ...c2];

  it("opens on confirmation.required with title, read-back, amount, invoice, charge date, rule code and title, and expiry", () => {
    render(<ConsoleShell sessionId="s" store={storeWith(upToC2)} />);
    const card = screen.getByRole("region", { name: "Refund $96.00 for the charge on 21 November" });
    expect(within(card).getByText(/I can refund \$96\.00 for the duplicate charge on 21 November/)).toBeTruthy();
    expect(within(card).getByText("$96.00")).toBeTruthy();
    expect(within(card).getByText("invoice ending 0413")).toBeTruthy();
    expect(within(card).getByText("21 Nov 2026")).toBeTruthy();
    expect(within(card).getByText("PRO_WITHIN_LIMITS")).toBeTruthy();
    expect(within(card).getByText("Refunds on the Pro plan")).toBeTruthy();
    expect(within(card).getByText("Expires in 11:26")).toBeTruthy();
    expect(within(card).getByRole("button", { name: "Cancel" })).toBeTruthy();
    expect(within(card).getByRole("button", { name: "Confirm refund" })).toBeTruthy();
  });

  it("Confirm is the only ember element on screen", () => {
    const { container } = render(<ConsoleShell sessionId="s" store={storeWith(upToC2)} />);
    const ember = container.querySelectorAll('[class*="bg-primary"], [class*="bg-ember"], [class*="text-ember"]');
    expect(ember).toHaveLength(1);
    expect(ember[0]?.textContent).toBe("Confirm refund");
  });

  it("a double click on Confirm sends one request with the ConfirmInput body", () => {
    const store = storeWith(upToC2);
    const sendTurn = vi.fn((_sid: string, _req: TurnRequest) => new Promise<never>(() => undefined));
    store.setState({ sendTurn });
    render(<ConsoleShell sessionId={SID} store={store} />);
    const btn = screen.getByRole("button", { name: "Confirm refund" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.doubleClick(btn);
    expect(sendTurn).toHaveBeenCalledTimes(1);
    const req = sendTurn.mock.calls[0]?.[1] as TurnRequest;
    expect(Object.keys(req).sort()).toEqual(["client_turn_id", "confirm"]);
    expect(req.confirm).toEqual({ pending_action_id: "PA-3F2A9C1B", decision: "confirm" });
    expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("Cancel sends decision cancel", () => {
    const store = storeWith(upToC2);
    const sendTurn = vi.fn((_sid: string, _req: TurnRequest) => new Promise<never>(() => undefined));
    store.setState({ sendTurn });
    render(<ConsoleShell sessionId={SID} store={store} />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(sendTurn.mock.calls[0]?.[1].confirm).toEqual({ pending_action_id: "PA-3F2A9C1B", decision: "cancel" });
  });

  it("after a failed request the card can be pressed again", async () => {
    const store = storeWith(upToC2);
    const sendTurn = vi.fn(() => Promise.reject(new Error("dropped")));
    store.setState({ sendTurn });
    render(<ConsoleShell sessionId={SID} store={store} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Confirm refund" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Confirm refund" }));
    });
    expect(sendTurn).toHaveBeenCalledTimes(2);
  });

  it("buttons are disabled while another turn is streaming", () => {
    const store = storeWith(upToC2);
    store.setState({ connection: { phase: "streaming", error: null } });
    render(<ConsoleShell sessionId={SID} store={store} />);
    expect((screen.getByRole("button", { name: "Confirm refund" }) as HTMLButtonElement).disabled).toBe(true);
  });

  const outcomes: [ActionResultEvent["outcome"], Partial<ActionResultEvent>, string, string | null][] = [
    ["committed", { changed: true }, "Refund issued", "$96.00 · invoice ending 0413"],
    ["submitted", { changed: true, ticket: { ...TICKET, stage: "pending_approval" } }, "Sent for approval", "ticket ending 8213"],
    ["cancelled", { changed: false }, "Cancelled", "nothing was changed"],
    ["declined", { changed: false, reason_codes: ["PAST_DUE"] }, "Declined", "PAST_DUE · nothing was changed"],
    ["rules_changed", { changed: false, reason_codes: ["PAST_DUE"] }, "Not done, the rules changed", "PAST_DUE · nothing was changed"],
    ["expired", { changed: false }, "Request no longer active", "nothing was changed"],
    ["failed", { changed: false, ticket: TICKET }, "Refund was not issued", "nothing was changed · ticket ending 8213"],
    ["unconfirmed", { changed: null, ticket: TICKET }, "Outcome unconfirmed", "ticket ending 8213"],
    ["step_up_failed", { changed: false }, "PIN didn't match", "nothing was changed"],
    ["step_up_locked", { changed: false }, "Request cancelled after 3 PIN tries", "nothing was changed"],
    ["act_alone_applied", { changed: true }, "Change applied", null],
  ];
  it.each(outcomes)("outcome %s: a receipt line in its turn, and the card closes", (outcome, fields, title, detail) => {
    const result: SseEvent = {
      ...env("tc", 2),
      type: "action.result",
      pending_action_id: "PA-3F2A9C1B",
      action_type: "refund",
      outcome,
      changed: null,
      reason_codes: [],
      policy_refs: [],
      result_text: "",
      ticket: null,
      ...fields,
    };
    render(<ConsoleShell sessionId="s" store={storeWith([...upToC2, result])} />);
    const receipts = document.querySelectorAll('[data-slot="receipt"]');
    expect(receipts).toHaveLength(1);
    const receipt = receipts[0] as HTMLElement;
    expect(within(receipt).getByText(title)).toBeTruthy();
    if (detail !== null) expect(within(receipt).getByText(detail)).toBeTruthy();
    const card = screen.getByRole("region", { name: "Refund $96.00 for the charge on 21 November" });
    expect(within(card).queryByRole("button")).toBeNull();
    expect(within(card).getByText(title)).toBeTruthy();
  });

  it("the S1 committed result in a later turn closes the card into a receipt", () => {
    render(<ConsoleShell sessionId="s" store={storeWith(s1)} />);
    expect(screen.getAllByText("Refund issued").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByRole("button", { name: "Confirm refund" })).toBeNull();
  });

  it("a newer card for another pending action greys this one out (superseded)", () => {
    const newer: SseEvent = {
      ...CONF,
      ...env("tn", 1),
      pending: { ...CONF.pending, pending_action_id: "PA-00000002", title: "Refund $12.00 for the charge on 22 November" },
    };
    render(<ConsoleShell sessionId="s" store={storeWith([...upToC2, newer])} />);
    const old = screen.getByRole("region", { name: "Refund $96.00 for the charge on 21 November" });
    expect(within(old).queryByRole("button")).toBeNull();
    expect(within(old).getByText("Replaced by a newer request.")).toBeTruthy();
    const live = screen.getByRole("region", { name: "Refund $12.00 for the charge on 22 November" });
    expect(within(live).getByRole("button", { name: "Confirm refund" })).toBeTruthy();
  });

  it("counts down and closes as expired at the deadline", () => {
    vi.useFakeTimers({ now: new Date("2026-11-27T13:00:35Z") });
    render(<ConsoleShell sessionId="s" store={storeWith(upToC2)} />);
    expect(screen.getByText("Expires in 11:26")).toBeTruthy();
    act(() => vi.advanceTimersByTime(6_000));
    expect(screen.getByText("Expires in 11:20")).toBeTruthy();
    act(() => vi.advanceTimersByTime(681_000));
    expect(screen.queryByRole("button", { name: "Confirm refund" })).toBeNull();
    expect(screen.getByText("This request expired. Ask again if you still need it.")).toBeTruthy();
  });
});
