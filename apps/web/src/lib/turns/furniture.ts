/**
 * What: pure view rules for the message furniture around an answer: the answer state (working,
 *   Gate 2 hold, slow, retrying, streaming), ticket chips, escalation words, decline notes,
 *   citations with safe links, the fallback model notice, error rows, the 2,000 character limit
 *   and the input placeholder.
 * Use when: the conversation log and input bar render (components/console/furniture.tsx), and tests.
 * Inputs: TurnView and ConversationState from the reducer; doc 07 TicketRef, Citation and events.
 * Output: plain strings and small view records.
 * Side effects: none.
 * Role: CS-E-04.S5.T3 copy and state rules in one tested place (doc 07 s3.4, s6.2, s6.4, s16;
 *   L1 s8). State is always given in words (DESIGN.md Words First Rule).
 */
import type { ActionResultEvent, Citation, EscalatedEvent, ModelFallbackEvent, TicketRef } from "@contracts/contracts";
import { LIMITS } from "@contracts/contracts";

import type { ConversationState, TurnView } from "./reducer";

/**
 * What: what the body of an assistant message shows right now.
 * Use when: choosing between the held skeleton, a slow or retrying line and the text.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view state. "working" (running, nothing to say yet), "checking_policy" (Gate 2 holds the
 *   draft, doc 07 s6.4), "slow" and "retrying" (status events), "streaming" (tokens arriving);
 *   null once the turn settled or failed.
 */
export type AnswerState = "working" | "checking_policy" | "slow" | "retrying" | "streaming" | null;

/**
 * What: the answer state of one turn.
 * Use when: rendering an assistant message body.
 * Inputs: the TurnView.
 * Output: an AnswerState.
 * Side effects: none.
 * Role: state rule (the reducer clears `status` on the first token and at completion).
 */
export function answerState(turn: TurnView): AnswerState {
  if (turn.phase !== "streaming") return null;
  if (turn.status !== null) return turn.status.kind;
  return turn.text !== "" ? "streaming" : "working";
}

/**
 * What: words for a ticket queue.
 * Use when: ticket chips.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (doc 07 s3.4 queues).
 */
const QUEUE_WORDS: Readonly<Record<TicketRef["queue"], string>> = Object.freeze({
  billing: "Billing",
  technical: "Technical",
  account: "Account",
  approvals: "Approvals",
});

/**
 * What: words for a ticket stage.
 * Use when: ticket chips.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (doc 07 s7.2 stages).
 */
const STAGE_WORDS: Readonly<Record<TicketRef["stage"], string>> = Object.freeze({
  open: "Open",
  pending_approval: "Pending approval",
  resolved: "Resolved",
  closed: "Closed",
});

/**
 * What: the ticket chip's words.
 * Use when: rendering a TicketChip.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record; `ticketId` is the full id shown on hover and focus (and copied on click).
 */
export interface TicketChipView {
  ticketId: string;
  label: string;
  meta: string;
  note: string;
}

/**
 * What: the chip for one ticket: "Ticket ending 8213", "P3 · Billing" (or the stage when it is
 *   not plain open), and the tooltip note "Open · Billing team".
 * Use when: a turn carries a ticket.
 * Inputs: the TicketRef.
 * Output: a TicketChipView.
 * Side effects: none.
 * Role: copy rule (Figma "Ticket chip, hover shows the full ID"; display comes from core).
 */
export function ticketChip(ticket: TicketRef): TicketChipView {
  const queue = QUEUE_WORDS[ticket.queue];
  const stage = STAGE_WORDS[ticket.stage];
  const label = ticket.display.charAt(0).toUpperCase() + ticket.display.slice(1);
  const meta = ticket.stage === "open" ? `${ticket.priority} · ${queue}` : `${stage} · ${queue}`;
  return { ticketId: ticket.ticket_id, label, meta, note: `${stage} · ${queue} team` };
}

/**
 * What: every ticket a turn reported, once each, in event order.
 * Use when: rendering the ticket chips under an answer.
 * Inputs: the TurnView.
 * Output: TicketRef[] (deduplicated by ticket_id; the latest copy wins, so a stage change shows).
 * Side effects: none.
 * Role: collects verification.result, action.result, escalated and final.ticket.
 */
