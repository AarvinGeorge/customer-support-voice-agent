/**
 * What: unit tests for the message furniture rules: answer state, ticket chips, escalation words,
 *   decline notes, citations (safe links), the fallback notice, error rows, the 2,000 character
 *   limit and the input placeholder.
 * Use when: `pnpm test`.
 * Inputs: the S1, S3, S5 and edge fixtures and small hand-built events.
 * Output: vitest results.
 * Side effects: none.
 * Role: CS-E-04.S5.T3 pure layer (doc 07 s3.4 TicketRef, s6.2, s6.4, s16; L1 s8).
 */
import { describe, expect, it } from "vitest";
import type { Citation, EscalatedEvent, SseEvent, TicketRef } from "@contracts/contracts";
import { loadFixture } from "@/test-support/fixtures";

import {
  answerState,
  citationViews,
  declineNotes,
  errorRows,
  escalationWords,
  fallbackNotice,
  inputPlaceholder,
  messageLimit,
  ticketChip,
  turnTickets,
} from "./furniture";
import { failTurn, initialConversationState, replayEvents, type TurnView } from "./reducer";

const s1 = loadFixture("s1_chat.jsonl");
const s3 = loadFixture("s3_decline.jsonl");
const s5 = loadFixture("s5_failures.jsonl");
const edge = loadFixture("edge_turns.jsonl");

const TICKET: TicketRef = {
  ticket_id: "TCK-26118213",
  short_id: "8213",
  display: "ticket ending 8213",
  stage: "open",
  priority: "P3",
  queue: "billing",
  target_label: "invoice ending 0412",
  verified: true,
};

/**
 * What: the TurnView of the nth turn (0-based) after replaying some events.
 * Use when: a test needs one settled or partial turn.
 * Inputs: events, the turn index.
 * Output: the TurnView.
 * Side effects: none.
 * Role: test helper.
 */
function turnOf(events: readonly SseEvent[], n: number): TurnView {
  const state = replayEvents(events);
  const id = state.turnOrder[n];
  const turn = id ? state.turns[id] : undefined;
  if (!turn) throw new Error(`no turn ${n}`);
  return turn;
}

