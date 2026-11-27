"use client";
/**
 * What: small shared pieces of the landing page: brand mark, recording notice, voice hint,
 *   and the countdown hook used by the 429 states.
 * Use when: the access gate and the persona list render.
 * Inputs: see each export.
 * Output: JSX pieces and a hook.
 * Side effects: useCountdown runs a one-second interval while a countdown is active.
 * Role: landing page building blocks (Figma page "Landing").
 */
import { CircleDot, Mic } from "lucide-react";
import { useEffect, useState } from "react";

/**
 * What: the "Quillstack Support" wordmark with the ink square.
 * Use when: the top of the gate and the persona page header.
 * Inputs: none.
 * Output: JSX.
 * Side effects: none.
 * Role: brand mark.
 */
export function BrandMark() {
  return (
    <span className="inline-flex items-center gap-1.5 text-heading">
      <span aria-hidden className="mr-0.5 size-[18px] rounded-[5px] bg-ink" />
      <span className="text-ink">Quillstack</span>
      <span className="text-muted">Support</span>
    </span>
  );
}

/**
 * What: the notice that the demo records conversations (L1 s12, Figma "Recording notice").
 * Use when: on the gate and at the end of the persona list.
 * Inputs: { className }.
 * Output: JSX paragraph with an icon.
 * Side effects: none.
 * Role: consent notice shown before any conversation starts.
 */
export function RecordingNotice({ className = "" }: { className?: string }) {
  return (
    <p className={`flex gap-2.5 text-caption text-muted ${className}`}>
      <CircleDot aria-hidden className="mt-px size-4 shrink-0" />
      <span>
        This demo records conversations, including voice transcripts, to evaluate the agent. The personas are fictional;
        please do not enter real personal or payment details.
      </span>
    </p>
  );
}

/**
 * What: the microphone hint under the persona list.
 * Use when: the persona list renders.
 * Inputs: none.
 * Output: JSX paragraph with an icon.
 * Side effects: none.
 * Role: sets expectations for voice before the console opens.
 */
export function VoiceHint() {
  return (
    <p className="flex gap-2.5 text-body-sm text-muted">
      <Mic aria-hidden className="mt-0.5 size-3.5 shrink-0" />
      <span>Voice needs microphone access and works best with headphones. Every persona can switch between chat and voice.</span>
    </p>
  );
}

/**
 * What: a whole-second countdown toward a deadline.
 * Use when: a 429 says how long to wait (ApiError.retryAfterS).
 * Inputs: none; call start(seconds) to begin, and start(null) to clear.
 * Output: { remaining: seconds left or 0, start }.
 * Side effects: a one-second interval while remaining is above 0; cleared on finish and unmount.
 * Role: shared by the gate (ACCESS_ATTEMPTS) and Start (IP_SESSIONS).
 */
export function useCountdown(): { remaining: number; start: (seconds: number | null) => void } {
  const [until, setUntil] = useState<number | null>(null);
  const [remaining, setRemaining] = useState(0);

  useEffect(() => {
    if (until === null) {
      setRemaining(0);
      return;
    }
    const tick = () => {
      const left = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      setRemaining(left);
      if (left === 0) setUntil(null);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [until]);

  const start = (seconds: number | null) => setUntil(seconds === null ? null : Date.now() + seconds * 1000);
  return { remaining, start };
}

/**
 * What: a wait in words: seconds up to a minute, then whole minutes rounded up.
 * Use when: showing a 429 countdown (ACCESS_ATTEMPTS can be close to an hour).
 * Inputs: seconds (whole, 1 or more).
 * Output: "42 s" or "59 min".
 * Side effects: none.
 * Role: formatter (review PR #8 M-3).
 */
export function formatWait(seconds: number): string {
  return seconds > 60 ? `${Math.ceil(seconds / 60)} min` : `${seconds} s`;
}
