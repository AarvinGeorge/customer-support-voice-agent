// @vitest-environment jsdom
/**
 * What: behaviour tests for the answer states and message furniture in the console: markdown
 *   answers, the Gate 2 hold, slow and retrying lines, error rows with Retry, ticket chips, the
 *   escalation line, the decline note, the Sources row, the fallback badge, and the input bar's
 *   2,000 limit counter and pending placeholder.
 * Use when: `pnpm test`.
 * Inputs: the S1, S3, S5 and edge fixtures replayed into a fresh turn store.
 * Output: vitest results.
 * Side effects: none (no network).
 * Role: CS-E-04.S5.T3 Done when: the S3 member fixture shows the decline with its policy ref; the
 *   S5 fixture shows the fallback badge; a citation opens the help page (new tab, rel noopener).
 */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SseEvent } from "@contracts/contracts";
import { loadFixture } from "@/test-support/fixtures";
import { createTurnStore } from "@/lib/turns/store";

import { ConsoleShell } from "./console-shell";
import { InputBar } from "./input-bar";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

const s1 = loadFixture("s1_chat.jsonl");
const s3 = loadFixture("s3_decline.jsonl");
const s5 = loadFixture("s5_failures.jsonl");
const edge = loadFixture("edge_turns.jsonl");

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/**
 * What: a store with the given user lines and fixture events applied, turn by turn.
 * Use when: rendering the console with replayed data.
 * Inputs: events, user lines per turn (null for no bubble).
 * Output: a fresh StoreApi.
 * Side effects: none.
 * Role: test helper (mirrors the dev replay route).
 */
function replayed(events: readonly SseEvent[], lines: (string | null)[] = []) {
  const store = createTurnStore();
  const ids = [...new Set(events.map((e) => e.turn_id))];
  ids.forEach((id, i) => {
    const line = lines[i];
    if (line) store.getState().addUserMessage(line, events.find((e) => e.turn_id === id)?.mode ?? "chat");
    for (const e of events.filter((e) => e.turn_id === id)) store.getState().apply(e);
  });
  return store;
}

/**
 * What: the events of one turn of a fixture, cut before the first event of a type.
 * Use when: freezing a turn mid-stream.
 * Inputs: events, turn index, the type to stop before.
 * Output: events.
 * Side effects: none.
 * Role: test helper.
 */
function until(events: readonly SseEvent[], n: number, type: SseEvent["type"]): SseEvent[] {
  const id = [...new Set(events.map((e) => e.turn_id))][n];
  const turn = events.filter((e) => e.turn_id === id);
  const cut = turn.findIndex((e) => e.type === type);
  return cut === -1 ? turn : turn.slice(0, cut);
}

describe("Done when: S3, S5 and citations", () => {
  it("the S3 member fixture shows the decline with its reason code and policy ref", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s3, ["Can I get the seat charge refunded?", null])} />);
    const note = screen.getByRole("group", { name: "Refund declined" });
    expect(within(note).getByText(/Only an account owner or billing admin can make billing changes\./)).toBeTruthy();
    expect(within(note).getByText(/ROLE_NOT_PERMITTED/)).toBeTruthy();
    expect(within(note).getByText(/Billing permissions · roles-and-permissions#billing-permissions/)).toBeTruthy();
    expect(within(note).getByText(/nothing was changed/)).toBeTruthy();
  });

  it("the S5 fixture shows the fallback badge with its reason on hover and focus", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s5, ["Refund the duplicate $96 charge, please.", "Yes, go ahead."])} />);
    const badge = screen.getByRole("button", { name: "Fallback model" });
    const tip = document.getElementById(badge.getAttribute("aria-describedby") as string);
    expect(tip?.textContent).toContain("Primary model rate limited (429)");
    expect(tip?.textContent).toContain("Answered by the fallback model gpt-4.1-mini. Policy checks are unchanged.");
    expect(screen.getAllByRole("button", { name: "Fallback model" })).toHaveLength(1);
  });

  it("a citation opens the help page in a new tab with rel noopener, excerpt on hover and focus", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s3)} />);
    const sources = screen.getByRole("group", { name: "Sources" });
    const link = within(sources).getByRole("link", { name: /Billing permissions/ });
    expect(link.getAttribute("href")).toBe("https://app.notion.com/p/4257c83d5896812f8c2ecfbb7aa2cb61");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    const card = document.getElementById(link.getAttribute("aria-describedby") as string);
    expect(card?.textContent).toContain("Only an account owner or billing admin can make billing changes.");
    expect(card?.textContent).toContain("Retrieval score 0.66");
  });
});

