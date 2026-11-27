"use client";
/**
 * What: the message furniture around an assistant answer: markdown text (Streamdown), the Gate 2
 *   hold, slow and retrying lines, error rows with Retry, ticket chips with the full id on hover
 *   and focus, the escalation line, the decline note, the Sources row of citation chips, and the
 *   fallback model badge.
 * Use when: the conversation log renders an agent turn.
 * Inputs: view records from lib/turns/furniture.ts and the turn's text.
 * Output: small JSX components.
 * Side effects: the ticket chip writes the full id to the clipboard on click; citation links open
 *   the public help page in a new tab.
 * Role: CS-E-04.S5.T3, laid out from the Figma pages "Console, chat (S1)" (S1 Message furniture
 *   states) and "States" (S3 decline, S5 fallback); DESIGN.md chips and state colors, every state
 *   in words too.
 */
import { CircleAlert, Clock, Copy, ExternalLink, FileText, LifeBuoy, RefreshCw, Repeat2, RotateCcw } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { Streamdown } from "streamdown";

import { Button } from "@/components/ui/button";
import type { AnswerState, CitationView, DeclineNote, ErrorRow, FallbackNotice, TicketChipView } from "@/lib/turns/furniture";
import { cn } from "@/lib/utils";

/**
 * What: a hint that shows above or below its trigger on hover and on keyboard focus, and is
 *   always in the DOM so the trigger can point at it with aria-describedby.
 * Use when: ticket chip, citation hover card, fallback badge.
 * Inputs: { id, className, children }.
 * Output: a role="tooltip" element, hidden until its group is hovered or holds focus.
 * Side effects: none.
 * Role: one popover pattern, no portal and no modal (DESIGN.md: no modals first; flat surfaces).
 */
function Hint({ id, className, children }: { id: string; className?: string; children: ReactNode }) {
  return (
    <span
      id={id}
      role="tooltip"
      className={cn(
        "pointer-events-none invisible absolute top-full left-0 z-20 mt-2 opacity-0 transition-opacity duration-150 ease-out-expo group-focus-within:visible group-focus-within:opacity-100 group-hover:visible group-hover:opacity-100",
        className,
      )}
    >
      {children}
    </span>
  );
}

/**
 * What: the dark tooltip surface (ink on paper) from the Figma tooltips.
 * Use when: ticket chip and fallback badge hints.
 * Inputs: none.
 * Output: a class string.
 * Side effects: none.
 * Role: style constant.
 */
const TOOLTIP = "flex w-max max-w-[min(30rem,85vw)] flex-col gap-0.5 rounded-[8px] bg-ink px-3 py-2 text-caption text-paper";

/**
 * What: the pill chip surface (raised, Rule border) shared by ticket, citation and badge chips.
 * Use when: any chip.
 * Inputs: none.
 * Output: a class string.
 * Side effects: none.
 * Role: style constant (Figma chips: 10px by 4px, pill, Label Small).
 */
const CHIP =
  "inline-flex max-w-full items-center gap-1.5 rounded-full border bg-raised px-2.5 py-1 text-label-sm outline-none transition-colors duration-150 ease-out-expo focus-visible:ring-2 focus-visible:ring-ring";

/**
 * What: the answer text as markdown (headings, lists, emphasis, links), streamed or settled.
 * Use when: an assistant turn has text.
 * Inputs: { text, streaming }.
 * Output: Streamdown output in Body type; links open in a new tab with rel noopener noreferrer.
 * Side effects: none.
 * Role: markdown renderer (doc 07 s6.2 FinalAnswer.text "markdown allowed"); Streamdown keeps its
 *   default sanitize and harden plugins because model text is untrusted; no code or diagram
 *   plugins, no table controls.
 */
export function AnswerMarkdown({ text, streaming }: { text: string; streaming: boolean }) {
  return (
    <div className="answer-markdown max-w-[70ch] text-body text-ink">
      <Streamdown
        mode={streaming ? "streaming" : "static"}
        parseIncompleteMarkdown={streaming}
        controls={false}
        linkSafety={{ enabled: false }}
        components={{
          strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer" className="text-ink underline decoration-rule underline-offset-2 hover:decoration-ink">
              {children}
            </a>
          ),
        }}
      >
        {text}
      </Streamdown>
    </div>
  );
}

/**
 * What: the held answer: two skeleton lines and a caption, shown while Gate 2 checks the draft.
 * Use when: answerState is "checking_policy" (the live trail line already says "Checking the
 *   answer against policy...").
 * Inputs: none.
 * Output: JSX (the skeleton is decorative; the caption is the words).
 * Side effects: a slow pulse unless reduced motion is set.
 * Role: Figma "Held for Gate 2"; nothing streams before Gate 2 decides (doc 07 s6.4).
 */
