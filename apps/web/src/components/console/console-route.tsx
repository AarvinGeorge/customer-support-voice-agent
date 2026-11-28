"use client";
/**
 * What: the /console client entry: reads ?session= and the display-only ?as= and ?who= hint, creates a fresh
 *   session when the tab opened without one, resets the store when the session changes, resolves
 *   the hint against the persona list, and wires New conversation to a fresh session.
 * Use when: rendered by src/app/console/page.tsx inside Suspense (useSearchParams).
 * Inputs: the URL query.
 * Output: ConsoleShell bound to the app turn store, with the Demo controls in the drawer slot.
 * Side effects: a tab without ?session= makes one POST /api/sessions and replaces the URL, keeping
 *   the hint (a 401 returns to the access page; any other failure shows a lasting error line with
 *   Try again, review PR #81 M-3); with ?as= (and ?who=) one GET /api/demo/personas; store reset on a session
 *   change (review PR #16, M5); New conversation makes one POST /api/sessions and replaces the
 *   URL without the hint (a new conversation starts signed out, review PR #81 M-2).
 * Role: route glue between the landing links (consoleStartPath, D-076) and the console shell. The
 *   hint is display-only: it is never sent to the server, and identity still comes only from the
 *   typed account id plus PIN that core verifies.
 */
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { consolePath, createSession, listPersonas, resolveStartHint } from "@/lib/api/access";
import { ACCESS_GATE_PATH, ApiError } from "@/lib/api/client";
import { turnStore } from "@/lib/turns/store";

import { Button } from "@/components/ui/button";

import { ConsoleShell, type VerifyHint } from "./console-shell";
import { DemoControls } from "./demo-controls";

/**
 * What: the ConsoleRoute component.
 * Use when: see module.
 * Inputs: none.
 * Output: JSX.
 * Side effects: see module.
 * Role: see module.
 */
export function ConsoleRoute() {
  const params = useSearchParams();
  const router = useRouter();
  const sessionId = params.get("session");
  const as = params.get("as");
  const who = params.get("who");
  const hintKey = as === null ? null : `${as}\u0000${who ?? ""}`;
  const starting = useRef(false);
  // The resolved hint is keyed on the ?as= value it was resolved for, so a changed or removed
  // ?as= or ?who= never shows an old name (review PR #81 M-2).
  const [resolved, setResolved] = useState<{ key: string; hint: VerifyHint | null } | null>(null);
  const hint = hintKey !== null && resolved?.key === hintKey ? resolved.hint : null;
  const [startError, setStartError] = useState<string | null>(null);

  useEffect(() => {
    const current = turnStore.getState().conversation.sessionId;
    if (current !== null && current !== sessionId) turnStore.getState().reset();
  }, [sessionId]);

  /**
   * What: creates a fresh session and opens it in this tab.
   * Use when: the tab opened without a session (keepHint true), Try again, and the New
   *   conversation button (keepHint false: the new conversation starts without the hint).
   * Inputs: keepHint.
   * Output: none.
   * Side effects: POST /api/sessions (empty body), store reset, URL replace; a 401 returns to the
   *   access page; any other failure sets the lasting error line.
   * Role: handler; a second call while one is starting is ignored (also covers the Strict Mode
   *   double effect, since refs survive it).
   */
  const newConversation = (keepHint: boolean) => {
    if (starting.current) return;
    starting.current = true;
    createSession()
      .then((created) => {
        turnStore.getState().reset();
        router.replace(consolePath(created.session_id, keepHint && as !== null ? { as, who } : null));
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 401) router.replace(ACCESS_GATE_PATH);
        else setStartError(err instanceof ApiError ? err.body.message : "The demo service did not answer.");
      })
      .finally(() => {
        starting.current = false;
      });
  };

  // A tab opened from the landing page has no session yet: every click starts a fresh one (D-076).
  useEffect(() => {
    if (sessionId === null) newConversation(true);
    // newConversation reads only refs, the router and `as`; run once per missing session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // The hint names a persona only when it matches one in the persona list; otherwise it is ignored.
  useEffect(() => {
    if (as === null || as === "") return;
    let ignore = false;
    listPersonas()
      .then((personas) => {
        // F13: the person comes from (account, name), never from the account alone.
        if (!ignore) setResolved({ key: hintKey as string, hint: resolveStartHint(personas, as, who) });
      })
      .catch(() => undefined);
    return () => {
      ignore = true;
    };
  }, [as, who, hintKey]);

  return (
    <ConsoleShell
      sessionId={sessionId}
      hint={hint}
      onNewConversation={() => {
        setStartError(null);
        newConversation(false);
      }}
      notice={
        startError === null ? null : (
          <div role="alert" className="mb-6 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-rule bg-raised px-4 py-3">
            <p className="text-body-sm text-ink">
              {"Couldn't start a conversation."} <span className="text-muted">{startError}</span>
            </p>
            <Button variant="quiet" size="sm" onClick={() => {
                setStartError(null);
                newConversation(true);
              }}
            >
              Try again
            </Button>
          </div>
        )
      }
      demoControls={<DemoControls store={turnStore} />}
    />
  );
}
