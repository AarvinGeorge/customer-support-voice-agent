/**
 * What: unit tests for the card view rules: verification result words, receipt per action
 *   outcome, the confirmation card's state (open, resolved, superseded, expired), the
 *   verification card's host and state, the request bodies and the formatters.
 * Use when: `pnpm test`.
 * Inputs: the S1 fixture and small hand-built events.
 * Output: vitest results.
 * Side effects: none.
 * Role: CS-E-04.S6.T1 Done when (one test per verification result and per outcome), pure layer.
 */
import { describe, expect, it } from "vitest";
import type { ActionResultEvent, ConfirmationRequiredEvent, SseEvent, VerificationResultEvent } from "@contracts/contracts";
import { loadFixture } from "@/test-support/fixtures";

import {
  cardIndex,
  confirmBody,
  confirmationCardState,
  formatAmount,
  formatChargeDate,
  formatCountdown,
  receiptFor,
  ruleTitle,
  verificationBody,
  verificationCardState,
  verificationNotice,
} from "./cards";
import { replayEvents } from "./reducer";

const s1 = loadFixture("s1_chat.jsonl");
const c2 = s1.filter((e) => e.turn_id.endsWith("c2"));
const PENDING = (c2.find((e) => e.type === "confirmation.required") as ConfirmationRequiredEvent).pending;

const ENV = { v: 1 as const, session_id: "S", trace_id: "T", ts: "2026-11-27T13:00:00Z", mode: "chat" as const };
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

/**
 * What: a verification.result event with the given fields.
 * Use when: testing verificationNotice.
 * Inputs: partial fields.
 * Output: the event.
 * Side effects: none.
 * Role: test builder.
 */
function vr(fields: Partial<VerificationResultEvent>): VerificationResultEvent {
  return { ...ENV, turn_id: "t", seq: 1, type: "verification.result", status: "failed", ...fields };
}

/**
 * What: an action.result event with the given fields.
 * Use when: testing receiptFor.
 * Inputs: partial fields.
 * Output: the event.
 * Side effects: none.
 * Role: test builder.
 */
function ar(fields: Partial<ActionResultEvent>): ActionResultEvent {
  return {
    ...ENV,
    turn_id: "t",
    seq: 1,
    type: "action.result",
    pending_action_id: PENDING.pending_action_id,
    action_type: "refund",
    outcome: "committed",
    changed: true,
    reason_codes: [],
    policy_refs: [],
    result_text: "",
    ticket: null,
    ...fields,
  };
}

describe("verificationNotice: one test per verification result (doc 07 s6.2, s6.3)", () => {
  it("verified names the person, role and plan", () => {
    const n = verificationNotice(
      vr({ status: "verified", person: { display_name: "Priya Shah", role: "billing_admin", tier: "pro", contract_status: "active", account_id: "QS-10423" } }),
    );
    expect(n).toEqual({ status: "verified", tone: "success", text: "Verified as Priya Shah, billing admin, Pro plan" });
  });

  it("failed gives the same words for any mismatch and the attempts left", () => {
    expect(verificationNotice(vr({ status: "failed", attempts_left: 2 })).text).toBe("That account ID and PIN didn't match. 2 attempts left.");
    expect(verificationNotice(vr({ status: "failed", attempts_left: 1 })).text).toBe("That account ID and PIN didn't match. 1 attempt left.");
    expect(verificationNotice(vr({ status: "failed" })).tone).toBe("error");
  });

  it("locked says verification is locked and names core's ticket", () => {
    const n = verificationNotice(vr({ status: "locked", ticket: TICKET }));
    expect(n.tone).toBe("error");
    expect(n.text).toBe("For your security, verification is locked for this conversation. Our team will follow up on ticket ending 8213.");
    expect(verificationNotice(vr({ status: "locked" })).text).toBe(
      "For your security, verification is locked for this conversation. Our team will follow up.",
    );
  });

  it("conflict uses the doc 07 s6.3 sentence", () => {
    expect(verificationNotice(vr({ status: "conflict" })).text).toBe(
      "This conversation is already verified as someone else. Please start a new conversation to verify as another person.",
    );
  });

  it("unavailable says nothing was checked and to try again", () => {
    const n = verificationNotice(vr({ status: "unavailable" }));
    expect(n.tone).toBe("warning");
    expect(n.text).toBe("We couldn't reach our account system, so you aren't verified yet. Please try again in a moment.");
  });
});

