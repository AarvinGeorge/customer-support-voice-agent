// @vitest-environment jsdom
/**
 * What: behaviour tests for the console shell: conversation log, StepTrajectory disclosure and
 *   live region, gate words, failed turns, identity chip, input bar.
 * Use when: `pnpm test`.
 * Inputs: the S1, S4, voice and edge fixtures replayed into a fresh turn store.
 * Output: vitest results.
 * Side effects: none (no network).
 * Role: CS-E-04.S5.T1 Done when: the S1 fixture renders chips that expand; S4 shows
 *   "0.41 / 0.45, escalate" as text.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SseEvent } from "@contracts/contracts";
import { loadFixture } from "@/test-support/fixtures";
import { createTurnStore } from "@/lib/turns/store";
import { failTurn } from "@/lib/turns/reducer";

import { ConsoleShell } from "./console-shell";
import { InputBar } from "./input-bar";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

const s1 = loadFixture("s1_chat.jsonl");
const s4 = loadFixture("s4_unsure.jsonl");

afterEach(cleanup);

/**
 * What: a store with the given user lines and fixture events applied, turn by turn.
 * Use when: rendering the console with replayed data.
 * Inputs: events, user lines per turn (null for no bubble).
 * Output: a fresh StoreApi.
 * Side effects: none.
 * Role: test helper (mirrors the dev replay route).
 */
function replayed(events: SseEvent[], lines: (string | null)[] = []) {
  const store = createTurnStore();
  const ids = [...new Set(events.map((e) => e.turn_id))];
  ids.forEach((id, i) => {
    const line = lines[i];
    if (line) store.getState().addUserMessage(line, events.find((e) => e.turn_id === id)?.mode ?? "chat");
    for (const e of events.filter((e) => e.turn_id === id)) store.getState().apply(e);
  });
  return store;
}

