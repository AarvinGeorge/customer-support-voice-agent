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
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { defaultRehypePlugins, Streamdown } from "streamdown";

import { Button } from "@/components/ui/button";
import { safeHref, type AnswerState, type CitationView, type DeclineNote, type ErrorRow, type FallbackNotice, type TicketChipView } from "@/lib/turns/furniture";
import { cn } from "@/lib/utils";

/**
 * What: the open state of one hint: opens on hover or focus of its group, stays open while the
 *   pointer is on the hint itself, closes on Escape until the pointer or focus leaves and comes
 *   back, and shifts the open hint left so it ends inside the viewport.
 * Use when: every Hint (ticket chip, citation card, fallback badge).
 * Inputs: none.
 * Output: { open, group, hintRef, shift }: spread group on the wrapper, pass hintRef and shift
 *   to Hint.
 * Side effects: a document keydown listener while open; one layout read when it opens.
 * Role: WCAG 1.4.13 (dismissible, hoverable, persistent) and 1.4.10 (no sideways scroll at
 *   320 px) for the one popover pattern.
 */
function useHint() {
  const [hover, setHover] = useState(false);
  const [focus, setFocus] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [shift, setShift] = useState(0);
  const hintRef = useRef<HTMLSpanElement>(null);
  const open = (hover || focus) && !dismissed;

  useEffect(() => {
    if (!open) return;
    /**
     * What: closes the hint on Escape.
     * Use when: a key goes down anywhere while the hint is open.
     * Inputs: the keyboard event.
     * Output: none.
     * Side effects: sets dismissed.
     * Role: handler (1.4.13 dismissible without moving pointer or focus).
     */
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDismissed(true);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  useLayoutEffect(() => {
    const el = hintRef.current;
    if (!open || el === null) {
      setShift(0);
      return;
    }
    const over = el.getBoundingClientRect().right - (window.innerWidth - 16);
    setShift(over > 0 ? Math.ceil(over) : 0);
  }, [open]);

  const group = {
    onMouseEnter: () => {
      setDismissed(false);
      setHover(true);
    },
    onMouseLeave: () => setHover(false),
    onFocus: () => {
      setDismissed(false);
      setFocus(true);
    },
    onBlur: () => setFocus(false),
    onKeyDown: (e: ReactKeyboardEvent) => {
      if (e.key === "Escape" && open) setDismissed(true);
    },
  };
  return { open, group, hintRef, shift };
}

/**
 * What: a hint under its trigger, shown while open; always in the DOM (display none when closed)
 *   so the trigger can point at it with aria-describedby.
 * Use when: ticket chip, citation hover card, fallback badge (with useHint).
 * Inputs: { id, open, hintRef, shift, className, children }.
 * Output: a role="tooltip" element with data-open.
 * Side effects: none.
 * Role: one popover pattern, no portal and no modal (DESIGN.md: no modals first; flat surfaces).
 *   The top padding (not a margin) bridges the gap so the pointer can move onto the hint.
 */
function Hint({
  id,
  open,
  hintRef,
  shift,
  className,
  children,
}: {
  id: string;
  open: boolean;
  hintRef: RefObject<HTMLSpanElement | null>;
  shift: number;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      id={id}
      ref={hintRef}
      role="tooltip"
      data-open={open ? "true" : "false"}
      style={shift > 0 ? { transform: `translateX(-${shift}px)` } : undefined}
      className={cn("absolute top-full left-0 z-20 pt-2", open ? "block motion-safe:animate-in motion-safe:fade-in-0" : "hidden", className)}
    >
      {children}
    </span>
  );
}

/**
 * What: scroll margins that keep a focused chip clear of the sticky header and input bar.
 * Use when: any focusable chip in the log.
 * Inputs: none.
 * Output: a class string.
 * Side effects: none.
 * Role: WCAG 2.4.11 focus not obscured (the html scroll padding does the same for every control).
 */
const CLEAR = "scroll-mt-[var(--header-clearance)] scroll-mb-[var(--input-bar-clearance)]";

/**
 * What: the dark tooltip surface (ink on paper) from the Figma tooltips.
 * Use when: ticket chip and fallback badge hints.
 * Inputs: none.
 * Output: a class string.
 * Side effects: none.
 * Role: style constant.
 */
const TOOLTIP = "flex w-max max-w-[min(30rem,calc(100vw-2rem))] flex-col gap-0.5 rounded-[8px] bg-ink px-3 py-2 text-caption text-paper";

/**
 * What: the pill chip surface (raised, Rule border) shared by ticket, citation and badge chips.
 * Use when: any chip.
 * Inputs: none.
 * Output: a class string.
 * Side effects: none.
 * Role: style constant (Figma chips: 10px by 4px, pill, Label Small).
 */
const CHIP = `inline-flex max-w-full items-center gap-1.5 rounded-full border bg-raised px-2.5 py-1 text-label-sm outline-none transition-colors duration-150 ease-out-expo focus-visible:ring-2 focus-visible:ring-ring ${CLEAR}`;

/**
 * What: the rehype plugins for model markdown: Streamdown's sanitize and harden, without its raw
 *   HTML plugin, so raw HTML in the text stays literal text and never becomes DOM.
 * Use when: AnswerMarkdown.
 * Inputs: none.
 * Output: a plugin list.
 * Side effects: throws at load if Streamdown stops exporting either plugin (fail loudly, never
 *   render without the sanitizer).
 * Role: guard (doc 07 s5.3 model output is untrusted; review PR #29 R1).
 */
const ANSWER_REHYPE = (["sanitize", "harden"] as const).map((name) => {
  const plugin = defaultRehypePlugins[name];
  if (plugin === undefined) throw new Error(`streamdown has no default rehype plugin "${name}"`);
  return plugin;
});

/**
 * What: the only elements model markdown may produce; anything else keeps its text only.
 * Use when: AnswerMarkdown (allowedElements with unwrapDisallowed).
 * Inputs: none.
 * Output: a tag list.
 * Side effects: none.
 * Role: allow list (review PR #29 I1 and M6: no images, media, headings, tables or raw HTML
 *   widgets in a support answer).
 */
const MARKDOWN_ELEMENTS = ["p", "br", "ul", "ol", "li", "strong", "em", "del", "code", "a", "span"] as const;

/**
 * What: the answer text as markdown (paragraphs, lists, emphasis, links, inline code), streamed
 *   or settled.
 * Use when: an assistant turn has text.
 * Inputs: { text, streaming }.
 * Output: Streamdown output in Body type; only http(s) links are live (new tab, rel noopener
 *   noreferrer); other links render as their text; images never render.
 * Side effects: none (no element can fetch a remote resource).
 * Role: markdown renderer (doc 07 s6.2 FinalAnswer.text "markdown allowed"). Model text is
 *   untrusted: raw HTML stays text (ANSWER_REHYPE drops the raw plugin), every other element is unwrapped
 *   to its text (MARKDOWN_ELEMENTS), and img maps to nothing, so a prompt-injected answer cannot
 *   load a tracking image or link to mailto, tel, same-origin or other schemes.
 */
export function AnswerMarkdown({ text, streaming }: { text: string; streaming: boolean }) {
  return (
    <div className="answer-markdown max-w-[70ch] text-body text-ink">
      <Streamdown
        mode={streaming ? "streaming" : "static"}
        parseIncompleteMarkdown={streaming}
        controls={false}
        linkSafety={{ enabled: false }}
        rehypePlugins={ANSWER_REHYPE}
        allowedElements={MARKDOWN_ELEMENTS}
        unwrapDisallowed
        components={{
          img: () => null,
          strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
          a: ({ href, children }) => {
            const safe = typeof href === "string" ? safeHref(href) : null;
            if (safe === null) return <span>{children}</span>;
            return (
              <a href={safe} target="_blank" rel="noopener noreferrer" className="text-ink underline decoration-rule underline-offset-2 hover:decoration-ink">
                {children}
              </a>
            );
          },
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
  const hint = useHint();
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
    <span className="relative inline-flex" {...hint.group}>
      <button type="button" aria-describedby={id} onClick={copy} className={cn(CHIP, "border-rule hover:border-muted")}>
        <LifeBuoy aria-hidden className="size-3.5 shrink-0 text-ink" />
        <span className="text-ink">{chip.label}</span>
        <span className="text-muted">· {chip.meta}</span>
      </button>
      <Hint id={id} open={hint.open} hintRef={hint.hintRef} shift={hint.shift}>
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
  const hint = useHint();
  const inner = (
    <>
      <FileText aria-hidden className="size-3.5 shrink-0 text-ink" />
      <span className="truncate text-ink">{view.chip}</span>
      {view.href && <ExternalLink aria-hidden className="size-3 shrink-0 text-muted" />}
    </>
  );
  return (
    <span className="relative inline-flex max-w-full" {...hint.group}>
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
        <span tabIndex={0} role="note" aria-describedby={id} className={cn(CHIP, "border-rule")}>
          {inner}
        </span>
      )}
      <Hint id={id} open={hint.open} hintRef={hint.hintRef} shift={hint.shift}>
        <span className="flex w-[min(25rem,calc(100vw-2rem))] flex-col gap-2 rounded-[12px] border border-rule bg-raised p-4">
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
  const hint = useHint();
  return (
    <span className="relative inline-flex" {...hint.group}>
      <button type="button" aria-describedby={id} className={cn(CHIP, "border-warning text-warning")}>
        <Repeat2 aria-hidden className="size-3.5 shrink-0" />
        <span>{notice.label}</span>
      </button>
      <Hint id={id} open={hint.open} hintRef={hint.hintRef} shift={hint.shift}>
        <span className={TOOLTIP}>
          <span className="font-medium">{notice.title}</span>
          <span>{notice.body}</span>
        </span>
      </Hint>
    </span>
  );
}