/**
 * What: the events of the nth turn of a fixture, cut before the first event of a given type.
 * Use when: freezing a turn mid-stream (status held, tokens flowing).
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

describe("answerState", () => {
  it("is working before any status or text, then follows the status kind", () => {
    const s1c4 = s1.filter((e) => e.turn_id.endsWith("c4"));
    expect(answerState(turnOf(s1c4.slice(0, 1), 0))).toBe("working");
    expect(answerState(turnOf(until(s1, 3, "gate"), 0))).toBe("working");
    const held = until(s1, 3, "token");
    expect(answerState(turnOf(held, 0))).toBe("checking_policy");
    const streaming = s1c4.filter((e) => e.type !== "turn.completed");
    expect(answerState(turnOf(streaming, 0))).toBe("streaming");
    expect(answerState(turnOf(s1c4, 0))).toBeNull();
  });

  it("names slow and retrying from the status event", () => {
    expect(answerState(turnOf(until(edge, 0, "model.fallback").concat(edge.filter((e) => e.type === "status" && e.kind === "slow")), 0))).toBe("slow");
    expect(answerState(turnOf(until(s5, 1, "action.result"), 0))).toBe("retrying");
  });

  it("is null for a failed turn", () => {
    const state = replayEvents(until(s1, 3, "token"));
    const id = state.turnOrder[0] as string;
    const failed = failTurn(state, id, "dropped").turns[id] as TurnView;
    expect(answerState(failed)).toBeNull();
  });
});

describe("ticketChip", () => {
  it("shows the short form, priority and queue, and keeps the full id for hover and focus", () => {
    expect(ticketChip(TICKET)).toEqual({
      ticketId: "TCK-26118213",
      label: "Ticket ending 8213",
      meta: "P3 · Billing",
      note: "Open · Billing team",
    });
  });

  it("says pending approval in words for an approval ticket", () => {
    const chip = ticketChip({ ...TICKET, stage: "pending_approval", queue: "approvals" });
    expect(chip.meta).toBe("Pending approval · Approvals");
    expect(chip.note).toBe("Pending approval · Approvals team");
  });
});

describe("turnTickets", () => {
  it("collects each ticket of a turn once (action result, escalation, final)", () => {
    expect(turnTickets(turnOf(s5, 1)).map((t) => t.ticket_id)).toEqual(["TCK-26118213"]);
  });

  it("is empty for a turn without tickets", () => {
    expect(turnTickets(turnOf(s1, 3))).toEqual([]);
  });
});

describe("escalationWords", () => {
  const esc = (reason: EscalatedEvent["reason"]): EscalatedEvent => ({
    v: 1, type: "escalated", seq: 1, session_id: "S", turn_id: "T", trace_id: "T", ts: "2026-11-27T13:00:00Z", mode: "chat",
    reason, ticket: null, origin: "code", offer_ticket_button: false,
  });

  it("has words for every escalation reason in doc 07 s6.2", () => {
    const reasons: EscalatedEvent["reason"][] = [
      "not_in_docs", "grounding_failed", "retrieval_unavailable", "tool_failure", "limit_reached", "deadline",
      "verify_locked", "needs_commitment", "customer_asked_human", "model_unavailable",
    ];
    const words = reasons.map((r) => escalationWords(esc(r)));
    for (const w of words) expect(w.length).toBeGreaterThan(10);
    expect(new Set(words).size).toBe(reasons.length);
    expect(escalationWords(esc("tool_failure"))).toBe("Handed to our team: a system we rely on didn't respond");
  });
});

describe("declineNotes", () => {
  it("quotes core's decline text with the reason code and policy ref (S3 member)", () => {
    expect(declineNotes(turnOf(s3, 1))).toEqual([
      {
        key: expect.any(String),
        text: "Only an account owner or billing admin can make billing changes.",
        codes: "ROLE_NOT_PERMITTED",
        refs: ["Billing permissions · roles-and-permissions#billing-permissions"],
        unchanged: true,
      },
    ]);
  });

  it("is empty when nothing was declined", () => {
    expect(declineNotes(turnOf(s1, 2))).toEqual([]);
  });

  it("leaves a decline that answers an open card to the card's receipt", () => {
    const events = s1.map((e) => (e.type === "action.result" ? { ...e, outcome: "declined" as const } : e));
    expect(declineNotes(turnOf(events, 2))).toEqual([]);
  });
});

describe("citationViews", () => {
  const base: Citation = {
    chunk_id: "a#1", label: "Refund policy, Refunds on the Pro plan", ref: "a#1", spoken: "x",
    url: "https://app.notion.com/p/abc#refunds", section_title: "Refunds on the Pro plan",
    excerpt: "Pro customers can get a refund.", score: 0.712,
  };

  it("builds chip, hover card and a safe https link", () => {
    expect(citationViews([base])).toEqual([
      {
        key: "a#1",
        chip: "Refunds on the Pro plan",
        heading: "Refund policy, Refunds on the Pro plan",
        excerpt: "Pro customers can get a refund.",
        href: "https://app.notion.com/p/abc#refunds",
        urlText: "app.notion.com/p/abc#refunds",
        score: "Retrieval score 0.71",
      },
    ]);
  });

  it("never links a non-http url and shows each chunk once", () => {
    const bad = { ...base, chunk_id: "b#1", url: "javascript:alert(1)" };
    const views = citationViews([base, base, bad]);
    expect(views.map((v) => v.key)).toEqual(["a#1", "b#1"]);
    expect(views[1]?.href).toBeNull();
  });

  it("reads the S1 fixture's citation", () => {
    const turn = turnOf(s1, 3);
    expect(citationViews(turn.completed?.final.citations ?? []).map((v) => v.chip)).toEqual(["Refunds on the Pro plan"]);
  });
});

describe("fallbackNotice", () => {
  it("explains the fallback model on the S5 turn", () => {
    expect(fallbackNotice(turnOf(s5, 0))).toEqual({
      label: "Fallback model",
      title: "Primary model rate limited (429)",
      body: "Answered by the fallback model gpt-4.1-mini. Policy checks are unchanged.",
    });
  });

  it("is null when the primary model answered", () => {
    expect(fallbackNotice(turnOf(s1, 3))).toBeNull();
  });
});

describe("errorRows", () => {
  it("shows the server error with its retryable flag (edge deadline turn)", () => {
    expect(errorRows(turnOf(edge, 2))).toEqual([{ key: expect.any(String), message: "I couldn't finish that in time.", retryable: true }]);
  });

  it("shows the client failure of a turn that never completed", () => {
    const state = replayEvents(until(s1, 3, "token"));
    const id = state.turnOrder[0] as string;
    const failed = failTurn(state, id, "dropped").turns[id] as TurnView;
    expect(errorRows(failed)).toEqual([{ key: "failure", message: "The connection dropped before this reply finished.", retryable: true }]);
  });

  it("is empty for a clean turn", () => {
    expect(errorRows(turnOf(s1, 3))).toEqual([]);
  });
});

describe("messageLimit", () => {
  it("stays hidden well under 2,000 characters", () => {
    expect(messageLimit("hi")).toEqual({ count: 2, max: 2000, show: false, over: false, text: "2 / 2,000" });
  });

  it("shows the counter near the limit and flags going over", () => {
    expect(messageLimit("a".repeat(1800))).toMatchObject({ show: true, over: false, text: "1,800 / 2,000" });
    expect(messageLimit("a".repeat(2001))).toMatchObject({ show: true, over: true, text: "2,001 / 2,000" });
  });

  it("counts characters, not UTF-16 units", () => {
    expect(messageLimit("😀😀").count).toBe(2);
  });
});

describe("inputPlaceholder", () => {
  it("asks for a yes while a confirmation is pending", () => {
    const state = replayEvents(s1.filter((e) => e.turn_id.endsWith("c1") || e.turn_id.endsWith("c2")));
    expect(inputPlaceholder(state, false)).toBe("Reply yes to confirm, or no to cancel");
  });

  it("uses the default and the no-session text otherwise", () => {
    expect(inputPlaceholder(initialConversationState, false)).toBe("Ask about billing, your account or a how-to");
    expect(inputPlaceholder(initialConversationState, true)).toBe("Start from the access page to open a session");
  });
});
