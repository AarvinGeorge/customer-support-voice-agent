"use client";
/**
 * What: the conversation log: one thread of user items and agent turns, chat and voice together,
 *   each agent turn with its StepTrajectory, answer text, failure row and footer.
 * Use when: the console renders the store's thread.
 * Inputs: { thread, conversation, cards?, onRetry? }.
 * Output: a role="log" region of <article> items (aria-live off: the console's one status region
 *   announces, review PR #20 M3), or the empty state.
 * Side effects: none (card presses go to the `cards` callbacks).
 * Role: CS-E-04.S5.T1 unified thread (doc 06 s16, D-002); CS-E-04.S6.T1 adds the verification
 *   result line, receipts and the two cards; CS-E-04.S5.T3 adds markdown answers, the answer
 *   states (Gate 2 hold, slow, retrying), error rows with Retry, decline notes, the Sources row,
 *   ticket chips, the escalation line and the fallback badge. CS-E-05.S4.T3 adds what the voice
 *   agent said aloud: an interrupted read-back freezes at what was heard with an Interrupted chip
 *   (the full answer one click away), and speech outside a turn is its own spoken line.
 */
import { CircleAlert, Mic, PanelRight, Pause, RotateCcw } from "lucide-react";
import type { StatusEvent } from "@contracts/contracts";
import { useState, type RefObject } from "react";
import type { ConfirmInput, VerificationInput } from "@contracts/contracts";

import { Button } from "@/components/ui/button";

import { cardIndex, receiptFor, ruleTitleIn, verificationCardState, type CardIndex } from "@/lib/turns/cards";
import {
  answerState,
  citationViews,
  declineNotes,
  errorRows,
  escalationWords,
  fallbackNotice,
  isPrepareDecline,
  ticketChip,
  turnTickets,
} from "@/lib/turns/furniture";
import type { ConversationState, TurnView } from "@/lib/turns/reducer";
import type { ThreadItem } from "@/lib/turns/store";

import { ConfirmationCard, ReceiptLine, VerificationCard, VerificationResultLine } from "./cards";
import {
  AnswerMarkdown,
  DeclineNoteBlock,
  EscalationLine,
  FallbackBadge,
  HeldAnswer,
  SourcesRow,
  StatusLine,
  TicketChip,
  TurnErrorRow,
} from "./furniture";
import { StepTrajectory } from "./step-trajectory";

/**
 * What: per-turn selection for the observability drawer.
 * Use when: ConsoleShell passes it so each reply gets an Inspect button.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: `selected` is the turn the open drawer shows (null when it is closed).
 */
export interface InspectActions {
  onInspect: (turnId: string) => void;
  selected: string | null;
}

/**
 * What: what the log needs to make the cards live.
 * Use when: the console shell renders the log.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: `disabled` while a turn streams; the callbacks send one TurnRequest each and resolve
 *   when the turn settles; `account` and `setAccount` keep the typed Account ID across a retry
 *   card; `pinRef` points at the live card's PIN field (for focus). Without it the cards render
 *   but presses do nothing.
 */
export interface CardActions {
  disabled: boolean;
  account: string;
  setAccount: (value: string) => void;
  pinRef: RefObject<HTMLInputElement | null>;
  verify: (body: VerificationInput) => Promise<unknown>;
  decide: (body: ConfirmInput) => Promise<unknown>;
}

/**
 * What: card actions that do nothing.
 * Use when: the log renders without a shell (no session).
 * Inputs: none.
 * Output: a frozen CardActions.
 * Side effects: none.
 * Role: default.
 */
const NO_ACTIONS: CardActions = Object.freeze({
  disabled: false,
  account: "",
  setAccount: () => undefined,
  pinRef: { current: null },
  verify: async () => undefined,
  decide: async () => undefined,
});

/**
 * What: the words for a turn's mode.
 * Use when: footers and voice labels.
 * Inputs: the mode.
 * Output: "Chat" or "Voice".
 * Side effects: none.
 * Role: helper.
 */
const modeWord = (mode: "chat" | "voice"): string => (mode === "voice" ? "Voice" : "Chat");

