/**
 * What: pure view rules for the verification card and the confirmation card: customer-safe words
 *   per verification result, the receipt line per action outcome, which turn hosts each card and
 *   its state, the request bodies, and the amount, date and countdown formatters.
 * Use when: the console renders cards (components/console/cards.tsx), and tests.
 * Inputs: ConversationState and TurnView from the reducer; doc 07 events and PendingPublic.
 * Output: plain strings, small view records and TurnRequest parts.
 * Side effects: none.
 * Role: CS-E-04.S6.T1 copy and state rules in one tested place (doc 07 s2.3, s6.2, s6.3, s7.1,
 *   s7.3). "Nothing was changed" appears only when core's result says changed false (s7.3).
 */
import type {
  ActionResultEvent,
  ConfirmInput,
  PendingPublic,
  VerificationInput,
  VerificationResultEvent,
  VerifiedPerson,
} from "@contracts/contracts";

import type { ConversationState, TurnView } from "./reducer";

/**
 * What: the color role of a card line; always paired with words (DESIGN.md Words First Rule).
 * Use when: styling a result or receipt line.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: maps to DESIGN.md state tokens.
 */
export type CardTone = "success" | "warning" | "error" | "muted";

/**
 * What: lower-case words for a verified person's role.
 * Use when: the verified line.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table.
 */
const ROLE_WORDS: Readonly<Record<VerifiedPerson["role"], string>> = Object.freeze({
  owner: "owner",
  billing_admin: "billing admin",
  member: "member",
});

/**
 * What: words for a plan tier.
 * Use when: the verified line.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table.
 */
const TIER_WORDS: Readonly<Record<VerifiedPerson["tier"], string>> = Object.freeze({
  free: "Free",
  pro: "Pro",
  business: "Business",
  enterprise: "Enterprise",
});

/**
 * What: the line shown for one verification.result.
 * Use when: rendering the result under the turn that carried it.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record.
 */
export interface VerificationNotice {
  status: VerificationResultEvent["status"];
  tone: CardTone;
  text: string;
}

/**
 * What: customer-safe words for a verification result (one per status in doc 07 s6.2).
 * Use when: a turn has a verification.result.
 * Inputs: the event.
 * Output: a VerificationNotice. "failed" never says which field was wrong (core answers the same
 *   for an unknown account and a wrong PIN, s3.1).
 * Side effects: none.
 * Role: copy rule.
 */
export function verificationNotice(ev: VerificationResultEvent): VerificationNotice {
  switch (ev.status) {
    case "verified": {
      const p = ev.person;
      const text = p ? `Verified as ${p.display_name}, ${ROLE_WORDS[p.role]}, ${TIER_WORDS[p.tier]} plan` : "Verified";
      return { status: ev.status, tone: "success", text };
    }
    case "failed": {
      const left = ev.attempts_left;
      const tail = typeof left === "number" ? ` ${left} ${left === 1 ? "attempt" : "attempts"} left.` : "";
      return { status: ev.status, tone: "error", text: `That account ID and PIN didn't match.${tail}` };
    }
    case "locked": {
      // D-070: a locked session has no verified requester, so no ticket exists and nobody can follow up.
      return {
        status: ev.status,
        tone: "error",
        text: "For your security, verification is locked for this conversation. See How to reach support in our help center for what to do next.",
      };
    }
    case "conflict":
      return {
        status: ev.status,
        tone: "warning",
        text: "This conversation is already verified as someone else. Please start a new conversation to verify as another person.",
      };
    case "unavailable":
      return {
        status: ev.status,
        tone: "warning",
        text: "We couldn't reach our account system, so you aren't verified yet. Please try again in a moment.",
      };
    default: {
      const unreachable: never = ev.status;
      return unreachable;
    }
  }
}

/**
 * What: the receipt line for one action.result.
 * Use when: rendering the outcome under the turn that carried it, and the closed card footer.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record.
 */
export interface Receipt {
  outcome: ActionResultEvent["outcome"];
  tone: CardTone;
  title: string;
  detail: string | null;
}

