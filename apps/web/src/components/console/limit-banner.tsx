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
import { useEffect, useState } from "react";

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
  const left = (now: number) => (untilMs === null ? 0 : Math.max(0, Math.ceil((untilMs - now) / 1000)));
  const [seconds, setSeconds] = useState(() => left(Date.now()));
  useEffect(() => {
    if (untilMs === null) {
      setSeconds(0);
      return;
    }
    const tick = () => {
      const s = left(Date.now());
      setSeconds(s);
      if (s === 0) {
        clearInterval(id);
        onDone();
      }
    };
    const id = setInterval(tick, 1000);
    tick();
    return () => clearInterval(id);
  }, [untilMs]);
  return seconds;
}

/**
 * What: the banner.
 * Use when: a notice is present.
 * Inputs: { notice, seconds } (seconds from useSecondsLeft).
 * Output: a labelled status region.
 * Side effects: none.
 * Role: the sentence is announced once (polite); the ticking clock is aria-hidden, its value
 *   is read with the label instead of every second.
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