describe("console shell with the S1 fixture", () => {
  it("renders user and assistant messages in thread order", () => {
    const store = replayed(s1, ["Hi, we were charged twice.", null, "Yes, go ahead.", "When does it arrive?"]);
    render(<ConsoleShell sessionId="s" store={store} />);
    const log = screen.getByRole("log", { name: "Conversation" });
    const items = within(log).getAllByRole("article");
    expect(items.map((el) => el.getAttribute("aria-label"))).toEqual([
      "You said", "Assistant", "Assistant", "You said", "Assistant", "You said", "Assistant",
    ]);
    expect(within(log).getByText("Hi, we were charged twice.")).toBeTruthy();
    expect(within(log).getByText(/Refunds reach your card in 5 to 10 business days/)).toBeTruthy();
  });

  it("each trail is a collapsed disclosure button that expands into its step rows", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s1)} />);
    const btn = screen.getByRole("button", { name: /6 steps, 4\.2 s · Refund ready to confirm/ });
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("2 charges of $96.00 on 21 Nov")).toBeNull();
    fireEvent.click(btn);
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    const panel = document.getElementById(btn.getAttribute("aria-controls") as string) as HTMLElement;
    expect(within(panel).getByText("Checked billing history")).toBeTruthy();
    expect(within(panel).getByText("2 charges of $96.00 on 21 Nov")).toBeTruthy();
    expect(within(panel).getByText("0.18 s")).toBeTruthy();
    expect(within(panel).getByText("allowed · PRO_WITHIN_LIMITS · refund-policy §2")).toBeTruthy();
    fireEvent.click(btn);
    expect(btn.getAttribute("aria-expanded")).toBe("false");
  });

  it("all four S1 turns settle to a summary, and the first shows its gate in words when opened", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s1)} />);
    const summaries = screen.getAllByRole("button", { name: /steps?, \d+\.\d s · / });
    expect(summaries).toHaveLength(4);
    fireEvent.click(summaries[0] as HTMLElement);
    expect(screen.getByText("0.71 / 0.45, answer")).toBeTruthy();
  });

  it("shows the verified identity chip from verification.result", () => {
    const store = createTurnStore();
    render(<ConsoleShell sessionId="s" store={store} />);
    expect(screen.getByText("Not verified")).toBeTruthy();
    act(() => s1.forEach((e) => store.getState().apply(e)));
    expect(screen.getByText("Verified as Priya Raman")).toBeTruthy();
    expect(screen.getByText("Owner · Pro")).toBeTruthy();
  });

  it("has New conversation, an Inspector placeholder, the theme toggle and a disabled mic", () => {
    const onNew = vi.fn();
    render(<ConsoleShell sessionId="s" store={replayed(s1)} onNewConversation={onNew} />);
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    expect(onNew).toHaveBeenCalledTimes(1);
    expect((screen.getByRole("button", { name: /Inspector/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("button", { name: /theme/ })).toBeTruthy();
    expect((screen.getByRole("button", { name: /Voice/ }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("S4: gate rows in words", () => {
  it('the escalation turn shows "0.41 / 0.45, escalate" as text with a full label', () => {
    render(<ConsoleShell sessionId="s" store={replayed(s4, ["Weather?", "HIPAA?", "We need a BAA."])} />);
    fireEvent.click(screen.getByRole("button", { name: /3 steps, 1\.9 s · Escalated/ }));
    const words = screen.getByText("0.41 / 0.45, escalate");
    expect(words.getAttribute("aria-label")).toBe("Retrieval score 0.41, answer threshold 0.45, floor 0.30: weak, escalate");
    expect(screen.getByRole("button", { name: /2 steps, 1\.7 s · Declined, off topic/ })).toBeTruthy();
  });
});

describe("live line, live region and failed turns", () => {
  it("while running, the summary is the running step's label, announced politely", () => {
    const store = createTurnStore();
    render(<ConsoleShell sessionId="s" store={store} />);
    const c2 = s1.filter((e) => e.turn_id.endsWith("c2"));
    act(() => c2.slice(0, 2).forEach((e) => store.getState().apply(e)));
    const status = screen.getByRole("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.textContent).toBe("Verifying your identity...");
    act(() => c2.slice(2, 9).forEach((e) => store.getState().apply(e)));
    expect(status.textContent).toBe("Checking your billing history...");
    act(() => c2.slice(9).forEach((e) => store.getState().apply(e)));
    expect(status.textContent).toBe("6 steps, 4.2 s · Refund ready to confirm");
  });

  it("a dropped turn renders Couldn't finish and its message, with no live row", () => {
    const store = createTurnStore();
    const c2 = s1.filter((e) => e.turn_id.endsWith("c2"));
    c2.slice(0, 9).forEach((e) => store.getState().apply(e));
    store.setState({ conversation: failTurn(store.getState().conversation, c2[0]?.turn_id as string, "dropped") });
    render(<ConsoleShell sessionId="s" store={store} />);
    const btn = screen.getByRole("button", { name: /4 steps · Couldn't finish/ });
    expect(screen.getByText("The connection dropped before this reply finished.")).toBeTruthy();
    fireEvent.click(btn);
    expect(screen.getByText("stopped")).toBeTruthy();
    expect(screen.queryByText("running")).toBeNull();
  });

  it("a voice turn joins the same thread and is marked as voice", () => {
    const store = replayed(s1.filter((e) => e.turn_id.endsWith("c1")), ["Refund please"]);
    const voice = loadFixture("voice_turn.jsonl").map((e) => ({ ...e, session_id: s1[0]?.session_id as string }));
    store.getState().addUserMessage("When will it arrive?", "voice");
    voice.forEach((e) => store.getState().apply(e));
    render(<ConsoleShell sessionId="s" store={store} />);
    expect(screen.getAllByRole("article")).toHaveLength(4);
    expect(screen.getAllByText("Voice").length).toBeGreaterThanOrEqual(2);
  });
});

describe("input bar", () => {
  it("sends on Enter, keeps Shift+Enter as a newline, and clears after send", () => {
    const onSend = vi.fn();
    render(<InputBar onSend={onSend} busy={false} disabled={false} />);
    const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello");
    expect(box.value).toBe("");
  });

  it("does not send blank text, while busy, or while composing", () => {
    const onSend = vi.fn();
    const { rerender } = render(<InputBar onSend={onSend} busy={false} disabled={false} />);
    const box = screen.getByRole("textbox", { name: "Message" });
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.change(box, { target: { value: "hi" } });
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    rerender(<InputBar onSend={onSend} busy disabled={false} />);
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).getAttribute("aria-disabled")).toBe("true");
  });
});