/**
 * What: one customer message bubble (Margin tint, left-aligned like the Figma thread).
 * Use when: a user thread item.
 * Inputs: { text, mode }.
 * Output: an <article>.
 * Side effects: none.
 * Role: user message.
 */
function UserMessage({ text, mode }: { text: string; mode: "chat" | "voice" }) {
  return (
    <article aria-label="You said" className="flex flex-col items-start gap-1">
      {mode === "voice" && (
        <span className="inline-flex items-center gap-1 text-caption text-muted">
          <Mic aria-hidden className="size-3" />
          <span>Voice</span>
        </span>
      )}
      <p className="max-w-[30.75rem] rounded-[16px] bg-margin px-4 py-2.5 text-body whitespace-pre-wrap text-ink">{text}</p>
    </article>
  );
}

/**
 * What: one assistant voice item of the thread.
 * Use when: typing the spoken props.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: alias.
 */
type SpokenItem = Extract<ThreadItem, { kind: "agent-voice" }>;

/**
 * What: the Interrupted chip (warning outline, pause icon, the word).
 * Use when: an assistant voice item was cut by a barge-in or Stop speaking.
 * Inputs: none.
 * Output: a pill.
 * Side effects: none.
 * Role: chip (Figma "Assistant message (voice, interrupted)"; words first, color reinforces).
 */
function InterruptedChip() {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-warning bg-raised px-2.5 py-1 text-label-sm text-warning">
      <Pause aria-hidden className="size-3.5" />
      <span>Interrupted</span>
    </span>
  );
}

/**
 * What: an interrupted read-back: the words heard, the Interrupted chip with a note, and a
 *   disclosure that shows the turn's full answer.
 * Use when: the voice item linked to a turn was interrupted.
 * Inputs: { heard, full, streaming }.
 * Output: JSX (fragment inside the assistant article).
 * Side effects: local open state.
 * Role: 04 item 2.2 (the transcript shows only what was heard; core's text stays one click away,
 *   doc 07 s6.6 R-15).
 */