export function turnTickets(turn: TurnView): TicketRef[] {
  const found = new Map<string, TicketRef>();
  /** What: records a ticket. Use when: collecting. Inputs: a TicketRef or nothing. Output: none. Side effects: sets `found`. Role: helper. */
  const add = (t: TicketRef | null | undefined) => {
    if (t) found.set(t.ticket_id, t);
  };
  add(turn.verificationResult?.ticket);
  for (const r of turn.actionResults) add(r.ticket);
  add(turn.escalation?.ticket);
  add(turn.completed?.final.ticket);
  return [...found.values()];
}

/**
 * What: words per escalation reason.
 * Use when: escalationWords.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table covering every doc 07 s6.2 escalated.reason.
 */
const ESCALATION_WORDS: Readonly<Record<EscalatedEvent["reason"], string>> = Object.freeze({
  not_in_docs: "our help articles don't cover this",
  grounding_failed: "the answer couldn't be checked against policy",
  retrieval_unavailable: "help article search was unavailable",
  tool_failure: "a system we rely on didn't respond",
  limit_reached: "this conversation reached a limit",
  deadline: "the reply ran out of time",
  verify_locked: "verification is locked for your security",
  needs_commitment: "this needs a decision from a person",
  customer_asked_human: "you asked for a person",
  model_unavailable: "our assistant is unavailable right now",
});

/**
 * What: the escalation reason line ("Handed to our team: ...").
 * Use when: a turn has an escalated event.
 * Inputs: the event.
 * Output: a sentence fragment without a final period. model_unavailable has no ticket until the
 *   customer asks (doc 07 s6.7), so it reads "Not handed over yet: ...".
 * Side effects: none.
 * Role: copy rule.
 */
export function escalationWords(ev: EscalatedEvent): string {
  const why = ESCALATION_WORDS[ev.reason];
  return ev.reason === "model_unavailable" ? `Not handed over yet: ${why}` : `Handed to our team: ${why}`;
}

/**
 * What: one decline note: core's customer-safe text, the reason codes and the policy refs.
 * Use when: rendering a declined action.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record.
 */
export interface DeclineNote {
  key: string;
  text: string;
  codes: string;
  refs: string[];
  unchanged: boolean;
}

/**
 * What: whether an action.result is a prepare decline (outcome declined, no pending action).
 * Use when: choosing a decline note over a receipt line.
 * Inputs: the event.
 * Output: boolean.
 * Side effects: none.
 * Role: predicate shared by declineNotes and the conversation log.
 */
export function isPrepareDecline(r: ActionResultEvent): boolean {
  return r.outcome === "declined" && (r.pending_action_id ?? null) === null;
}

/**
 * What: the decline notes of a turn, one per action.result with outcome declined and no pending
 *   action (a prepare that core declined; a decline that answers an open card stays a receipt on
 *   that card, CS-E-04.S6.T1).
 * Use when: the S3 member decline and every other business decline (doc 07 s6.2 action.result).
 * Inputs: the TurnView.
 * Output: DeclineNote[]; refs read "{title} · {article}#{section}" so the policy is traceable.
 * Side effects: none.
 * Role: copy rule ("quote the policy when it says no", PRODUCT.md).
 */
export function declineNotes(turn: TurnView): DeclineNote[] {
  return turn.actionResults
    .filter(isPrepareDecline)
    .map((r) => ({
      key: String(r.seq),
      text: r.result_text,
      codes: r.reason_codes.join(", "),
      refs: r.policy_refs.map((p) => `${p.title} · ${p.article}#${p.section}`),
      unchanged: r.changed === false,
    }));
}

/**
 * What: one citation chip and its hover card.
 * Use when: the Sources row.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record; `href` is null when the url is not http(s), so it is never a link.
 */
export interface CitationView {
  key: string;
  chip: string;
  heading: string;
  excerpt: string;
  href: string | null;
  urlText: string;
  score: string;
}

/**
 * What: a citation url as a link target, only for http and https.
 * Use when: building a CitationView.
 * Inputs: the url string from the agent.
 * Output: the normalized href, or null.
 * Side effects: none.
 * Role: guard (help article text and its urls are untrusted, L1 s10).
 */
