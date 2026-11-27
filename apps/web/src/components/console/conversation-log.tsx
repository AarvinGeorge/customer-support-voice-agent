"use client";
/**
 * What: the conversation log: one thread of user items and agent turns, chat and voice together,
 *   each agent turn with its StepTrajectory, answer text, failure row and footer.
 * Use when: the console renders the store's thread.
 * Inputs: { thread, turns, onRetry? }.
 * Output: a role="log" region of <article> items (aria-live off: the console's one status region
 *   announces, review PR #20 M3), or the empty state.
 * Side effects: none.
 * Role: CS-E-04.S5.T1 unified thread (doc 06 s16, D-002). Cards, citations, tickets and
 *   markdown arrive with CS-E-04.S5.T3 and CS-E-04.S6.T1.
 */
import { CircleAlert, Mic, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";

import type { TurnView } from "@/lib/turns/reducer";
import type { ThreadItem } from "@/lib/turns/store";

import { StepTrajectory } from "./step-trajectory";

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
 * What: one agent turn: trail, answer text, the terminal failure row, footer.
 * Use when: a turn thread item.
 * Inputs: { turn }.
 * Output: an <article>.
 * Side effects: none.
 * Role: assistant message (plain text paragraphs until Streamdown lands in CS-E-04.S5.T3).
 */
function AssistantMessage({ turn }: { turn: TurnView }) {
  const paragraphs = turn.text.split(/\n{2,}/).filter((p) => p.trim() !== "");
  const latency = turn.completed ? `${(turn.completed.usage.latency_ms / 1000).toFixed(2)} s` : null;
  return (
    <article aria-label="Assistant" className="flex flex-col items-start gap-3.5">
      <StepTrajectory turn={turn} />
      {paragraphs.length > 0 && (
        <div className="flex max-w-[70ch] flex-col gap-3 text-body text-ink">
          {paragraphs.map((p, i) => (
            <p key={i} className="whitespace-pre-wrap">
              {p}
            </p>
          ))}
        </div>
      )}
      {turn.failure && (
        <p className="inline-flex items-start gap-2 text-body-sm text-ink">
          <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-error" />
          <span>
            <span className="font-medium text-error">Couldn&apos;t finish. </span>
            <span>{turn.failure.message}</span>
            {turn.failure.retryable && <span className="text-muted"> You can send your message again.</span>}
          </span>
        </p>
      )}
      {latency !== null && (
        <p className="font-mono text-mono-sm text-muted tabular">
          <span>{modeWord(turn.mode)}</span>
          <span> · {latency}</span>
        </p>
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
 * Inputs: { thread, turns } from the turn store; onRetry for error rows.
 * Output: JSX.
 * Side effects: none.
 * Role: see module.
 */
export function ConversationLog({
  thread,
  turns,
  onRetry,
}: {
  thread: readonly ThreadItem[];
  turns: Readonly<Record<string, TurnView>>;
  onRetry?: (errorId: string) => void;
}) {
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
      {thread.map((item) => {
        if (item.kind === "user") return <UserMessage key={item.id} text={item.text} mode={item.mode} />;
        if (item.kind === "error") return <SendError key={item.id} item={item} onRetry={onRetry} />;
        const turn = turns[item.turnId];
        return turn ? <AssistantMessage key={item.id} turn={turn} /> : null;
      })}
    </div>
  );
}