export function HeldAnswer() {
  return (
    <div data-testid="answer-held" className="flex w-full max-w-[32.5rem] flex-col gap-3">
      <span aria-hidden className="h-3 w-full rounded-[6px] bg-rule motion-safe:animate-pulse" />
      <span aria-hidden className="h-3 w-3/5 rounded-[6px] bg-rule motion-safe:animate-pulse" />
      <p className="text-caption text-muted">The answer shows once it passes the policy check.</p>
    </div>
  );
}

/**
 * What: the slow or retrying line under a running turn, in the server's words.
 * Use when: answerState is "slow" or "retrying".
 * Inputs: { state, message }.
 * Output: a warning-toned line with an icon.
 * Side effects: none.
 * Role: status event view (doc 07 s6.2 status; L1 s8 "Taking a little longer").
 */
export function StatusLine({ state, message }: { state: Extract<AnswerState, "slow" | "retrying">; message: string }) {
  const Icon = state === "slow" ? Clock : RefreshCw;
  return (
    <p data-testid="answer-status" className="inline-flex items-center gap-2 text-body-sm text-warning">
      <Icon aria-hidden className={cn("size-4 shrink-0", state === "retrying" && "motion-safe:animate-spin [animation-duration:2s]")} />
      <span>{message}</span>
    </p>
  );
}

/**
 * What: one error row: "Couldn't finish." with the message, and Retry when retryable and the
 *   customer's text is known.
 * Use when: errorRows(turn) is not empty.
 * Inputs: { row, onRetry? }.
 * Output: JSX.
 * Side effects: Retry calls onRetry.
 * Role: error row (doc 07 s1.3 retryable; the browser never retries by itself, s8.1).
 */
export function TurnErrorRow({ row, onRetry }: { row: ErrorRow; onRetry?: () => void }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <p className="inline-flex items-start gap-2 text-body-sm text-ink">
        <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-error" />
        <span>
          <span className="font-medium text-error">Couldn&apos;t finish. </span>
          <span>{row.message}</span>
          {row.retryable && onRetry === undefined && <span className="text-muted"> You can send your message again.</span>}
        </span>
      </p>
      {row.retryable && onRetry !== undefined && (
        <Button variant="quiet" size="sm" onClick={onRetry}>
          <RotateCcw aria-hidden className="size-3.5" />
          Retry
        </Button>
      )}
    </div>
  );
}

/**
 * What: the ticket chip "Ticket ending 8213 · P3 · Billing"; hover or focus shows the full id,
 *   click copies it.
 * Use when: a turn carries a ticket (escalation, failed action, lockout, approval).
 * Inputs: { chip }.
 * Output: a button with a tooltip.
 * Side effects: navigator.clipboard.writeText on click (when available).
 * Role: DESIGN.md chips ("full ID on hover"), Figma "Ticket chip, hover shows the full ID".
 */
export function TicketChip({ chip }: { chip: TicketChipView }) {
  const id = useId();
  const [copied, setCopied] = useState(false);
  /**
   * What: copies the full ticket id.
   * Use when: the chip is clicked.
   * Inputs: none.
   * Output: none.
   * Side effects: clipboard write; flips the hint to "Copied" for 2 s.
   * Role: handler (fails quietly when the clipboard is blocked; the id stays visible in the hint).
   */
  const copy = () => {
    void navigator.clipboard
      ?.writeText(chip.ticketId)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      })
      .catch(() => undefined);
  };
  return (
    <span className="group relative inline-flex">
      <button type="button" aria-describedby={id} onClick={copy} className={cn(CHIP, "border-rule hover:border-muted")}>
        <LifeBuoy aria-hidden className="size-3.5 shrink-0 text-ink" />
        <span className="text-ink">{chip.label}</span>
        <span className="text-muted">· {chip.meta}</span>
      </button>
      <Hint id={id}>
        <span className={TOOLTIP}>
          <span className="font-mono text-mono font-medium">{chip.ticketId}</span>
          <span className="inline-flex items-center gap-1">
            {copied ? (
              "Copied the full ID"
            ) : (
              <>
                {chip.note} · <Copy aria-hidden className="size-3" /> Click to copy the full ID
              </>
            )}
          </span>
        </span>
      </Hint>
    </span>
  );
}

/**
 * What: the escalation reason line ("Handed to our team: ...").
 * Use when: a turn has an escalated event.
 * Inputs: { text }.
 * Output: a muted line with the life buoy icon.
 * Side effects: none.
 * Role: escalation reason in words (doc 07 s6.2 escalated.reason).
 */
export function EscalationLine({ text }: { text: string }) {
  return (
    <p className="inline-flex items-center gap-2 text-body-sm text-muted">
      <LifeBuoy aria-hidden className="size-4 shrink-0" />
      <span>{text}</span>
    </p>
  );
}