describe("answer states", () => {
  it("holds the draft with the Gate 2 line and a skeleton while checking policy", () => {
    render(<ConsoleShell sessionId="s" store={replayed(until(s1, 3, "token"))} />);
    expect(screen.getByRole("button", { name: /Checking the answer against policy/ })).toBeTruthy();
    expect(screen.getByTestId("answer-held")).toBeTruthy();
  });

  it("shows the slow and retrying lines in words", () => {
    const e1 = edge.filter((e) => e.turn_id.endsWith("e1"));
    const slowCut = e1.slice(0, e1.findIndex((e) => e.type === "status") + 1);
    const { unmount } = render(<ConsoleShell sessionId="s" store={replayed(slowCut)} />);
    expect(screen.getByTestId("answer-status").textContent).toContain("This is taking a little longer than usual...");
    unmount();
    render(<ConsoleShell sessionId="s" store={replayed(until(s5, 1, "action.result"))} />);
    expect(screen.getByTestId("answer-status").textContent).toContain("Retrying the billing system...");
  });

  it("renders the answer as markdown", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s3)} />);
    const strong = screen.getByText("member", { selector: "strong" });
    expect(strong).toBeTruthy();
  });

  it("an error row with retryable true offers Retry that sends the customer's text again", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      calls.push(JSON.parse(init?.body as string).text);
      return new Response("", { status: 200, headers: { "content-type": "text/event-stream" } });
    }));
    const deadline = edge.filter((e) => e.turn_id.endsWith("e3"));
    const store = replayed(deadline, ["What changed on my invoice?"]);
    render(<ConsoleShell sessionId={deadline[0]?.session_id as string} store={store} />);
    expect(screen.getByText("I couldn't finish that in time.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await vi.waitFor(() => expect(calls).toEqual(["What changed on my invoice?"]));
  });

  it("offers Retry on the latest turn only, so an old error cannot resend out of order", () => {
    const deadline = edge.filter((e) => e.turn_id.endsWith("e3"));
    const later = s1.filter((e) => e.turn_id === s1[0]?.turn_id);
    render(<ConsoleShell sessionId="s" store={replayed([...deadline, ...later], ["What changed on my invoice?", "How do I add a seat?"])} />);
    expect(screen.getByText("I couldn't finish that in time.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("a non-retryable error has no Retry", () => {
    const deadline = edge.filter((e) => e.turn_id.endsWith("e3")).map((e) => (e.type === "error" ? { ...e, retryable: false } : e));
    render(<ConsoleShell sessionId="s" store={replayed(deadline, ["What changed?"])} />);
    expect(screen.getByText("I couldn't finish that in time.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});

describe("tickets and escalation", () => {
  it("shows the ticket chip with the short form, and the full id on hover and focus", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s5)} />);
    const chip = screen.getByRole("button", { name: /Ticket ending 8213/ });
    expect(chip.textContent).toContain("P2 · Billing");
    const tip = document.getElementById(chip.getAttribute("aria-describedby") as string);
    expect(tip?.textContent).toContain("TCK-26118213");
    expect(screen.getByText("Handed to our team: a system we rely on didn't respond")).toBeTruthy();
  });

  it("copies the full ticket id on click", async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<ConsoleShell sessionId="s" store={replayed(s5)} />);
    fireEvent.click(screen.getByRole("button", { name: /Ticket ending 8213/ }));
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith("TCK-26118213"));
  });
});

describe("input bar limit and placeholder", () => {
  it("shows the counter near 2,000 and blocks sending over it", () => {
    const onSend = vi.fn();
    render(<InputBar onSend={onSend} busy={false} disabled={false} />);
    const box = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(box, { target: { value: "a".repeat(1800) } });
    expect(screen.getByText("1,800 / 2,000")).toBeTruthy();
    expect(screen.getByText("1,800 / 2,000").closest("p")?.getAttribute("aria-live")).toBeNull();
    fireEvent.change(box, { target: { value: "a".repeat(2001) } });
    expect(screen.getByText("2,001 / 2,000")).toBeTruthy();
    expect(screen.getByText(/Shorten your message to send/)).toBeTruthy();
    expect(screen.getByText("2,001 / 2,000").closest("p")?.getAttribute("aria-live")).toBe("polite");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("asks for a yes while a confirmation is pending", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s1.filter((e) => e.turn_id.endsWith("c1") || e.turn_id.endsWith("c2")))} />);
    expect(screen.getByRole("textbox", { name: "Message" }).getAttribute("placeholder")).toBe("Reply yes to confirm, or no to cancel");
  });
});