/**
 * What: done and not-done words per action type.
 * Use when: committed and failed receipts.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (action_type is a free string on action.result; unknown types use "other").
 */
const ACTION_WORDS: Readonly<Record<string, { done: string; notDone: string }>> = Object.freeze({
  refund: { done: "Refund issued", notDone: "Refund was not issued" },
  billing_email: { done: "Billing email changed", notDone: "Billing email was not changed" },
  grace_extension: { done: "Grace period extended", notDone: "Grace period was not extended" },
  other: { done: "Done", notDone: "Not done" },
});

/**
 * What: a USD amount from cents ("$96.00").
 * Use when: the card's Amount row and receipts.
 * Inputs: integer cents.
 * Output: a string.
 * Side effects: none.
 * Role: formatter (PendingPublic has no currency field; Quillstack bills in USD, decision in the
 *   CS-E-04.S6 report).
 */
export function formatAmount(cents: number): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(cents / 100);
}

/**
 * What: short month names.
 * Use when: formatChargeDate.
 * Inputs: none.
 * Output: a frozen array.
 * Side effects: none.
 * Role: constant (no Intl here so the date never shifts with the viewer's time zone).
 */
const MONTHS = Object.freeze(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]);

/**
 * What: a business date "2026-11-21" as "21 Nov 2026".
 * Use when: the card's Charge date row.
 * Inputs: the YYYY-MM-DD string from PendingPublic.charge_date.
 * Output: the formatted date, or the input unchanged when it is not YYYY-MM-DD.
 * Side effects: none.
 * Role: formatter.
 */
export function formatChargeDate(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  const month = m ? MONTHS[Number(m[2]) - 1] : undefined;
  if (!m || month === undefined) return iso;
  return `${Number(m[3])} ${month} ${m[1]}`;
}

/**
 * What: remaining time as m:ss ("4:32"), never below 0:00.
 * Use when: the card's expiry countdown.
 * Inputs: milliseconds left.
 * Output: a string.
 * Side effects: none.
 * Role: formatter (rounds up, so 0:00 shows only at expiry).
 */