/**
 * What: the decline note: "Refund declined", core's quoted reason, then the reason codes and the
 *   policy refs in mono, and "nothing was changed" when core said so.
 * Use when: declineNotes(turn) is not empty.
 * Inputs: { note, action }.
 * Output: a role="group" block on Margin, labelled "{Action} declined".
 * Side effects: none.
 * Role: Figma "S3 Member refund declined" quoted policy block; the policy is quoted when the
 *   answer is no (PRODUCT.md).
 */
export function DeclineNoteBlock({ note, action }: { note: DeclineNote; action: string }) {
  const id = useId();
  const title = `${action} declined`;
  return (
    <div role="group" aria-labelledby={id} className="flex w-full flex-col gap-1.5 rounded-md bg-margin px-4 py-3.5">
      <p id={id} className="inline-flex items-center gap-1.5 text-label-sm text-error">
        <CircleAlert aria-hidden className="size-3.5 shrink-0" />
        {title}
      </p>
      <p className="text-body text-ink">&ldquo;{note.text}&rdquo;</p>
      <p className="font-mono text-mono-sm break-words text-muted">
        {[note.codes, ...note.refs].filter(Boolean).join(" · ")}
        {note.unchanged && <span> · nothing was changed</span>}
      </p>
    </div>
  );
}

/**
 * What: one citation chip linking the public help page, with its hover and focus card (section,
 *   excerpt, url, retrieval score).
 * Use when: the Sources row.
 * Inputs: { view }.
 * Output: an <a target="_blank" rel="noopener noreferrer"> chip, or a plain chip when the url is
 *   not http(s).
 * Side effects: opening the link opens a new tab.
 * Role: Figma "Citation chip, links to the public help article" (doc 07 s16: citations link the
 *   public Notion page).
 */
function CitationChip({ view }: { view: CitationView }) {
  const id = useId();
  const inner = (
    <>
      <FileText aria-hidden className="size-3.5 shrink-0 text-ink" />
      <span className="truncate text-ink">{view.chip}</span>
      {view.href && <ExternalLink aria-hidden className="size-3 shrink-0 text-muted" />}
    </>
  );
  return (
    <span className="group relative inline-flex max-w-full">
      {view.href ? (
        <a
          href={view.href}
          target="_blank"
          rel="noopener noreferrer"
          aria-describedby={id}
          className={cn(CHIP, "border-rule hover:border-muted")}
        >
          {inner}
          <span className="sr-only">(opens in a new tab)</span>
        </a>
      ) : (
        <span tabIndex={0} aria-describedby={id} className={cn(CHIP, "border-rule")}>
          {inner}
        </span>
      )}
      <Hint id={id}>
        <span className="flex w-[min(25rem,85vw)] flex-col gap-2 rounded-[12px] border border-rule bg-raised p-4">
          <span className="text-label text-ink">{view.heading}</span>
          <span className="text-body-sm text-ink">&ldquo;{view.excerpt}&rdquo;</span>
          <span className="truncate font-mono text-mono-sm text-muted">{view.urlText}</span>
          <span className="font-mono text-mono-sm text-muted tabular">{view.score}</span>
        </span>
      </Hint>
    </span>
  );
}

/**
 * What: the Sources row: "Source" or "Sources" and one chip per cited chunk.
 * Use when: a settled answer has citations.
 * Inputs: { views }.
 * Output: a role="group" labelled "Sources", or null when empty.
 * Side effects: none.
 * Role: Figma S1 "Sources" row.
 */
export function SourcesRow({ views }: { views: readonly CitationView[] }) {
  if (views.length === 0) return null;
  return (
    <div role="group" aria-label="Sources" className="flex max-w-full flex-wrap items-center gap-2">
      <span className="text-label-sm text-muted">{views.length === 1 ? "Source" : "Sources"}</span>
      {views.map((v) => (
        <CitationChip key={v.key} view={v} />
      ))}
    </div>
  );
}

/**
 * What: the fallback model badge (warning outline, words "Fallback model") with the reason and
 *   the model on hover and focus.
 * Use when: fallbackNotice(turn) is not null.
 * Inputs: { notice }.
 * Output: a focusable button with a tooltip (it does nothing on press).
 * Side effects: none.
 * Role: Figma "S5 LLM 429 with fallback model" badge (L1 s8).
 */
export function FallbackBadge({ notice }: { notice: FallbackNotice }) {
  const id = useId();
  return (
    <span className="group relative inline-flex">
      <button type="button" aria-describedby={id} className={cn(CHIP, "border-warning text-warning")}>
        <Repeat2 aria-hidden className="size-3.5 shrink-0" />
        <span>{notice.label}</span>
      </button>
      <Hint id={id}>
        <span className={TOOLTIP}>
          <span className="font-medium">{notice.title}</span>
          <span>{notice.body}</span>
        </span>
      </Hint>
    </span>
  );
}
