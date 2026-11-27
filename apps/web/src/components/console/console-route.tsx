"use client";
/**
 * What: the /console client entry: reads ?session= and the display-only ?as= hint, creates a fresh
 *   session when the tab opened without one, resets the store when the session changes, resolves
 *   the hint against the persona list, and wires New conversation to a fresh session.
 * Use when: rendered by src/app/console/page.tsx inside Suspense (useSearchParams).
 * Inputs: the URL query.
 * Output: ConsoleShell bound to the app turn store, with the Demo controls in the drawer slot.
 * Side effects: a tab without ?session= makes one POST /api/sessions and replaces the URL (a 401
 *   returns to the access page, other failures show a toast); with ?as= one GET /api/demo/personas;
 *   store reset on a session change (review PR #16, M5); New conversation makes one
 *   POST /api/sessions and replaces the URL; a failure shows a toast.
 * Role: route glue between the landing links (consoleStartPath, D-076) and the console shell. The
 *   hint is display-only: it is never sent to the server, and identity still comes only from the
 *   typed account id plus PIN that core verifies.
 */
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { consolePath, createSession, listPersonas } from "@/lib/api/access";
import { ACCESS_GATE_PATH, ApiError } from "@/lib/api/client";
import { turnStore } from "@/lib/turns/store";

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
  const starting = useRef(false);
  const [hint, setHint] = useState<VerifyHint | null>(null);

  useEffect(() => {
    const current = turnStore.getState().conversation.sessionId;
    if (current !== null && current !== sessionId) turnStore.getState().reset();
  }, [sessionId]);

  /**
   * What: creates a fresh session and opens it in this tab, keeping the hint in the URL.
   * Use when: the tab opened without a session, and the New conversation button.
   * Inputs: none.
   * Output: none.
   * Side effects: POST /api/sessions (empty body), store reset, URL replace; a 401 returns to the
   *   access page; any other failure shows a toast.
   * Role: handler; a second call while one is starting is ignored (also covers the Strict Mode
   *   double effect, since refs survive it).
   */
  const newConversation = async () => {
    if (starting.current) return;
    starting.current = true;
    try {
      const created = await createSession();
      turnStore.getState().reset();
      router.replace(consolePath(created.session_id, as));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) router.replace(ACCESS_GATE_PATH);
      else toast.error(err instanceof ApiError ? err.body.message : "Couldn't start a new conversation. Try again in a moment.");
    } finally {
      starting.current = false;
    }
  };

  // A tab opened from the landing page has no session yet: every click starts a fresh one (D-076).
  useEffect(() => {
    if (sessionId === null) void newConversation();
    // newConversation reads only refs, the router and `as`; run once per missing session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // The hint names a persona only when it matches one in the persona list; otherwise it is ignored.
  useEffect(() => {
    if (as === null || as === "") return;
    let ignore = false;
    listPersonas()
      .then((personas) => {
        const p = personas.find((x) => x.account_id === as);
        if (!ignore && p !== undefined) setHint({ name: p.name, accountId: p.account_id });
      })
      .catch(() => undefined);
    return () => {
      ignore = true;
    };
  }, [as]);

  return (
    <ConsoleShell
      sessionId={sessionId}
      hint={as === null || as === "" ? null : hint}
      onNewConversation={() => void newConversation()}
      demoControls={<DemoControls store={turnStore} />}
    />
  );
}
