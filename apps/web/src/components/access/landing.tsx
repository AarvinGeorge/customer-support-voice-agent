"use client";
/**
 * What: the landing flow: check access, show the gate or the persona list, start a session.
 * Use when: route `/` (ACCESS_GATE_PATH).
 * Inputs: none.
 * Output: one of four states: checking, gate, personas, or an unreachable-service notice.
 * Side effects: GET /api/demo/personas on mount and after a good code; POST /api/sessions on
 *   Start, then client navigation to /console?session=<id>.
 * Role: CS-E-04.S1.T1 page controller (doc 07 s2.1, s2.2, s9).
 */
import type { Persona } from "@contracts/contracts";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { consolePath, createSession, listPersonas } from "@/lib/api/access";
import { ApiError } from "@/lib/api/client";

import { AccessGate } from "./access-gate";
import { BrandMark, RecordingNotice, VoiceHint, formatWait, useCountdown } from "./parts";
import { PersonaList } from "./persona-list";

/**
 * What: which screen the landing page shows.
 * Use when: Landing state.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: state union.
 */
type View = { kind: "checking" } | { kind: "gate" } | { kind: "personas"; personas: Persona[] } | { kind: "unreachable" };

/**
 * What: the landing component.
 * Use when: rendered by app/page.tsx.
 * Inputs: none.
 * Output: JSX for the current view.
 * Side effects: see module.
 * Role: page controller.
 */
export function Landing() {
  const router = useRouter();
  const [view, setView] = useState<View>({ kind: "checking" });
  const [startingIndex, setStartingIndex] = useState<number | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const startCountdown = useCountdown();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const focusHeading = useRef(false);
  const starting = useRef(false);
  const lastRemaining = useRef(0);

  /**
   * What: loads the personas; a 401 means the visitor still needs the gate.
   * Use when: on mount and after onGranted.
   * Inputs: none.
   * Output: none.
   * Side effects: network request; sets view.
   * Role: access probe and data load in one call.
   */
  const load = useCallback(async () => {
    try {
      const personas = await listPersonas();
      setView({ kind: "personas", personas });
    } catch (err) {
      setView(err instanceof ApiError && err.status === 401 ? { kind: "gate" } : { kind: "unreachable" });
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional state sync or reset in an effect; refactor deferred (no behaviour change in the lint pass)
    void load();
  }, [load]);

  // After a granted code the gate unmounts; move focus to the list heading so keyboard and
  // screen-reader users land on the new content (review PR #8 I-2). Not on a cold load.
  useEffect(() => {
    if (view.kind === "personas" && focusHeading.current) {
      focusHeading.current = false;
      headingRef.current?.focus();
    }
  }, [view.kind]);

  // When a Start rate-limit countdown ends, clear its message (review PR #8 M-2).
  useEffect(() => {
    if (lastRemaining.current > 0 && startCountdown.remaining === 0) setStartError(null);
    lastRemaining.current = startCountdown.remaining;
  }, [startCountdown.remaining]);

  /**
   * What: creates a session and opens the console.
   * Use when: a Start button is pressed.
   * Inputs: the persona row index (only used to show "Starting" on that row).
   * Output: none.
   * Side effects: POST /api/sessions; navigation; on 401 returns to the gate; on 429 a countdown.
   * Role: Start handler.
   */
  async function start(index: number) {
    if (starting.current) return;
    starting.current = true;
    setStartingIndex(index);
    setStartError(null);
    try {
      const created = await createSession();
      router.push(consolePath(created.session_id));
    } catch (err) {
      starting.current = false;
      setStartingIndex(null);
      if (err instanceof ApiError && err.status === 401) {
        setView({ kind: "gate" });
      } else if (err instanceof ApiError && err.status === 429) {
        setStartError(err.body.message);
        startCountdown.start(err.retryAfterS ?? 60);
      } else {
        setStartError(err instanceof ApiError ? err.body.message : "The demo service did not answer. Try again.");
      }
    }
  }

  if (view.kind === "checking") {
    return (
      <main className="grid min-h-dvh place-items-center" aria-busy="true">
        <span role="status" className="text-body-sm text-muted">
          Checking access
        </span>
      </main>
    );
  }

  if (view.kind === "gate") {
    return (
      <AccessGate
        onGranted={() => {
          focusHeading.current = true;
          void load();
        }}
      />
    );
  }

  if (view.kind === "unreachable") {
    return (
      <main className="mx-auto flex min-h-dvh max-w-[25rem] flex-col justify-center gap-4 px-4">
        <BrandMark />
        <p role="alert" className="text-body text-ink">
          The demo service did not answer. Wait a moment and try again.
        </p>
        <Button variant="quiet" className="self-start" onClick={() => void load()}>
          Try again
        </Button>
      </main>
    );
  }

  const limited = startCountdown.remaining > 0;
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="flex h-16 items-center justify-between border-b border-rule px-4 md:h-[129px] md:px-6">
        <BrandMark />
        <div className="flex items-center gap-3">
          <span className="text-caption text-muted">Demo access granted</span>
          <ThemeToggle />
        </div>
      </header>
      <main className="mx-auto flex w-full max-w-[60rem] flex-col px-4 pt-10 pb-16 md:px-0 md:pt-14">
        <div className="flex max-w-[45rem] flex-col gap-2">
          <h1 ref={headingRef} tabIndex={-1} className="text-title text-ink outline-none">
            Choose who you are
          </h1>
          <p className="text-body text-muted">
            Each persona is a seeded Quillstack customer. When the agent asks you to verify, use that persona&apos;s
            account ID and support PIN. PINs are shown here on purpose; they are demo data.
          </p>
        </div>
        <div className="mt-7">
          <PersonaList
            personas={view.personas}
            onStart={(i) => void start(i)}
            startingIndex={startingIndex}
            startDisabled={limited}
          />
        </div>
        {startError && (
          <p role="alert" className={limited ? "mt-4 text-body-sm text-info" : "mt-4 text-body-sm text-error"}>
            {startError}
            {limited ? ` Try again in ${formatWait(startCountdown.remaining)}.` : ""}
          </p>
        )}
        <div className="mt-7 flex flex-col gap-7">
          <VoiceHint />
          <RecordingNotice className="max-w-[45rem]" />
        </div>
      </main>
    </div>
  );
}
