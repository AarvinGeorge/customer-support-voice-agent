// @vitest-environment jsdom
/**
 * What: behaviour tests for the observability drawer inside the console shell: opening from the
 *   Inspector button, the metric strip, per-turn selection, Gate 1 words, LangSmith link, the
 *   Demo controls slot, focus management and Escape.
 * Use when: `pnpm test`.
 * Inputs: the S1 and S4 fixtures replayed into a fresh turn store.
 * Output: vitest results.
 * Side effects: none (no network).
 * Role: CS-E-06.S3.T1 Done when (component level).
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SseEvent } from "@contracts/contracts";

import { loadFixture } from "@/test-support/fixtures";
import { createTurnStore } from "@/lib/turns/store";

import { ConsoleShell } from "./console-shell";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

const s1 = loadFixture("s1_chat.jsonl");
const s4 = loadFixture("s4_unsure.jsonl");

afterEach(cleanup);

/**
 * What: a store with the fixture events applied (no user lines).
 * Use when: rendering the console with replayed data.
 * Inputs: events.
 * Output: a fresh StoreApi.
 * Side effects: none.
 * Role: test helper.
 */
function replayed(events: SseEvent[]) {
  const store = createTurnStore();
  for (const e of events) store.getState().apply(e);
  return store;
}

/**
 * What: renders the shell and opens the drawer with the Inspector button.
 * Use when: most tests.
 * Inputs: events and extra shell props.
 * Output: { drawer, trigger }.
 * Side effects: renders into the jsdom document.
 * Role: test helper.
 */
function openDrawer(events: SseEvent[], props: Partial<Parameters<typeof ConsoleShell>[0]> = {}) {
  render(<ConsoleShell sessionId="s" store={replayed(events)} {...props} />);
  const trigger = screen.getByRole("button", { name: /inspector/i });
  fireEvent.click(trigger);
  return { drawer: screen.getByRole("complementary", { name: "Inspector" }), trigger };
}

