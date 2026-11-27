// @vitest-environment jsdom
/**
 * What: tests for the voice dock (state word and icon, bars, caption, Mute, Stop speaking, End)
 *   and the two-row bar visualizer with mocked level sources.
 * Use when: `pnpm test`.
 * Inputs: none.
 * Output: Vitest results.
 * Side effects: none (a manual frame scheduler; no audio).
 * Role: CS-E-05.S4.T1 Done when: bars move separately for user and agent; each voice state shows
 *   its word and icon; reduced motion gives a static level meter.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { VOICE_LABELS, VOICE_STATES, type VoiceState } from "@/lib/voice/machine";
import type { VoiceSnapshot } from "@/lib/voice/client";

import { VOICE_ICON_NAMES, VoiceBars, VoiceDock, type VoiceLevels } from "./voice-dock";

afterEach(cleanup);

/**
 * What: a frame scheduler the test steps by hand.
 * Use when: VoiceBars animates.
 * Inputs: none.
 * Output: { schedule, cancel, step(n) }.
 * Side effects: none.
 * Role: requestAnimationFrame stand-in.
 */
function frames() {
  let queue: Array<() => void> = [];
  return {
    schedule: (cb: () => void) => {
      queue.push(cb);
      return queue.length;
    },
    cancel: () => {
      queue = [];
    },
    step(n: number) {
      for (let i = 0; i < n; i += 1) {
        const q = queue;
        queue = [];
        for (const cb of q) cb();
      }
    },
  };
}

/** What: bar heights in px of one row. Use when: asserting motion. Inputs: row name. Output: numbers. Side effects: none. Role: helper. */
const heights = (row: string) =>
  [...screen.getByTestId(`bars-${row}`).querySelectorAll<HTMLElement>("[data-bar]")].map((b) => Number.parseFloat(b.style.height));

const snap = (state: VoiceState, over: Partial<VoiceSnapshot> = {}): VoiceSnapshot => ({
  state,
  label: VOICE_LABELS[state],
  caption: null,
  error: null,
  muted: false,
  endsAt: null,
  agentLine: null,
  ...over,
});

describe("VoiceBars", () => {
  it("moves the You row with the mic and the Agent row with agent audio, separately", () => {
    const f = frames();
    const lv = { mic: 0.8, agent: 0 };
    render(<VoiceBars levels={() => lv} reducedMotion={false} schedule={f.schedule} cancel={f.cancel} />);
    act(() => f.step(10));
    expect(Math.max(...heights("you"))).toBeGreaterThan(8);
    expect(Math.max(...heights("agent"))).toBe(2);
    lv.mic = 0;
    lv.agent = 0.9;
    act(() => f.step(130));
    expect(Math.max(...heights("you"))).toBe(2);
    expect(Math.max(...heights("agent"))).toBeGreaterThan(8);
  });

  it("under reduced motion shows one static level meter per row (no moving history)", () => {
    vi.useFakeTimers();
    const lv = { mic: 0.5, agent: 0.25 };
    render(<VoiceBars levels={() => lv} reducedMotion />);
    expect(screen.queryAllByTestId(/^bars-/)).toHaveLength(0);
    act(() => vi.advanceTimersByTime(100));
    const you = screen.getByRole("meter", { name: "You" });
    const agent = screen.getByRole("meter", { name: "Agent" });
    expect(you.getAttribute("aria-valuenow")).toBe("50");
    expect(agent.getAttribute("aria-valuenow")).toBe("25");
    vi.useRealTimers();
  });
});

describe("VoiceDock", () => {
  const noLevels: VoiceLevels = () => ({ mic: 0, agent: 0 });

  it.each(VOICE_STATES.filter((s) => s !== "idle"))("state %s shows its word and its icon", (state) => {
    render(<VoiceDock voice={snap(state)} levels={noLevels} onMute={() => {}} onStopSpeaking={() => {}} onEnd={() => {}} />);
    const label = screen.getByTestId("voice-state");
    expect(label.textContent).toContain(VOICE_LABELS[state]);
    expect(label.querySelector("svg")?.getAttribute("data-icon")).toBe(VOICE_ICON_NAMES[state]);
  });

  it("every state has a distinct icon name", () => {
    const names = VOICE_STATES.map((s) => VOICE_ICON_NAMES[s]);
    expect(new Set(names).size).toBe(names.length);
  });

  it("Mute, Stop speaking and Back to typing call their handlers; Mute shows its pressed state", () => {
    const onMute = vi.fn();
    const onStop = vi.fn();
    const onEnd = vi.fn();
    const { rerender } = render(<VoiceDock voice={snap("speaking")} levels={noLevels} onMute={onMute} onStopSpeaking={onStop} onEnd={onEnd} />);
    fireEvent.click(screen.getByRole("button", { name: "Mute" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop speaking" }));
    fireEvent.click(screen.getByRole("button", { name: /Back to typing/ }));
    expect(onMute).toHaveBeenCalledTimes(1);
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(onEnd).toHaveBeenCalledTimes(1);
    rerender(<VoiceDock voice={snap("speaking", { muted: true })} levels={noLevels} onMute={onMute} onStopSpeaking={onStop} onEnd={onEnd} />);
    expect(screen.getByRole("button", { name: "Unmute" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("Stop speaking is enabled only while the agent is speaking", () => {
    const { rerender } = render(<VoiceDock voice={snap("listening")} levels={noLevels} onMute={() => {}} onStopSpeaking={() => {}} onEnd={() => {}} />);
    expect((screen.getByRole("button", { name: "Stop speaking" }) as HTMLButtonElement).disabled).toBe(true);
    rerender(<VoiceDock voice={snap("speaking")} levels={noLevels} onMute={() => {}} onStopSpeaking={() => {}} onEnd={() => {}} />);
    expect((screen.getByRole("button", { name: "Stop speaking" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("shows the live caption of what the customer is saying, and the time left", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-27T12:00:00Z"));
    render(
      <VoiceDock
        voice={snap("listening", { caption: { itemId: "u1", text: "Actually, what about the invoice", final: false }, endsAt: Date.now() + 468_000 })}
        levels={noLevels}
        onMute={() => {}}
        onStopSpeaking={() => {}}
        onEnd={() => {}}
      />,
    );
    expect(screen.getByText("You, live")).toBeTruthy();
    expect(screen.getByText("Actually, what about the invoice")).toBeTruthy();
    expect(screen.getByTestId("voice-state").textContent).toContain("7:48 left");
    vi.useRealTimers();
  });

  it("while speaking, the caption box shows what the agent is saying", () => {
    render(<VoiceDock voice={snap("speaking")} agentLine="The invoice ending 0412 is for 96 dollars." levels={noLevels} onMute={() => {}} onStopSpeaking={() => {}} onEnd={() => {}} />);
    expect(screen.getAllByText("Agent")).toHaveLength(2);
    expect(screen.getByText("The invoice ending 0412 is for 96 dollars.")).toBeTruthy();
  });
});
