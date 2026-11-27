"use client";
/**
 * What: the /console client entry: reads ?session=, resets the store when the session changes,
 *   and wires New conversation to a fresh session.
 * Use when: rendered by src/app/console/page.tsx inside Suspense (useSearchParams).
 * Inputs: the URL query.
 * Output: ConsoleShell bound to the app turn store.
 * Side effects: store reset on a session change (review PR #16, M5); New conversation makes one
 *   POST /api/sessions and replaces the URL; a failure shows a toast.
 * Role: route glue between the landing flow (consolePath) and the console shell.
 */
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef } from "react";
import { toast } from "sonner";

import { consolePath, createSession } from "@/lib/api/access";
import { turnStore } from "@/lib/turns/store";

import { ConsoleShell } from "./console-shell";

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
  const starting = useRef(false);

  useEffect(() => {
    const current = turnStore.getState().conversation.sessionId;
    if (current !== null && current !== sessionId) turnStore.getState().reset();
  }, [sessionId]);

  /**
   * What: starts a new conversation (a new session) and opens it.
   * Use when: the New conversation button.
   * Inputs: none.
   * Output: none.
   * Side effects: POST /api/sessions (401 returns to the access page), store reset, navigation.
   * Role: handler; a second click while one is starting is ignored.
   */
  const newConversation = async () => {
    if (starting.current) return;
    starting.current = true;
    try {
      const created = await createSession();
      turnStore.getState().reset();
      router.replace(consolePath(created.session_id));
    } catch {
      toast.error("Couldn't start a new conversation. Try again in a moment.");
    } finally {
      starting.current = false;
    }
  };

  return <ConsoleShell sessionId={sessionId} onNewConversation={() => void newConversation()} />;
}