function InterruptedAnswer({ heard, full, streaming }: { heard: string; full: string; streaming: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      {open ? <AnswerMarkdown text={full} streaming={streaming} /> : <p className="text-body text-ink">{heard}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <InterruptedChip />
        <span className="text-caption text-muted">Audio stopped here. The full answer stays in text.</span>
        <Button variant="ghost" size="sm" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          {open ? "Show what was heard" : "Show full answer"}
        </Button>
      </div>
    </>
  );
}

/**
 * What: speech the voice agent said outside a relayed answer (a greeting, a holding phrase).
 * Use when: an assistant voice item with no turn.
 * Inputs: { item }.
 * Output: an <article> labelled "Assistant, spoken".
 * Side effects: none.
 * Role: shows what the realtime model said aloud (R-15 mitigation: nothing spoken is hidden).
 */
function SpokenLine({ item }: { item: SpokenItem }) {
  return (
    <article aria-label="Assistant, spoken" className="flex flex-col items-start gap-1.5">
      <span className="inline-flex items-center gap-1 text-caption text-muted">
        <Mic aria-hidden className="size-3" />
        <span>Voice, spoken</span>
      </span>
      <p className="text-body text-ink">{item.heard}</p>
      {item.interrupted && <InterruptedChip />}
    </article>
  );
}

/**
 * What: the subject word of a decline note per action type.
 * Use when: titling a DeclineNoteBlock ("Refund declined").
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (action_type is a free string; unknown types use "Request").
 */
const DECLINE_SUBJECT: Readonly<Record<string, string>> = Object.freeze({
  refund: "Refund",
  billing_email: "Billing email change",
  grace_extension: "Grace extension",
});

/**
 * What: one agent turn: trail, verification result and receipt lines, the answer (markdown, or
 *   the held, slow or retrying state), decline notes, the cards, Sources, ticket chips, the
 *   escalation line, error rows and the footer with the fallback badge.
 * Use when: a turn thread item.
 * Inputs: { turn, conversation, index, cards, onResend?, inspect?, number, spoken? } (onResend is set when
 *   the customer's typed text for this turn is known, so a retryable error row can offer Retry;
 *   inspect and the 1-based turn number add the footer's Inspect button; spoken is the voice
 *   read-back of this turn, which replaces the answer text when it was interrupted).
 * Output: an <article>.
 * Side effects: none.
 * Role: assistant message; the order follows Figma S1 and States (receipts and the quoted
 *   decline policy above the text, so the text's closing question comes last; cards below it,
 *   chips and footer last).
 */
function AssistantMessage({
  turn,
  conversation,
  index,
  cards,
  onResend,
  inspect,
  number,
  spoken,
}: {
  spoken?: SpokenItem;
  turn: TurnView;
  conversation: ConversationState;
  index: CardIndex;
  cards: CardActions;
  onResend?: () => void;
  inspect?: InspectActions;
  number: number;
}) {
  const latency = turn.completed ? `${(turn.completed.usage.latency_ms / 1000).toFixed(2)} s` : null;
  const verifyState = verificationCardState(conversation, turn.turnId, index);
  const state = answerState(turn);
  const status: StatusEvent | null = turn.status;
  const declines = declineNotes(turn);
  const sources = citationViews(turn.completed?.final.citations ?? []);
  const tickets = turnTickets(turn).map(ticketChip);
  const fallback = fallbackNotice(turn);
  const errors = errorRows(turn);
  return (
    <article aria-label="Assistant" className="flex flex-col items-start gap-3.5">
      <StepTrajectory turn={turn} />
      {turn.verificationResult && <VerificationResultLine result={turn.verificationResult} />}
      {turn.actionResults
        .filter((r) => !isPrepareDecline(r))
        .map((r) => (
          <ReceiptLine key={r.seq} receipt={receiptFor(r, (r.pending_action_id && index.pendings[r.pending_action_id]) || null)} />
        ))}
      {declines.map((note) => {
        const action = turn.actionResults.find((r) => String(r.seq) === note.key)?.action_type ?? "";
        return <DeclineNoteBlock key={note.key} note={note} action={DECLINE_SUBJECT[action] ?? "Request"} />;
      })}
      {spoken?.interrupted ? (
        <InterruptedAnswer heard={spoken.heard} full={turn.text} streaming={turn.phase === "streaming"} />
      ) : (
        turn.text.trim() !== "" && <AnswerMarkdown text={turn.text} streaming={turn.phase === "streaming"} />
      )}
      {state === "checking_policy" && <HeldAnswer />}
      {(state === "slow" || state === "retrying") && status && <StatusLine state={state} message={status.message} />}
      {verifyState !== "hidden" && (
        <VerificationCard
          request={conversation.awaitingVerification ?? turn.verificationRequired}
          state={verifyState}
          disabled={cards.disabled}
          account={cards.account}
          onAccountChange={cards.setAccount}
          pinRef={cards.pinRef}
          onSubmit={cards.verify}
        />
      )}
      {turn.confirmation && (
        <ConfirmationCard
          confirmation={turn.confirmation}
          turnId={turn.turnId}
          index={index}
          ruleTitle={ruleTitleIn(conversation, turn.turnId, turn.confirmation.pending.reason_code)}
          receivedAt={turn.confirmationReceivedAt}
          disabled={cards.disabled}
          onDecide={cards.decide}
        />
      )}
      <SourcesRow views={sources} />
      {turn.escalation && <EscalationLine text={escalationWords(turn.escalation)} />}
      {tickets.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {tickets.map((t) => (
            <TicketChip key={t.ticketId} chip={t} />
          ))}
        </div>
      )}
      {errors.map((row) => (
        <TurnErrorRow key={row.key} row={row} onRetry={row.retryable && !cards.disabled ? onResend : undefined} />
      ))}
      {(latency !== null || fallback !== null || inspect !== undefined) && (
        <div className="flex flex-wrap items-center gap-2.5">
          {fallback && <FallbackBadge notice={fallback} />}
          {latency !== null && (
            <p className="font-mono text-mono-sm text-muted tabular">
              <span>{modeWord(turn.mode)}</span>
              <span> · {latency}</span>
            </p>
          )}
          {inspect && (
            <button
              type="button"
              onClick={() => inspect.onInspect(turn.turnId)}
              aria-label={`Inspect turn ${number}`}
              aria-pressed={inspect.selected === turn.turnId}
              className="inline-flex items-center gap-1 rounded-sm px-1 font-mono text-mono-sm text-muted hover:text-ink focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none aria-pressed:text-ink aria-pressed:underline underline-offset-4"
            >
              <PanelRight aria-hidden className="size-3" />
              {inspect.selected === turn.turnId ? "Inspecting" : "Inspect"}
            </button>
          )}
        </div>
      )}
    </article>
  );
}

