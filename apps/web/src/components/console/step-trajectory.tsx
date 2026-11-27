"use client";
/**
 * What: StepTrajectory, the step trail under an assistant message: a live line while the turn
 *   runs that settles to "n steps, s · outcome", and a disclosure that expands into one row per
 *   step (icon, past-tense title, mono detail, right-aligned latency; gate rows show the score
 *   against the thresholds in words and a small meter).
 * Use when: rendering any agent turn, chat or voice, that has steps.
 * Inputs: { turn: TurnView }.
 * Output: JSX, or null for a finished or failed turn with no steps.
 * Side effects: a 100 ms timer while the turn runs (elapsed seconds).
 * Role: CS-E-04.S5.T1 (doc 06 s16, F1 s4, DESIGN.md "Step trail"); copy rules live in
 *   lib/turns/trail.ts.
 */
import {
  BookOpen,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  Circle,
  CircleAlert,
  LifeBuoy,
  Lock,
  Receipt,
  ShieldCheck,
  Sparkles,
  TicketCheck,
  UserRound,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useEffect, useId, useState } from "react";

import type { TurnView } from "@/lib/turns/reducer";
import { formatSeconds, stepRow, trailSummary, type GateMeter, type StepIcon, type StepRowView, type TrailTone } from "@/lib/turns/trail";
import { cn } from "@/lib/utils";

/**
 * What: lucide icon per step icon role (F1 s4.1).
 * Use when: rendering a row.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: icon table.
 */
export const ICONS: Readonly<Record<StepIcon, LucideIcon>> = Object.freeze({
  verify: ShieldCheck,
  account: UserRound,
  billing: Receipt,
  search: BookOpen,
  wallet: Wallet,
  commit: CheckCheck,
  escalation: LifeBuoy,
  ticket: TicketCheck,
  model: Sparkles,
  ask: Lock,
  other: Circle,
});

/**
 * What: text color class per tone.
 * Use when: a row detail or the meter fill.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: token mapping (DESIGN.md state colors; ember only for the running step).
 */
export const TONE_TEXT: Readonly<Record<TrailTone, string>> = Object.freeze({
  muted: "text-muted",
  ember: "text-ember",
  success: "text-success",
  warning: "text-warning",
  error: "text-error",
});

/**
 * What: background class per tone, for the meter fill.
 * Use when: GateMeterBar.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: token mapping.
 */
const TONE_BG: Readonly<Record<TrailTone, string>> = Object.freeze({
  muted: "bg-muted",
  ember: "bg-ember",
  success: "bg-success",
  warning: "bg-warning",
  error: "bg-error",
});

/**
 * What: exponential ease-out (DESIGN.md motion).
 * Use when: expand and collapse transitions.
 * Inputs: none.
 * Output: a cubic-bezier tuple.
 * Side effects: none.
 * Role: constant.
 */
const EASE_OUT_EXPO = [0.16, 1, 0.3, 1] as const;

/**
 * What: milliseconds since the component mounted, refreshed every 100 ms while active.
 * Use when: the running summary's elapsed seconds.
 * Inputs: whether the timer should tick.
 * Output: elapsed milliseconds.
 * Side effects: an interval while active, cleared on stop or unmount.
 * Role: hook.
 */
function useElapsed(active: boolean): number {
  const [start] = useState(() => Date.now());
  const [now, setNow] = useState(start);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(timer);
  }, [active]);
  return now - start;
}

/**
 * What: the 48 px retrieval meter: fill to the score, ticks at tau_low (muted) and tau_high (ink).
 * Use when: a row carries a retrieval gate with a score.
 * Inputs: { meter }.
 * Output: JSX, hidden from assistive tech (the words carry the meaning).
 * Side effects: none.
 * Role: visual reinforcement only (DESIGN.md Words First Rule).
 */
export function GateMeterBar({ meter }: { meter: GateMeter }) {
  /** What: a 0 to 1 value as a clamped CSS percent. Use when: positioning. Inputs: n. Output: "41%". Side effects: none. Role: helper. */
  const pct = (n: number) => `${Math.min(Math.max(n, 0), 1) * 100}%`;
  return (
    <span aria-hidden className="relative inline-block h-1.5 w-12 shrink-0 rounded-[3px] bg-rule">
      <span className={cn("absolute inset-y-0 left-0 rounded-[3px]", TONE_BG[meter.tone])} style={{ width: pct(meter.score) }} />
      {meter.tauLow !== null && <span className="absolute -top-[3px] h-3 w-px bg-muted" style={{ left: pct(meter.tauLow) }} />}
      {meter.tauHigh !== null && <span className="absolute -top-[3px] h-3 w-[1.5px] bg-ink" style={{ left: pct(meter.tauHigh) }} />}
    </span>
  );
}