export function formatCountdown(ms: number): string {
  const s = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/**
 * What: the receipt words for one action.result (every outcome in doc 07 s6.2 and s7.3).
 * Use when: a turn has action results; the confirmation card's closed footer.
 * Inputs: the event, and the pending action it answers when known (for amount, invoice and kind).
 * Output: a Receipt. "nothing was changed" is added only when `changed === false`; a failed
 *   submit for approval says "nothing was sent" instead (s7.3, review PR #24 M9).
 * Side effects: none.
 * Role: copy rule.
 */
export function receiptFor(ev: ActionResultEvent, pending: PendingPublic | null): Receipt {
  const submit = pending?.kind === "submit_for_approval";
  const words = submit
    ? { done: "Sent for approval", notDone: "Request was not sent" }
    : (ACTION_WORDS[ev.action_type] ?? (ACTION_WORDS.other as { done: string; notDone: string }));
  const unchanged = ev.changed === false ? "nothing was changed" : null;
  const unsent = ev.changed === false ? (submit ? "nothing was sent" : "nothing was changed") : null;
  const ticket = ev.ticket?.display ?? null;
  const policy = ev.policy_refs[0];
  const rule = [ev.reason_codes[0], policy ? `${policy.title} §${policy.section}` : undefined].filter(Boolean).join(" · ") || null;
  const join = (...parts: (string | null | undefined)[]): string | null => parts.filter((p): p is string => !!p).join(" · ") || null;
  const make = (tone: CardTone, title: string, detail: string | null): Receipt => ({ outcome: ev.outcome, tone, title, detail });
  switch (ev.outcome) {
    case "committed": {
      const amount = typeof pending?.amount_cents === "number" ? formatAmount(pending.amount_cents) : null;
      return make("success", words.done, join(amount, pending?.invoice_ref));
    }
    case "act_alone_applied":
      return make("success", "Change applied", null);
    case "submitted":
      return make("warning", "Sent for approval", ticket);
    case "cancelled":
      return make("muted", "Cancelled", unchanged);
    case "declined":
      return make("error", "Declined", join(rule, unchanged));
    case "rules_changed":
      return make("warning", "Not done, the rules changed", join(rule, unchanged));
    case "expired":
      return make("muted", "Request no longer active", unchanged);
    case "failed":
      return make("error", words.notDone, join(unsent, ticket));
    case "unconfirmed":
      return make("error", "Outcome unconfirmed", ticket);
    case "step_up_failed":
      return make("error", "PIN didn't match", unchanged);
    case "step_up_locked":
      return make("error", "Request cancelled after 3 PIN tries", unchanged);
    default: {
      const unreachable: never = ev.outcome;
      return unreachable;
    }
  }
}

/**
 * What: where each card lives and what has answered it, across the whole conversation.
 * Use when: rendering cards; computed once per render of the log.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view index. `verificationHost` is the newest turn with verification.required, or a later
 *   turn whose verification.result was "unavailable" (the card moves below that line, review
 *   PR #24 M5);
 *   `results` maps a pending action id to its latest action.result (it arrives in a later turn
 *   than the card); `confirmationTurn` maps a pending id to the newest turn that showed its card;
 *   `latestPendingId` is the pending id of the newest confirmation.required; `pendings` maps a
 *   pending id to its newest PendingPublic (for the amount and invoice on a receipt).
 */
export interface CardIndex {
  verificationHost: string | null;
  results: Readonly<Record<string, ActionResultEvent>>;
  confirmationTurn: Readonly<Record<string, string>>;
  latestPendingId: string | null;
  pendings: Readonly<Record<string, PendingPublic>>;
}

/**
 * What: builds the CardIndex from conversation state.
 * Use when: the conversation log renders.
 * Inputs: the ConversationState.
 * Output: a CardIndex.
 * Side effects: none.
 * Role: index builder (turn order is arrival order).
 */
export function cardIndex(state: ConversationState): CardIndex {
  let verificationHost: string | null = null;
  let latestPendingId: string | null = null;
  const results: Record<string, ActionResultEvent> = {};
  const confirmationTurn: Record<string, string> = {};
  const pendings: Record<string, PendingPublic> = {};
  for (const id of state.turnOrder) {
    const turn = state.turns[id];
    if (!turn) continue;
    if (turn.verificationRequired || turn.verificationResult?.status === "unavailable") verificationHost = id;
    if (turn.confirmation) {
      confirmationTurn[turn.confirmation.pending.pending_action_id] = id;
      pendings[turn.confirmation.pending.pending_action_id] = turn.confirmation.pending;
      latestPendingId = turn.confirmation.pending.pending_action_id;
    }
    for (const r of turn.actionResults) {
      if (r.pending_action_id) results[r.pending_action_id] = r;
      // A press answered without a pending id (doc 07 s6.2 allows null) closes the open card it
      // came from: the newest card, when this turn was started by a confirm press (M3).
      else if (turn.started?.input_kind === "confirm" && latestPendingId !== null && !results[latestPendingId]) {
        results[latestPendingId] = r;
      }
    }
  }
  return { verificationHost, results, confirmationTurn, latestPendingId, pendings };
}

/**
 * What: the state of one confirmation card.
 * Use when: deciding whether Confirm and Cancel show.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view state; only "open" shows buttons.
 */
export type ConfirmationCardState =
  | { kind: "open" }
  | { kind: "resolved"; result: ActionResultEvent }
  | { kind: "superseded" }
  | { kind: "expired" };

/**
 * What: decides a confirmation card's state.
 * Use when: rendering the card in its turn.
 * Inputs: the pending action, the hosting turn id, the CardIndex, the current client time and
 *   the client-clock deadline (null when the pending has no expires_at).
 * Output: resolved (an action.result answered this pending, by press or by a typed yes or no),
 *   else superseded (a newer card exists: another pending, or this one shown again later; doc 07
 *   s3.3 "lane C greys that card out"), else expired (deadline passed, s7.1 600 s), else open.
 * Side effects: none.
 * Role: state rule. The server stays the judge: a press on a card the server no longer holds
 *   comes back as action.result expired (s7.3).
 */
export function confirmationCardState(
  pending: PendingPublic,
  turnId: string,
  index: CardIndex,
  nowMs: number,
  deadlineMs: number | null,
): ConfirmationCardState {
  const id = pending.pending_action_id;
  const result = index.results[id];
  if (result) return { kind: "resolved", result };
  if (index.latestPendingId !== id || index.confirmationTurn[id] !== turnId) return { kind: "superseded" };
  if (deadlineMs !== null && nowMs >= deadlineMs) return { kind: "expired" };
  return { kind: "open" };
}

/**
 * What: decides what the verification card in one turn shows.
 * Use when: rendering a turn that carried verification.required.
 * Inputs: the conversation, the turn id, the CardIndex.
 * Output: "form" (the live card), "locked" (verification locked for the session), or "hidden"
 *   (not the newest request, already verified, or closed by a conflict).
 * Side effects: none.
 * Role: state rule (doc 07 s6.3 steps 4 and 5).
 */
export function verificationCardState(state: ConversationState, turnId: string, index: CardIndex): "form" | "locked" | "hidden" {
  if (index.verificationHost !== turnId || state.verified !== null) return "hidden";
  if (state.verifyLocked) return "locked";
  return state.awaitingVerification === null ? "hidden" : "form";
}

/**
 * What: the TurnRequest.verification body from the card's fields, or null if not valid.
 * Use when: the Verify press.
 * Inputs: the typed account id and PIN.
 * Output: VerificationInput (account id trimmed and upper-cased, 1..20 chars; PIN exactly 4
 *   digits) or null.
 * Side effects: none.
 * Role: request builder (doc 07 s2.3; core normalizes the account id further).
 */
export function verificationBody(accountId: string, pin: string): VerificationInput | null {
  const account_id = accountId.trim().toUpperCase();
  if (account_id.length < 1 || account_id.length > 20 || !/^\d{4}$/.test(pin)) return null;
  return { account_id, pin };
}

/**
 * What: the TurnRequest.confirm body for a press.
 * Use when: Confirm or Cancel.
 * Inputs: the pending action id and the decision.
 * Output: ConfirmInput without step_up_pin (step-up is designed only in the thin slice).
 * Side effects: none.
 * Role: request builder (doc 07 s2.3).
 */
export function confirmBody(pendingActionId: string, decision: ConfirmInput["decision"]): ConfirmInput {
  return { pending_action_id: pendingActionId, decision };
}

/**
 * What: the policy title behind a reason code, from the prepare step's rule in the same turn.
 * Use when: the card's Rule row (PendingPublic carries the code only).
 * Inputs: the turn, the reason code.
 * Output: the PolicyRef title, or null when no prepare step in the turn names that code.
 * Side effects: none.
 * Role: lookup (doc 07 s6.2 step.completed.rule).
 */
export function ruleTitle(turn: TurnView, reasonCode: string): string | null {
  for (const id of turn.stepOrder) {
    const rule = turn.steps[id]?.completed?.rule;
    if (rule && rule.reason_code === reasonCode) return rule.policy_ref.title;
  }
  return null;
}

/**
 * What: the policy title for a reason code, from the given turn or else the newest earlier turn
 *   whose prepare step names it.
 * Use when: the card's Rule row, including a card shown again by a code read-back with no
 *   prepare step of its own (review PR #24 M6).
 * Inputs: the conversation, the card's turn id, the reason code.
 * Output: the PolicyRef title, or null.
 * Side effects: none.
 * Role: lookup over ruleTitle.
 */
export function ruleTitleIn(state: ConversationState, turnId: string, reasonCode: string): string | null {
  const end = state.turnOrder.indexOf(turnId);
  for (let i = end; i >= 0; i -= 1) {
    const turn = state.turns[state.turnOrder[i] as string];
    const title = turn ? ruleTitle(turn, reasonCode) : null;
    if (title !== null) return title;
  }
  return null;
}