/**
 * What: the row for a send that failed before any reply event: what happened in words, and Retry
 *   when the failure is retryable and the text was kept.
 * Use when: an error thread item (review PR #20, I2).
 * Inputs: { item, onRetry? }.
 * Output: an <article>.
 * Side effects: Retry calls onRetry with the item id.
 * Role: error row (doc 07 s8.1: the browser never retries on its own; the customer may).
 */
function SendError({ item, onRetry }: { item: Extract<ThreadItem, { kind: "error" }>; onRetry?: (id: string) => void }) {
  const canRetry = item.retryable && item.text !== null && onRetry !== undefined;
  return (
    <article aria-label="Assistant" className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <p className="inline-flex items-start gap-2 text-body-sm text-ink">
        <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-error" />
        <span>
          <span className="font-medium text-error">Couldn&apos;t send. </span>
          <span>{item.message}</span>
        </span>
      </p>
      {canRetry && (
        <Button variant="quiet" size="sm" onClick={() => onRetry(item.id)}>
          <RotateCcw aria-hidden className="size-3.5" />
          Retry
        </Button>
      )}
    </article>
  );
}

/**
 * What: the conversation log component.
 * Use when: see module.
 * Inputs: { thread, conversation } from the turn store, the card actions, onRetry for send error
 *   rows, onResend for a retryable error inside the latest turn only (sends the customer's chat
 *   text again; an older error never resends out of order), inspect for the drawer's per-turn
 *   selection.
 * Output: JSX.
 * Side effects: none.
 * Role: see module.
 */
export function ConversationLog({
  thread,
  conversation,
  cards = NO_ACTIONS,
  onRetry,
  onResend,
  inspect,
}: {
  thread: readonly ThreadItem[];
  conversation: ConversationState;
  cards?: CardActions;
  onRetry?: (errorId: string) => void;
  onResend?: (text: string) => void;
  inspect?: InspectActions;
}) {
  const turns = conversation.turns;
  const index = cardIndex(conversation);
  const spokenByTurn = new Map<string, SpokenItem>();
  for (const item of thread) if (item.kind === "agent-voice" && item.turnId !== null && turns[item.turnId]) spokenByTurn.set(item.turnId, item);
  if (thread.length === 0) {
    return (
      <div className="flex flex-1 flex-col justify-center gap-2 py-12">
        <p className="text-heading text-ink">How can we help?</p>
        <p className="max-w-[60ch] text-body text-muted">
          Ask about a charge, a refund, your account or a how-to. Each step the agent takes, and every check it runs, shows
          under its reply.
        </p>
      </div>
    );
  }
  return (
    <div role="log" aria-label="Conversation" aria-live="off" className="flex flex-col gap-7">
      {thread.map((item, i) => {
        if (item.kind === "user") return <UserMessage key={item.id} text={item.text} mode={item.mode} />;
        if (item.kind === "error") return <SendError key={item.id} item={item} onRetry={onRetry} />;
        if (item.kind === "agent-voice") return item.turnId !== null && turns[item.turnId] ? null : <SpokenLine key={item.id} item={item} />;
        const turn = turns[item.turnId];
        const prev = thread[i - 1];
        const typed = prev?.kind === "user" && prev.mode === "chat" ? prev.text : null;
        const latest = i === thread.length - 1;
        const resend = latest && typed !== null && onResend !== undefined ? () => onResend(typed) : undefined;
        return turn ? (
          <AssistantMessage
            key={item.id}
            turn={turn}
            conversation={conversation}
            index={index}
            cards={cards}
            onResend={resend}
            inspect={inspect}
            number={conversation.turnOrder.indexOf(turn.turnId) + 1}
            spoken={spokenByTurn.get(turn.turnId)}
          />
        ) : null;
      })}
    </div>
  );
}
