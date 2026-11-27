"use client";
/**
 * What: the landing flow: check access, then show the gate or the two journeys (D-076): one
 *   "Start a conversation" link (signed out) and the persona reference sheet with a Start link per
 *   row (signed in, display-only hint).
 * Use when: route `/` (ACCESS_GATE_PATH).
 * Inputs: none.
 * Output: one of four states: checking, gate, personas, or an unreachable-service notice.
 * Side effects: GET /api/demo/personas on mount and after a good code. Start links open /console
 *   in a new tab; the console there creates the fresh session, so this page creates none.
 * Role: CS-E-04.S1.T1 page controller (doc 07 s2.1, s2.2, s9).
 */
import type { Persona } from "@contracts/contracts";
import { ArrowUpRight } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import { ThemeToggle } from "@/components/theme-toggle";
import { Button, buttonVariants } from "@/components/ui/button";
import { consoleStartPath, listPersonas } from "@/lib/api/access";
import { ApiError } from "@/lib/api/client";

import { AccessGate } from "./access-gate";
import { BrandMark, RecordingNotice, VoiceHint } from "./parts";
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
 * What: asks the agent for the personas and turns the outcome into a View; a 401 means the gate.
 * Use when: on mount and after a granted code.
 * Inputs: none.
 * Output: a Promise of the View (never rejects).
 * Side effects: network request.
 * Role: data load shared by the mount effect and onGranted.
 */
async function resolveView(): Promise<View> {
  try {
    return { kind: "personas", personas: await listPersonas() };
  } catch (err) {
    return err instanceof ApiError && err.status === 401 ? { kind: "gate" } : { kind: "unreachable" };
  }
}

/**
 * What: the landing component.
 * Use when: rendered by app/page.tsx.
 * Inputs: none.
 * Output: JSX for the current view.
 * Side effects: see module.
 * Role: page controller.
 */
export function Landing() {
  const [view, setView] = useState<View>({ kind: "checking" });
  const headingRef = useRef<HTMLHeadingElement>(null);
  const focusHeading = useRef(false);
  const startHelpId = useId();

  /**
   * What: loads the personas; a 401 means the visitor still needs the gate.
   * Use when: on mount and after onGranted.
   * Inputs: none.
   * Output: none.
   * Side effects: network request; sets view.
   * Role: access probe and data load in one call.
   */
  const load = useCallback(async () => {
    setView(await resolveView());
  }, []);

  // Fetching on mount belongs in an effect; the ignore flag drops a stale answer (react.dev,
  // "You Might Not Need an Effect": Fetching data).
  useEffect(() => {
    let ignore = false;
    void resolveView().then((next) => {
      if (!ignore) setView(next);
    });
    return () => {
      ignore = true;
    };
  }, []);

  // After a granted code the gate unmounts; move focus to the list heading so keyboard and
  // screen-reader users land on the new content (review PR #8 I-2). Not on a cold load.
  useEffect(() => {
    if (view.kind === "personas" && focusHeading.current) {
      focusHeading.current = false;
      headingRef.current?.focus();
    }
  }, [view.kind]);

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
            Each persona is a seeded Quillstack customer. Start as one (in a new tab) to arrive signed in: the verification card
            names them and fills in the account ID. Or start a conversation signed out and verify with any account ID
            and support PIN below. PINs are shown here on purpose; they are demo data.
          </p>
        </div>
        <div className="mt-6 flex flex-wrap items-center gap-x-4 gap-y-2">
          <a
            href={consoleStartPath()}
            target="_blank"
            rel="noopener"
            aria-describedby={startHelpId}
            className={buttonVariants({ variant: "ink", className: "h-10" })}
          >
            Start a conversation
            <span className="sr-only"> (opens in a new tab)</span>
            <ArrowUpRight aria-hidden data-icon="new-tab" className="size-3.5" />
          </a>
          <span id={startHelpId} className="text-body-sm text-muted">
            Opens in a new tab, signed out. Each start is a new conversation.
          </span>
        </div>
        <div className="mt-7">
          <PersonaList personas={view.personas} />
        </div>
        <div className="mt-7 flex flex-col gap-7">
          <VoiceHint />
          <RecordingNotice className="max-w-[45rem]" />
        </div>
      </main>
    </div>
  );
}
