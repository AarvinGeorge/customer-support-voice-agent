"use client";
/**
 * What: the 429 and 503 banner above the input bar: what happened in words, a live m:ss
 *   countdown from retry_after_s, and the limit caption under the input bar.
 * Use when: the turn store holds a LimitNotice (a pre-stream 429 or 503, doc 07 s9).
 * Inputs: see each export.
 * Output: JSX.
 * Side effects: a one-second interval while a countdown runs; onDone when it reaches zero.
 * Role: CS-E-07.S1.T4, from the Figma "Rate limit banner (our 429)" frame.
 */
import { Clock } from "lucide-react";
import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";

import { formatClock, limitWords } from "@/lib/turns/limits";
import type { LimitNotice } from "@/lib/turns/store";

/**
 * What: whole seconds left until a deadline, refreshed every second.
 * Use when: the banner countdown and the input bar lock.
 * Inputs: the deadline in ms (null for none) and a callback for when it passes.
 * Output: seconds left (0 when none).
 * Side effects: an interval while time is left; calls onDone once at zero.
 * Role: hook.
 */
export function useSecondsLeft(untilMs: number | null, onDone: () => void): number {
  // The clock is an external store: subscribe once per deadline and read whole seconds left
  // (react.dev, "You Might Not Need an Effect": Subscribing to an external store).
  const subscribe = useCallback(
    (notify: () => void) => {
      if (untilMs === null) return () => {};
      const id = setInterval(notify, 1000);
      return () => clearInterval(id);
    },
    [untilMs],
  );
  const read = () => (untilMs === null ? 0 : Math.max(0, Math.ceil((untilMs - Date.now()) / 1000)));
  const left = useSyncExternalStore(subscribe, read, () => 0);
  const doneRef = useRef(onDone);
  useEffect(() => {
    doneRef.current = onDone;
  });
  // Reaching zero is an effect of the clock having run out: tell the owner once.
  useEffect(() => {
    if (untilMs !== null && left === 0) doneRef.current();
  }, [untilMs, left]);
  return left;
}

/**
 * What: the banner.
 * Use when: a notice is present.
 * Inputs: { notice, seconds } (seconds from useSecondsLeft).
 * Output: a labelled status region.
 * Side effects: none.
 * Role: the sentence is the banner's status text; the ticking clock is aria-hidden so it is never
 *   read every second. The wait length and the unlock are announced once each by the console's
 *   polite announcer (console-shell.tsx), not by this component.
 */
export function LimitBanner({ notice, seconds }: { notice: LimitNotice; seconds: number }) {
  const { message } = limitWords(notice);
  return (
    <div
      role="status"
      aria-label="Sending paused"
      className="mb-3 flex items-center gap-2.5 rounded-lg border border-rule bg-margin px-4 py-3 text-body-sm text-ink"
    >
      <Clock aria-hidden className="size-4 shrink-0 text-info" strokeWidth={1.75} />
      <span className="flex-1">{message}</span>
      {notice.untilMs !== null && (
        <span aria-hidden className="shrink-0 font-mono text-mono text-info tabular">
          {formatClock(seconds)}
        </span>
      )}
    </div>
  );
}