/**
 * What: one expanded step row.
 * Use when: the trail is open.
 * Inputs: { row }.
 * Output: an <li>.
 * Side effects: none (fades in when it arrives while the trail is open).
 * Role: row layout from the Figma "Step trail" frame.
 */
function StepRow({ row }: { row: StepRowView }) {
  const Icon = ICONS[row.icon];
  return (
    <motion.li
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.16, ease: EASE_OUT_EXPO }}
      className={cn(
        "grid grid-cols-[16px_200px_minmax(0,1fr)_auto] items-center gap-x-2.5 gap-y-0.5 rounded-[8px] px-3 py-[7px] max-sm:grid-cols-[16px_minmax(0,1fr)_auto]",
        row.running && "bg-ember-soft",
      )}
    >
      <Icon aria-hidden className="size-4 shrink-0 text-ink" strokeWidth={1.75} />
      <span className="truncate text-label text-ink">
        {row.title}
        <span className="sr-only">, {row.statusWords}</span>
      </span>
      <span className="flex min-w-0 items-center gap-2 max-sm:col-span-2 max-sm:col-start-2 max-sm:row-start-2">
        {row.detail && (
          <span aria-hidden={row.detailLabel ? true : undefined} className={cn("truncate font-mono text-mono-sm tabular", TONE_TEXT[row.tone])}>
            {row.detail}
          </span>
        )}
        {row.detail && row.detailLabel && <span className="sr-only">{row.detailLabel}</span>}
        {row.meter && <GateMeterBar meter={row.meter} />}
      </span>
      <span className="shrink-0 text-right font-mono text-mono-sm text-muted tabular max-sm:col-start-3 max-sm:row-start-1">{row.latency}</span>
    </motion.li>
  );
}

/**
 * What: the StepTrajectory component.
 * Use when: see module.
 * Inputs: { turn }.
 * Output: JSX or null.
 * Side effects: see module.
 * Role: see module. The open state is per turn and survives new events; collapsed by default.
 *   Announcements come from the console's single status region (review PR #20, M3). Under reduced
 *   motion the height change is instant (Motion's reducedMotion keeps height animations, M4).
 */
export function StepTrajectory({ turn }: { turn: TurnView }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const summary = trailSummary(turn);
  const running = summary.state === "running";
  const elapsed = useElapsed(running);
  const reduce = useReducedMotion() === true;
  if (summary.state !== "running" && summary.stepCount === 0) return null;

  const rows = turn.stepOrder.flatMap((id) => {
    const step = turn.steps[id];
    return step ? [stepRow(step, turn)] : [];
  });
  const alert = summary.state === "failed" || summary.failedCount > 0 || turn.completed?.end_reason === "error";
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <div className="flex w-full flex-col gap-2">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((v) => !v)}
        className="-mx-1 inline-flex max-w-full items-center gap-2 self-start rounded-sm px-1 text-left outline-none transition-colors duration-150 ease-out-expo hover:text-ink focus-visible:ring-2 focus-visible:ring-ring"
      >
        {running ? (
          <span aria-hidden data-icon="live" className="size-2 shrink-0 rounded-full bg-ember motion-safe:animate-pulse" />
        ) : alert ? (
          <CircleAlert aria-hidden data-icon="alert" className="size-3.5 shrink-0 text-error" />
        ) : (
          <Check aria-hidden data-icon="check" className="size-3.5 shrink-0 text-muted" />
        )}
        <span className={cn("truncate text-label", running ? "text-ink" : "text-muted")}>{summary.text}</span>
        {running && (
          <span aria-hidden className="shrink-0 font-mono text-mono-sm text-muted tabular">
            {formatSeconds(elapsed)}
          </span>
        )}
        <Chevron aria-hidden className="size-3.5 shrink-0 text-muted" />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            id={panelId}
            key="panel"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1, transition: { duration: reduce ? 0 : 0.2, ease: EASE_OUT_EXPO } }}
            exit={{ height: 0, opacity: 0, transition: { duration: reduce ? 0 : 0.15, ease: EASE_OUT_EXPO } }}
            className="w-full overflow-hidden"
          >
            <ol className="flex w-full flex-col rounded-[12px] bg-margin py-1.5" aria-label="Steps">
              <AnimatePresence initial={false}>
                {rows.map((row) => (
                  <StepRow key={row.id} row={row} />
                ))}
              </AnimatePresence>
            </ol>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
