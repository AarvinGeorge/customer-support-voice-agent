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
    expect(within(log).getByText(/Refunds reach your card in 5 to 10 business days after/, { selector: "p" })).toBeTruthy();
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

  it("has New conversation, the Inspector toggle, the theme toggle and a mic that starts voice", () => {
    const onNew = vi.fn();
    render(<ConsoleShell sessionId="s" store={replayed(s1)} onNewConversation={onNew} />);
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    expect(onNew).toHaveBeenCalledTimes(1);
    expect((screen.getByRole("button", { name: /Inspector/ }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("button", { name: /theme/ })).toBeTruthy();
    const mic = screen.getByRole("button", { name: "Start voice" }) as HTMLButtonElement;
    expect(mic.disabled).toBe(false);
    expect(mic.getAttribute("aria-pressed")).toBe("false");
  });

  it("the mic is disabled without a session", () => {
    render(<ConsoleShell sessionId={null} store={replayed(s1)} />);
    expect((screen.getByRole("button", { name: "Start voice" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("S4: gate rows in words", () => {
  it('the escalation turn shows "0.41 / 0.45, escalate" as text with a full label', () => {
    render(<ConsoleShell sessionId="s" store={replayed(s4, ["Weather?", "HIPAA?", "We need a BAA."])} />);
    fireEvent.click(screen.getByRole("button", { name: /3 steps, 1\.9 s · Escalated/ }));
    const words = screen.getByText("0.41 / 0.45, escalate");
    expect(words.getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByText("Retrieval score 0.41, answer threshold 0.45, floor 0.30: weak, escalate").className).toContain("sr-only");
    expect(screen.getByRole("button", { name: /2 steps, 1\.7 s · Declined, off topic/ })).toBeTruthy();
  });
});

describe("live line, live region and failed turns", () => {
  it("while running, the summary is the running step's label, announced politely by one region", () => {
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
    expect(status.textContent).toBe("6 steps, 4.2 s · Refund ready to confirm. I can refund $96.00 for the duplicate charge on 21 November (invoice ending 0413). Shall I go ahead?");
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("log").getAttribute("aria-live")).toBe("off");
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

describe("review PR #20 fixes", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("I2: a send that fails before any event shows an error row; Retry sends the text again", async () => {
    const calls: string[] = [];
    let fail = true;
    const c4 = s1.filter((e) => e.turn_id.endsWith("c4"));
    vi.stubGlobal("fetch", vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      calls.push(JSON.parse(init?.body as string).text);
      if (fail) throw new TypeError("Failed to fetch");
      const wire = c4.map((e) => `event: ${e.type}\nid: ${e.seq}\ndata: ${JSON.stringify(e)}\n\n`).join("");
      return new Response(wire, { status: 200, headers: { "content-type": "text/event-stream" } });
    }));
    const store = createTurnStore();
    render(<ConsoleShell sessionId={c4[0]?.session_id as string} store={store} />);
    const box = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(box, { target: { value: "How long until it arrives?" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(await screen.findByText("The connection was lost before this reply finished.")).toBeTruthy();
    expect(screen.getAllByText(/Couldn.t send/).length).toBeGreaterThan(0);
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/Refunds reach your card in 5 to 10 business days/, { selector: "p" })).toBeTruthy();
    expect(calls).toEqual(["How long until it arrives?", "How long until it arrives?"]);
    expect(screen.getAllByText("How long until it arrives?")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("I2: a non-retryable failure has no Retry button", () => {
    const store = createTurnStore();
    store.setState({ thread: [{ kind: "error", id: "e1", text: "hi", userItemId: null, message: "Not allowed.", retryable: false }] });
    render(<ConsoleShell sessionId="s" store={store} />);
    expect(screen.getByText("Not allowed.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("M2: a turn that ends with end_reason error shows the alert icon, not a check", () => {
    const c4 = s1.filter((e) => e.turn_id.endsWith("c4"));
    const last = c4.at(-1) as Extract<SseEvent, { type: "turn.completed" }>;
    const store = replayed([...c4.slice(0, -1), { ...last, end_reason: "error" }]);
    render(<ConsoleShell sessionId="s" store={store} />);
    const btn = screen.getByRole("button", { name: /Couldn't finish/ });
    expect(btn.querySelector("[data-icon='alert']")).not.toBeNull();
  });

  it("M6: a failed turn with no steps shows no trail, only the failure row", () => {
    const store = createTurnStore();
    const c4 = s1.filter((e) => e.turn_id.endsWith("c4"));
    store.getState().apply(c4[0] as SseEvent);
    store.setState({ conversation: failTurn(store.getState().conversation, c4[0]?.turn_id as string, "dropped") });
    render(<ConsoleShell sessionId="s" store={store} />);
    expect(screen.queryByRole("button", { name: /0 steps/ })).toBeNull();
    expect(screen.getByText("The connection dropped before this reply finished.")).toBeTruthy();
  });

  it("M9: aria-controls is set only while the panel exists", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s1)} />);
    const btn = screen.getByRole("button", { name: /6 steps/ });
    expect(btn.getAttribute("aria-controls")).toBeNull();
    fireEvent.click(btn);
    expect(document.getElementById(btn.getAttribute("aria-controls") as string)).not.toBeNull();
  });
});

describe("voice dock in the shell (CS-E-05.S4.T1)", () => {
  /**
   * What: a stand-in voice controller in a given state.
   * Use when: rendering the shell mid-call without a VoiceClient.
   * Inputs: the state.
   * Output: a VoiceController with spy actions.
   * Side effects: none.
   * Role: test helper.
   */
  const controller = (state: "listening" | "ended" | "error") => ({
    voice: { state, label: state, caption: null, error: state === "error" ? "Voice couldn't connect just now." : null, muted: false, endsAt: null, agentLine: null },
    toggle: vi.fn(),
    end: vi.fn(),
    mute: vi.fn(),
    stopSpeaking: vi.fn(),
    levels: () => ({ mic: 0, agent: 0 }),
    available: true,
  });

  it("the dock replaces the input bar while a call is open; Back to typing ends the call", () => {
    const v = controller("listening");
    render(<ConsoleShell sessionId="s" store={replayed(s1)} voice={v} />);
    expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
    expect(screen.getByRole("region", { name: "Voice call" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Back to typing/ }));
    expect(v.end).toHaveBeenCalledTimes(1);
  });

  it("after the call the input bar is back, with the error words when it failed", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s1)} voice={controller("error")} />);
    expect(screen.getByRole("textbox", { name: "Message" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Voice call" })).toBeNull();
    expect(screen.getByText("Voice couldn't connect just now.")).toBeTruthy();
  });
});

describe("voice transcripts in the thread (CS-E-05.S4.T3)", () => {
  const voiceTurn = loadFixture("voice_turn.jsonl");

  it("an interrupted read-back shows only what was heard, the Interrupted chip, and the full answer on request", () => {
    const store = replayed(voiceTurn, ["When will my refund arrive?"]);
    const turnId = voiceTurn[0]?.turn_id as string;
    const full = store.getState().conversation.turns[turnId]?.text ?? "";
    store.getState().upsertAgentVoice({ itemId: "a1", text: "Refunds reach your card in five to ten business days.", heard: "Refunds reach your card", interrupted: true, turnId });
    render(<ConsoleShell sessionId="s" store={store} />);
    const log = screen.getByRole("log", { name: "Conversation" });
    expect(within(log).getByText("Refunds reach your card")).toBeTruthy();
    expect(within(log).getByText("Interrupted")).toBeTruthy();
    expect(within(log).queryByText(full)).toBeNull();
    fireEvent.click(within(log).getByRole("button", { name: "Show full answer" }));
    expect(within(log).getByText(full)).toBeTruthy();
    expect(within(log).getAllByRole("article").map((a) => a.getAttribute("aria-label"))).toEqual(["You said", "Assistant"]);
  });

  it("a read-back heard in full adds nothing (the answer is already the turn's text)", () => {
    const store = replayed(voiceTurn, ["When will my refund arrive?"]);
    const turnId = voiceTurn[0]?.turn_id as string;
    store.getState().upsertAgentVoice({ itemId: "a1", text: "Spoken words.", heard: "Spoken words.", interrupted: false, turnId });
    render(<ConsoleShell sessionId="s" store={store} />);
    expect(screen.queryByText("Spoken words.")).toBeNull();
    expect(screen.queryByText("Interrupted")).toBeNull();
  });

  it("speech outside a turn (a holding phrase) is its own spoken line, marked when interrupted", () => {
    const store = createTurnStore();
    store.getState().upsertAgentVoice({ itemId: "h1", text: "One moment while I check that.", heard: "One moment", interrupted: true, turnId: null });
    render(<ConsoleShell sessionId="s" store={store} />);
    const line = screen.getByRole("article", { name: "Assistant, spoken" });
    expect(within(line).getByText("One moment")).toBeTruthy();
    expect(within(line).getByText("Interrupted")).toBeTruthy();
  });
});