describe("receiptFor: one test per action.result outcome (doc 07 s6.2, s7.1, s7.3)", () => {
  it("committed: the action done, with amount and invoice", () => {
    expect(receiptFor(ar({ outcome: "committed" }), PENDING)).toEqual({
      outcome: "committed",
      tone: "success",
      title: "Refund issued",
      detail: "$96.00 · invoice ending 0413",
    });
  });

  it("submitted: sent for approval with the ticket", () => {
    expect(receiptFor(ar({ outcome: "submitted", ticket: { ...TICKET, stage: "pending_approval" } }), PENDING)).toMatchObject({
      tone: "warning",
      title: "Sent for approval",
      detail: "ticket ending 8213",
    });
  });

  it("cancelled: nothing changed only because core said changed false", () => {
    expect(receiptFor(ar({ outcome: "cancelled", changed: false }), PENDING)).toMatchObject({
      tone: "muted",
      title: "Cancelled",
      detail: "nothing was changed",
    });
  });

  it("declined: the reason code and the policy", () => {
    const r = receiptFor(
      ar({
        outcome: "declined",
        changed: false,
        reason_codes: ["ROLE_NOT_PERMITTED"],
        policy_refs: [{ article: "roles", section: "3", title: "Roles and permissions" }],
      }),
      null,
    );
    expect(r).toMatchObject({ tone: "error", title: "Declined", detail: "ROLE_NOT_PERMITTED · Roles and permissions §3 · nothing was changed" });
  });

  it("rules_changed: not done, the rules changed", () => {
    const r = receiptFor(ar({ outcome: "rules_changed", changed: false, reason_codes: ["PAST_DUE"] }), PENDING);
    expect(r).toMatchObject({ tone: "warning", title: "Not done, the rules changed", detail: "PAST_DUE · nothing was changed" });
  });

  it("expired: the request is no longer active", () => {
    expect(receiptFor(ar({ outcome: "expired", changed: false }), PENDING)).toMatchObject({
      tone: "muted",
      title: "Request no longer active",
      detail: "nothing was changed",
    });
  });

  it("failed: not issued, nothing changed, and the ticket", () => {
    expect(receiptFor(ar({ outcome: "failed", changed: false, ticket: TICKET }), PENDING)).toMatchObject({
      tone: "error",
      title: "Refund was not issued",
      detail: "nothing was changed · ticket ending 8213",
    });
  });

  it("unconfirmed: never claims nothing changed (changed is null)", () => {
    const r = receiptFor(ar({ outcome: "unconfirmed", changed: null, ticket: TICKET }), PENDING);
    expect(r).toMatchObject({ tone: "error", title: "Outcome unconfirmed", detail: "ticket ending 8213" });
    expect(r.detail).not.toContain("nothing");
  });

  it("step_up_failed and step_up_locked (step-up is out of the slice; the words still exist)", () => {
    expect(receiptFor(ar({ outcome: "step_up_failed", changed: false }), PENDING)).toMatchObject({ tone: "error", title: "PIN didn't match" });
    expect(receiptFor(ar({ outcome: "step_up_locked", changed: false }), PENDING)).toMatchObject({
      tone: "error",
      title: "Request cancelled after 3 PIN tries",
    });
  });

  it("act_alone_applied: the change was applied", () => {
    expect(receiptFor(ar({ outcome: "act_alone_applied", action_type: "notification_prefs", changed: true }), null)).toMatchObject({
      tone: "success",
      title: "Change applied",
      detail: null,
    });
  });

  it("names other actions by their type", () => {
    expect(receiptFor(ar({ outcome: "committed", action_type: "billing_email" }), null).title).toBe("Billing email changed");
    expect(receiptFor(ar({ outcome: "failed", action_type: "grace_extension", changed: false }), null).title).toBe(
      "Grace period was not extended",
    );
  });
});

describe("cardIndex and confirmationCardState", () => {
  it("the S1 card is open after c2, and resolved by the committed result in c3", () => {
    const afterC2 = replayEvents(s1.filter((e) => e.turn_id.endsWith("c1") || e.turn_id.endsWith("c2")));
    const turnId = c2[0]?.turn_id as string;
    expect(confirmationCardState(PENDING, turnId, cardIndex(afterC2), 0, null)).toEqual({ kind: "open" });
    const all = replayEvents(s1);
    const state = confirmationCardState(PENDING, turnId, cardIndex(all), 0, null);
    expect(state.kind).toBe("resolved");
    expect(state.kind === "resolved" && state.result.outcome).toBe("committed");
  });

  it("a later card for another pending action supersedes this one", () => {
    const base = s1.filter((e) => e.turn_id.endsWith("c1") || e.turn_id.endsWith("c2"));
    const newer: SseEvent = {
      ...ENV,
      session_id: c2[0]?.session_id as string,
      turn_id: "turn-new",
      seq: 1,
      type: "confirmation.required",
      pending: { ...PENDING, pending_action_id: "PA-00000002" },
      confirm_label: "Confirm refund",
    };
    const idx = cardIndex(replayEvents([...base, newer]));
    expect(confirmationCardState(PENDING, c2[0]?.turn_id as string, idx, 0, null)).toEqual({ kind: "superseded" });
    expect(confirmationCardState(newer.pending, "turn-new", idx, 0, null)).toEqual({ kind: "open" });
  });

  it("the same pending shown again later (code read-back repeat): only the newest card is live", () => {
    const base = s1.filter((e) => e.turn_id.endsWith("c1") || e.turn_id.endsWith("c2"));
    const again: SseEvent = { ...(c2.find((e) => e.type === "confirmation.required") as SseEvent), turn_id: "turn-again", seq: 1 };
    const idx = cardIndex(replayEvents([...base, again]));
    expect(confirmationCardState(PENDING, c2[0]?.turn_id as string, idx, 0, null).kind).toBe("superseded");
    expect(confirmationCardState(PENDING, "turn-again", idx, 0, null).kind).toBe("open");
  });

  it("past its deadline the card is expired", () => {
    const idx = cardIndex(replayEvents(c2));
    expect(confirmationCardState(PENDING, c2[0]?.turn_id as string, idx, 1000, 999).kind).toBe("expired");
    expect(confirmationCardState(PENDING, c2[0]?.turn_id as string, idx, 1000, 1001).kind).toBe("open");
  });
});