describe("Inspector drawer", () => {
  it("is closed until the Inspector button opens it, and the button reports its state", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s1)} />);
    expect(screen.queryByRole("complementary", { name: "Inspector" })).toBeNull();
    const trigger = screen.getByRole("button", { name: /inspector/i }) as HTMLButtonElement;
    expect(trigger.disabled).toBe(false);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("complementary", { name: "Inspector" })).toBeTruthy();
  });

  it("moves focus into the drawer on open; Escape closes it and returns focus to the button", () => {
    const { drawer, trigger } = openDrawer(s1);
    expect(drawer.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    expect(screen.queryByRole("complementary", { name: "Inspector" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("the close button closes it too", () => {
    const { trigger } = openDrawer(s1);
    fireEvent.click(screen.getByRole("button", { name: "Close inspector" }));
    expect(screen.queryByRole("complementary", { name: "Inspector" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("S1: shows the latest turn by default and fills every strip value from usage", () => {
    const { drawer } = openDrawer(s1);
    expect(within(drawer).getByText("Turn 4 of 4")).toBeTruthy();
    const strip = within(drawer).getByRole("list", { name: "Turn metrics" });
    const items = within(strip).getAllByRole("listitem");
    expect(items).toHaveLength(9);
    expect(within(strip).getByText("End-to-end")).toBeTruthy();
    for (const item of items) expect(item.textContent?.length).toBeGreaterThan(0);
  });

  it("previous and next buttons move between turns, and Inspect on a reply selects it", () => {
    const { drawer } = openDrawer(s1);
    fireEvent.click(within(drawer).getByRole("button", { name: "Previous turn" }));
    expect(within(drawer).getByText("Turn 3 of 4")).toBeTruthy();
    expect(within(drawer).getByText(/refund committed, changed/)).toBeTruthy();
    fireEvent.click(within(drawer).getByRole("button", { name: "Next turn" }));
    expect(within(drawer).getByText("Turn 4 of 4")).toBeTruthy();
    expect((within(drawer).getByRole("button", { name: "Next turn" }) as HTMLButtonElement).disabled).toBe(true);
    const inspect = screen.getAllByRole("button", { name: /Inspect turn 1/ });
    fireEvent.click(inspect[0] as HTMLElement);
    expect(within(drawer).getByText("Turn 1 of 4")).toBeTruthy();
    const strip = within(drawer).getByRole("list", { name: "Turn metrics" });
    expect(within(strip).getByText("3.90 s", { selector: "[data-metric=latency] *" })).toBeTruthy();
    expect(within(strip).getByText("$0.0180")).toBeTruthy();
    expect(within(strip).getByText("2,100 cached")).toBeTruthy();
    expect(within(strip).getByText("agent_a")).toBeTruthy();
  });

  it("Inspect on a reply opens the drawer when it is closed", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s1)} />);
    fireEvent.click(screen.getAllByRole("button", { name: /Inspect turn 2/ })[0] as HTMLElement);
    const drawer = screen.getByRole("complementary", { name: "Inspector" });
    expect(within(drawer).getByText("Turn 2 of 4")).toBeTruthy();
    expect(within(drawer).getByText("PRO_WITHIN_LIMITS", { selector: "[data-rule] *" })).toBeTruthy();
  });

  it("S4: the escalation turn shows the score against the threshold in words and numbers, with a provisional badge", () => {
    const { drawer } = openDrawer(s4);
    expect(within(drawer).getByText("Score 0.41 is between tau_low 0.30 and tau_high 0.45: weak match, so the agent escalated.")).toBeTruthy();
    expect(within(drawer).getByText("0.41 vs tau_low 0.30 · tau_high 0.45")).toBeTruthy();
    expect(within(drawer).getByText("provisional")).toBeTruthy();
    expect(within(drawer).getByText("not in the help articles · ticket ending 8214 · opened by code")).toBeTruthy();
  });

  it("links the turn to LangSmith when the project URL is configured, and shows the trace id", () => {
    const { drawer } = openDrawer(s1, { langsmithProjectUrl: "https://smith.langchain.com/o/t/projects/p/p1" });
    const turnId = s1.at(-1)?.turn_id ?? "";
    const link = within(drawer).getByRole("link", { name: /Open in LangSmith/ });
    expect(link.getAttribute("href")).toBe(`https://smith.langchain.com/o/t/projects/p/p1/r/${turnId}?poll=true`);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(within(drawer).getByText(`trace ${turnId.slice(-8)}`)).toBeTruthy();
  });

  it("says the LangSmith link is not configured without a project URL", () => {
    const { drawer } = openDrawer(s1, { langsmithProjectUrl: "" });
    expect(within(drawer).queryByRole("link", { name: /Open in LangSmith/ })).toBeNull();
    expect(within(drawer).getByText(/LangSmith link not configured/)).toBeTruthy();
  });

  it("renders the Demo controls slot when given", () => {
    const { drawer } = openDrawer(s1, { demoControls: <p>fault toggle here</p> });
    const slot = within(drawer).getByRole("region", { name: "Demo controls" });
    expect(within(slot).getByText("fault toggle here")).toBeTruthy();
  });

  it("says so when there are no turns yet", () => {
    render(<ConsoleShell sessionId="s" store={createTurnStore()} />);
    fireEvent.click(screen.getByRole("button", { name: /inspector/i }));
    const drawer = screen.getByRole("complementary", { name: "Inspector" });
    expect(within(drawer).getByText(/No turns yet/)).toBeTruthy();
  });

  it("shows a running turn without a strip until turn.completed arrives", () => {
    const store = createTurnStore();
    render(<ConsoleShell sessionId="s" store={store} />);
    fireEvent.click(screen.getByRole("button", { name: /inspector/i }));
    act(() => {
      for (const e of s1.slice(0, 3)) store.getState().apply(e);
    });
    const drawer = screen.getByRole("complementary", { name: "Inspector" });
    expect(within(drawer).getByText(/Numbers arrive when the turn finishes/)).toBeTruthy();
    expect(within(drawer).getByText("Thought it through")).toBeTruthy();
  });

  it("a turn picked with Inspect is forgotten when the session changes (back to the latest turn)", () => {
    const store = replayed(s1);
    const view = render(<ConsoleShell sessionId="s" store={store} />);
    fireEvent.click(screen.getAllByRole("button", { name: /Inspect turn 1/ })[0] as HTMLElement);
    const drawer = screen.getByRole("complementary", { name: "Inspector" });
    expect(within(drawer).getByText("Turn 1 of 4")).toBeTruthy();
    view.rerender(<ConsoleShell sessionId="s" store={store} />);
    expect(within(drawer).getByText("Turn 1 of 4")).toBeTruthy();
    view.rerender(<ConsoleShell sessionId="s2" store={store} />);
    expect(within(screen.getByRole("complementary", { name: "Inspector" })).getByText("Turn 4 of 4")).toBeTruthy();
  });
});