function safeHref(url: string): string | null {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

/**
 * What: the citation views of an answer, one per chunk.
 * Use when: turn.completed final.citations (doc 07 s6.2, s16).
 * Inputs: the citations.
 * Output: CitationView[] (chip = section title; card = label, excerpt, url without the scheme,
 *   retrieval score to two decimals).
 * Side effects: none.
 * Role: copy rule.
 */
export function citationViews(citations: readonly Citation[]): CitationView[] {
  const seen = new Set<string>();
  const out: CitationView[] = [];
  for (const c of citations) {
    if (seen.has(c.chunk_id)) continue;
    seen.add(c.chunk_id);
    const href = safeHref(c.url);
    out.push({
      key: c.chunk_id,
      chip: c.section_title || c.label,
      heading: c.label,
      excerpt: c.excerpt,
      href,
      urlText: (href ?? c.url).replace(/^https?:\/\//, ""),
      score: `Retrieval score ${c.score.toFixed(2)}`,
    });
  }
  return out;
}

/**
 * What: the tooltip title per fallback reason.
 * Use when: fallbackNotice.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (doc 07 s6.2 model.fallback.reason).
 */
const FALLBACK_TITLES: Readonly<Record<ModelFallbackEvent["reason"], string>> = Object.freeze({
  rate_limit: "Primary model rate limited (429)",
  fault_llm_429: "Primary model rate limited (429)",
  timeout: "Primary model timed out",
  "5xx": "Primary model returned an error",
  spend_cap: "Primary model spend cap reached",
  auth: "Primary model rejected our credentials",
});

/**
 * What: the fallback badge and its tooltip.
 * Use when: rendering a message footer.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record.
 */
export interface FallbackNotice {
  label: string;
  title: string;
  body: string;
}

/**
 * What: the fallback notice of a turn, from its model.fallback event or, without one, from
 *   usage.fallback_used.
 * Use when: the message footer (L1 s8: "Fallback model, with a badge").
 * Inputs: the TurnView.
 * Output: a FallbackNotice, or null when the primary model answered.
 * Side effects: none.
 * Role: copy rule.
 */
export function fallbackNotice(turn: TurnView): FallbackNotice | null {
  const ev = turn.fallbacks.at(-1);
  if (ev === undefined && turn.completed?.usage.fallback_used !== true) return null;
  const to = ev?.to_model ? ` ${ev.to_model}` : "";
  return {
    label: "Fallback model",
    title: ev ? FALLBACK_TITLES[ev.reason] : "Primary model unavailable",
    body: `Answered by the fallback model${to}. Policy checks are unchanged.`,
  };
}

/**
 * What: one error row under a turn.
 * Use when: rendering errors.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record; Retry shows only when `retryable`.
 */
export interface ErrorRow {
  key: string;
  message: string;
  retryable: boolean;
}

/**
 * What: the error rows of a turn: each server `error` event (doc 07 s1.3 message and retryable),
 *   then the client failure when the stream ended without turn.completed.
 * Use when: rendering an assistant message.
 * Inputs: the TurnView.
 * Output: ErrorRow[].
 * Side effects: none.
 * Role: copy rule (the browser never retries by itself; Retry is the customer's choice, s8.1).
 */
export function errorRows(turn: TurnView): ErrorRow[] {
  const rows: ErrorRow[] = turn.errors.map((e) => ({ key: String(e.seq), message: e.message, retryable: e.retryable }));
  if (turn.failure) rows.push({ key: "failure", message: turn.failure.message, retryable: turn.failure.retryable });
  return rows;
}

/**
 * What: the input bar's character counter.
 * Use when: rendering the input bar.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record; `show` from 90 percent of the limit, `over` blocks sending.
 */
export interface MessageLimit {
  count: number;
  max: number;
  show: boolean;
  over: boolean;
  text: string;
}

/**
 * What: the character count against TurnRequest.text's 2,000 limit (doc 07 s2.3, LIMITS).
 * Use when: every input change.
 * Inputs: the typed text.
 * Output: MessageLimit; counts code points, the way the server's str length does.
 * Side effects: none.
 * Role: limit rule (the server still rejects over-long text with 422).
 */
export function messageLimit(text: string): MessageLimit {
  const max = LIMITS.MESSAGE_MAX_CHARS;
  const count = [...text].length;
  const fmt = new Intl.NumberFormat("en-US");
  return { count, max, show: count >= max * 0.9, over: count > max, text: `${fmt.format(count)} / ${fmt.format(max)}` };
}

/**
 * What: the input bar placeholder.
 * Use when: rendering the input bar.
 * Inputs: the conversation, whether there is no session.
 * Output: a sentence: no session, a pending confirmation (a typed yes or no works too, doc 07
 *   s7.3), or the default prompt.
 * Side effects: none.
 * Role: copy rule.
 */
export function inputPlaceholder(state: ConversationState, noSession: boolean): string {
  if (noSession) return "Start from the access page to open a session";
  if (state.pendingAction !== null) return "Reply yes to confirm, or no to cancel";
  return "Ask about billing, your account or a how-to";
}
