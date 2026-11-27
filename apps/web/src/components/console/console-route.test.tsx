// @vitest-environment jsdom
/**
 * What: the New conversation race through the real /console client entry (review PR #20, I1).
 * Use when: `pnpm test`.
 * Inputs: a fake agent (POST /api/sessions, a hand-fed SSE stream for the old session, a normal
 *   stream for the new one) and mocked Next.js navigation.
 * Output: vitest results.
 * Side effects: stubs global fetch; resets the app turn store.
 * Role: proves an old session's late events never reach the new conversation and new replies render.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { loadFixture, toWire } from "@/test-support/fixtures";
import { turnStore } from "@/lib/turns/store";

import { ConsoleRoute } from "./console-route";

const nav = vi.hoisted(() => ({ session: "", replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: nav.replace }),
  useSearchParams: () => new URLSearchParams(`session=${nav.session}`),
}));

const s1 = loadFixture("s1_chat.jsonl");
const OLD = s1[0]?.session_id as string;
const NEW = "019a7c2e-9999-7000-8000-00000000a001";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  turnStore.getState().reset();
});

describe("New conversation while a reply is streaming", () => {
  it("aborts the old stream, keeps its late events out, and renders the new session's reply", async () => {
    nav.session = OLD;
    const enc = new TextEncoder();
    let feed: ReadableStreamDefaultController<Uint8Array> | null = null;
    let oldSignal: AbortSignal | null = null;
    const c2 = s1.filter((e) => e.turn_id.endsWith("c2"));
    const c4new = s1.filter((e) => e.turn_id.endsWith("c4")).map((e) => ({ ...e, session_id: NEW }));
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
        const path = String(url);
        if (path === "/api/sessions") {
          return new Response(JSON.stringify({ session_id: NEW, created_at: "2026-11-27T12:00:00Z" }), { status: 201, headers: { "content-type": "application/json" } });
        }
        if (path.includes(OLD)) {
          oldSignal = init?.signal ?? null;
          const body = new ReadableStream<Uint8Array>({
            start(c) {
              feed = c;
              oldSignal?.addEventListener("abort", () => c.error(new DOMException("aborted", "AbortError")));
            },
          });
          return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
        }
        return new Response(toWire(c4new), { status: 200, headers: { "content-type": "text/event-stream" } });
      }),
    );
    const { rerender } = render(<ConsoleRoute />);
    const box = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(box, { target: { value: "Refund the duplicate, please" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await vi.waitFor(() => expect(feed).not.toBeNull());
    await act(async () => feed?.enqueue(enc.encode(toWire(c2.slice(0, 2)))));
    expect(await screen.findByText("Verifying your identity...", { selector: "span" })).toBeTruthy();

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "New conversation" })));
    await vi.waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`/console?session=${NEW}`));
    nav.session = NEW;
    rerender(<ConsoleRoute />);
    expect((oldSignal as AbortSignal | null)?.aborted).toBe(true);

    await act(async () => {
      try {
        feed?.enqueue(enc.encode(toWire(c2.slice(2))));
      } catch {
        // aborted stream: the late chunk is refused, which is the point
      }
    });
    expect(screen.queryByText(/I can refund \$96\.00/)).toBeNull();
    expect(turnStore.getState().conversation.sessionId).toBeNull();
    expect(screen.getByText("Not verified")).toBeTruthy();

    const box2 = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(box2, { target: { value: "How long until it reaches my card?" } });
    fireEvent.keyDown(box2, { key: "Enter" });
    expect(await screen.findByText(/Refunds reach your card in 5 to 10 business days/, { selector: "p" })).toBeTruthy();
    expect(turnStore.getState().conversation.sessionId).toBe(NEW);
    expect(screen.queryByText("Refund the duplicate, please")).toBeNull();
  });
});