describe("verificationCardState", () => {
  it("the c1 card is a form while verification is awaited, hidden once verified", () => {
    const c1 = s1.filter((e) => e.turn_id.endsWith("c1"));
    const t1 = c1[0]?.turn_id as string;
    const st1 = replayEvents(c1);
    expect(cardIndex(st1).verificationHost).toBe(t1);
    expect(verificationCardState(st1, t1, cardIndex(st1))).toBe("form");
    const st2 = replayEvents([...c1, ...c2]);
    expect(verificationCardState(st2, t1, cardIndex(st2))).toBe("hidden");
  });

  it("only the newest verification.required hosts the card; a lock shows the locked state", () => {
    const c1 = s1.filter((e) => e.turn_id.endsWith("c1"));
    const t1 = c1[0]?.turn_id as string;
    const sid = c1[0]?.session_id as string;
    const retry: SseEvent[] = [
      { ...ENV, session_id: sid, turn_id: "t2", seq: 1, type: "verification.result", status: "failed", attempts_left: 2 },
      {
        ...ENV,
        session_id: sid,
        turn_id: "t2",
        seq: 2,
        type: "verification.required",
        reason: "retry",
        prompt_text: "x",
        attempts_left: 2,
        fields: ["account_id", "pin"],
      },
    ];
    const st = replayEvents([...c1, ...retry]);
    const idx = cardIndex(st);
    expect(idx.verificationHost).toBe("t2");
    expect(verificationCardState(st, t1, idx)).toBe("hidden");
    expect(verificationCardState(st, "t2", idx)).toBe("form");
    const locked = replayEvents(
      [{ ...ENV, session_id: sid, turn_id: "t3", seq: 1, type: "verification.result", status: "locked", ticket: TICKET }],
      st,
    );
    expect(verificationCardState(locked, "t2", cardIndex(locked))).toBe("locked");
  });
});

describe("request bodies (doc 07 s2.3)", () => {
  it("verificationBody trims and uppercases the account id and requires a 4-digit PIN", () => {
    expect(verificationBody(" qs-10423 ", "4821")).toEqual({ account_id: "QS-10423", pin: "4821" });
    expect(verificationBody("QS-10423", "482")).toBeNull();
    expect(verificationBody("QS-10423", "48a1")).toBeNull();
    expect(verificationBody("", "4821")).toBeNull();
    expect(verificationBody("Q".repeat(21), "4821")).toBeNull();
  });

  it("confirmBody carries only the pending id and the decision (no step-up PIN)", () => {
    expect(confirmBody("PA-3F2A9C1B", "confirm")).toEqual({ pending_action_id: "PA-3F2A9C1B", decision: "confirm" });
    expect(confirmBody("PA-3F2A9C1B", "cancel")).toEqual({ pending_action_id: "PA-3F2A9C1B", decision: "cancel" });
  });
});

describe("formatters and rule title", () => {
  it("formats amount, charge date and countdown", () => {
    expect(formatAmount(9600)).toBe("$96.00");
    expect(formatAmount(24000)).toBe("$240.00");
    expect(formatChargeDate("2026-11-21")).toBe("21 Nov 2026");
    expect(formatChargeDate("not a date")).toBe("not a date");
    expect(formatCountdown(272_000)).toBe("4:32");
    expect(formatCountdown(686_000)).toBe("11:26");
    expect(formatCountdown(-5)).toBe("0:00");
  });

  it("ruleTitle reads the policy title from the prepare step of the same turn", () => {
    const st = replayEvents(c2);
    const turn = st.turns[c2[0]?.turn_id as string];
    expect(turn && ruleTitle(turn, "PRO_WITHIN_LIMITS")).toBe("Refunds on the Pro plan");
    expect(turn && ruleTitle(turn, "OTHER")).toBeNull();
  });
});
