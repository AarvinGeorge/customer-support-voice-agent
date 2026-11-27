"use client";
/**
 * What: the console shell: top bar (brand, verified identity chip, New conversation, Inspector
 *   placeholder, theme toggle), the conversation log and the input bar, bound to a turn store.
 * Use when: the /console route and the dev-only fixture replay route.
 * Inputs: { sessionId, store?, onNewConversation? }.
 * Output: the full-height console frame.
 * Side effects: sending a message or pressing a card calls store.sendTurn (one network request);
 *   an in-flight turn is aborted on unmount (its turn ends in the failed state, review PR #16 I1).
 * Role: CS-E-04.S5.T1 shell, laid out from the Figma page "Console, chat (S1)".
 */
import { MotionConfig } from "motion/react";
import { PanelRight, Plus, Shield, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef } from "react";
import { useStore, type StoreApi } from "zustand";
import type { ConfirmInput, TurnRequest, VerificationInput, VerifiedPerson } from "@contracts/contracts";

import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { turnStore, type TurnStoreState } from "@/lib/turns/store";

import { ConversationLog, type CardActions } from "./conversation-log";
import { InputBar } from "./input-bar";

/**
 * What: words for a verified person's role.
 * Use when: the identity chip.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table.
 */
const ROLE_WORDS: Readonly<Record<VerifiedPerson["role"], string>> = Object.freeze({
  owner: "Owner",
  billing_admin: "Billing admin",
  member: "Member",
});

/**
 * What: words for a plan tier.
 * Use when: the identity chip.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table.
 */
const TIER_WORDS: Readonly<Record<VerifiedPerson["tier"], string>> = Object.freeze({
  free: "Free",
  pro: "Pro",
  business: "Business",
  enterprise: "Enterprise",
});

/**
 * What: the identity chip: "Verified as {name}" with role and plan, or "Not verified".
 * Use when: the top bar.
 * Inputs: { person } from conversation.verified.
 * Output: a pill.
 * Side effects: none.
 * Role: session identity shown in words (DESIGN.md chips).
 */
export function IdentityChip({ person }: { person: VerifiedPerson | null }) {
  if (person === null) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-rule bg-raised px-2.5 py-1 text-label-sm text-muted">
        <Shield aria-hidden className="size-3.5" />
        <span>Not verified</span>
      </span>
    );
  }
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5 rounded-full border border-rule bg-raised px-2.5 py-1 text-label-sm">
      <ShieldCheck aria-hidden className="size-3.5 shrink-0 text-success" />
      <span className="truncate text-ink">Verified as {person.display_name}</span>
      <span className="shrink-0 text-muted max-sm:hidden">
        {ROLE_WORDS[person.role]} · {TIER_WORDS[person.tier]}
      </span>
    </span>
  );
}

/**
 * What: props of ConsoleShell.
 * Use when: typing the component.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: `store` defaults to the app singleton; tests and the replay route pass their own.
 */
export interface ConsoleShellProps {
  sessionId: string | null;
  store?: StoreApi<TurnStoreState>;
  onNewConversation?: () => void;
}

/**
 * What: the ConsoleShell component.
 * Use when: see module.
 * Inputs: ConsoleShellProps.
 * Output: JSX.
 * Side effects: see module.
 * Role: see module.
 */
export function ConsoleShell({ sessionId, store = turnStore, onNewConversation }: ConsoleShellProps) {
  const thread = useStore(store, (s) => s.thread);
  const conversation = useStore(store, (s) => s.conversation);
  const person = useStore(store, (s) => s.conversation.verified);
  const busy = useStore(store, (s) => s.connection.phase === "streaming");
  const inflight = useRef<AbortController | null>(null);

  useEffect(() => () => inflight.current?.abort(), []);

  /**
   * What: sends one turn: a chat message, or a card's verification or confirm body.
   * Use when: the input bar submits, or a card is pressed.
   * Inputs: the TurnRequest without client_turn_id (exactly one of text, verification, confirm).
   * Output: a promise that settles when the turn ends (resolves on success and on failure).
   * Side effects: one streamed request with a fresh client_turn_id; failures land on the turn
   *   (failed phase) or in connection.error (shown by CS-E-04.S5.T3), so the rejection is
   *   swallowed here. Without a session nothing is sent.
   * Role: handler. The request object is not kept anywhere after the call, so a card PIN lives
   *   only for the request (doc 07 s11).
   */
  const run = useCallback(
    (part: Omit<TurnRequest, "client_turn_id">): Promise<void> => {
      if (sessionId === null) return Promise.resolve();
      const ctrl = new AbortController();
      inflight.current = ctrl;
      return store
        .getState()
        .sendTurn(sessionId, { client_turn_id: crypto.randomUUID(), ...part }, { signal: ctrl.signal })
        .then(() => undefined)
        .catch(() => undefined)
        .finally(() => {
          if (inflight.current === ctrl) inflight.current = null;
        });
    },
    [sessionId, store],
  );

  /**
   * What: sends one chat message.
   * Use when: the input bar submits.
   * Inputs: the text.
   * Output: none.
   * Side effects: see run.
   * Role: handler.
   */
  const send = useCallback((text: string) => void run({ text }), [run]);

  const cards: CardActions = useMemo(
    () => ({
      disabled: busy,
      verify: (verification: VerificationInput) => run({ verification }),
      decide: (confirm: ConfirmInput) => run({ confirm }),
    }),
    [busy, run],
  );

  return (
    <MotionConfig reducedMotion="user">
      <div className="flex min-h-dvh flex-col bg-paper">
        <header className="sticky top-0 z-10 flex items-center gap-4 border-b border-rule bg-paper px-6 py-3.5 max-sm:gap-2 max-sm:px-4">
          <span className="flex shrink-0 items-center gap-1.5">
            <span aria-hidden className="size-[18px] rounded-[5px] bg-ink" />
            <span className="text-heading text-ink">Quillstack</span>
            <span className="text-heading text-muted max-sm:hidden">Support</span>
          </span>
          <span className="flex-1" />
          <IdentityChip person={person} />
          <Button variant="quiet" onClick={onNewConversation} aria-label="New conversation">
            <Plus aria-hidden className="size-3.5" />
            <span className="max-sm:hidden">New conversation</span>
          </Button>
          <Button variant="quiet" disabled title="The Inspector arrives with the observability drawer" className="max-sm:hidden">
            <PanelRight aria-hidden className="size-3.5" />
            Inspector
          </Button>
          <ThemeToggle className="size-[34px] rounded-full border-rule bg-raised text-ink" />
        </header>
        <main className="mx-auto flex w-full max-w-[45rem] flex-1 flex-col px-4 pt-10 pb-6 sm:px-0">
          <div className="flex flex-1 flex-col">
            <ConversationLog thread={thread} conversation={conversation} cards={cards} />
          </div>
          <div className="sticky bottom-0 bg-paper pt-7 pb-[max(1rem,env(safe-area-inset-bottom))]">
            <InputBar onSend={send} busy={busy} disabled={sessionId === null} />
          </div>
        </main>
      </div>
    </MotionConfig>
  );
}
