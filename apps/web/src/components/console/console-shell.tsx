"use client";
/**
 * What: the console shell: top bar (brand, verified identity chip, New conversation, Inspector
 *   toggle, theme toggle), the conversation log, the input bar and the observability drawer,
 *   bound to a turn store.
 * Use when: the /console route and the dev-only fixture replay route.
 * Inputs: { sessionId, store?, onNewConversation? }.
 * Output: the full-height console frame.
 * Side effects: sending a message or pressing a card calls store.sendTurn (one network request);
 *   an in-flight turn is aborted on unmount (its turn ends in the failed state, review PR #16 I1).
 *   The mic button starts a voice call (useVoice); while it is open the voice dock replaces the
 *   input bar; voice turns land in the same thread.
 *   Closing the drawer returns focus to the Inspector button; ending a call returns focus to
 *   the message box (review PR #55 M4). A 429 or 503 notice shows the
 *   limit banner with a countdown, pauses Send and the cards, and keeps the draft.
 * Role: CS-E-04.S5.T1 shell, laid out from the Figma page "Console, chat (S1)"; the drawer is
 *   CS-E-06.S3.T1 (Figma "Observability drawer"); limit banner and Create ticket are
 *   CS-E-07.S1.T4 (Figma "States").
 */
import { MotionConfig } from "motion/react";
import { PanelRight, Plus, Shield, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useStore, type StoreApi } from "zustand";
import type { ConfirmInput, TurnRequest, VerificationInput, VerifiedPerson } from "@contracts/contracts";

import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { publicConfig } from "@/lib/config/public";
import type { TurnView } from "@/lib/turns/reducer";
import { turnStore, type ThreadItem, type TurnStoreState } from "@/lib/turns/store";
import { inputPlaceholder } from "@/lib/turns/furniture";
import { limitWords } from "@/lib/turns/limits";
import { trailSummary } from "@/lib/turns/trail";
import { isLive } from "@/lib/voice/machine";
import { useVoice, type VoiceController } from "@/lib/voice/use-voice";

import { ConversationLog, type CardActions } from "./conversation-log";
import { InputBar } from "./input-bar";
import { InspectorDrawer } from "./inspector-drawer";
import { LimitBanner, useSecondsLeft } from "./limit-banner";
import { VoiceDock } from "./voice-dock";

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
 * What: the sentence the console's one polite status region announces for the latest thread item:
 *   a running turn's live line, a settled turn's summary and answer, a failure, or a send error.
 * Use when: ConsoleShell renders its status region.
 * Inputs: the thread and turns.
 * Output: a string ("" when there is nothing to say).
 * Side effects: none.
 * Role: single announcer (review PR #20, M3), so trails and streamed text are not read twice.
 */
export function announcement(thread: readonly ThreadItem[], turns: Readonly<Record<string, TurnView>>): string {
  const last = thread.at(-1);
  if (last === undefined || last.kind === "user") return "";
  if (last.kind === "error") return `Couldn't send. ${last.message}`;
  if (last.kind === "agent-voice") return "";
  const turn = turns[last.turnId];
  if (turn === undefined) return "";
  const summary = trailSummary(turn);
  if (turn.phase === "failed") return `Couldn't finish. ${turn.failure?.message ?? ""}`.trim();
  if (summary.state === "running") return summary.text;
  return summary.stepCount > 0 ? `${summary.text}. ${turn.text}` : turn.text;
}

/**
 * What: the voice line above the input bar after a call: the state in words and the error words
 *   when a call failed or dropped.
 * Use when: a call has ended or failed (hidden while idle; the dock shows live calls).
 * Inputs: { state, label, caption, error }.
 * Output: a short line, or nothing.
 * Side effects: none.
 * Role: CS-E-05.S3.T3 line, kept by CS-E-05.S4.T1 for the after-call state.
 */
export function VoiceLine({ state, label, caption, error }: { state: string; label: string; caption: string | null; error: string | null }) {
  if (state === "idle") return null;
  return (
    <p data-voice-state={state} className="mb-2 flex min-w-0 items-baseline gap-2 text-caption text-muted">
      <span className="shrink-0 font-medium text-ink">{label}</span>
      {error !== null ? <span>{error}</span> : caption !== null && caption !== "" && <span className="truncate italic">{caption}</span>}
    </p>
  );
}

/**
 * What: props of ConsoleShell.
 * Use when: typing the component.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: `store` defaults to the app singleton; tests and the replay route pass their own.
 *   `langsmithProjectUrl` defaults to the public config value; `demoControls` fills the drawer's
 *   Demo controls slot (CS-E-07.S1.T4).
 */
export interface ConsoleShellProps {
  sessionId: string | null;
  store?: StoreApi<TurnStoreState>;
  onNewConversation?: () => void;
  langsmithProjectUrl?: string;
  demoControls?: ReactNode;
  /** A stand-in voice controller (tests, the dev replay); the default is useVoice for the session. */
  voice?: VoiceController;
}

/**
 * What: the ConsoleShell component.
 * Use when: see module.
 * Inputs: ConsoleShellProps.
 * Output: JSX.
 * Side effects: see module.
 * Role: see module.
 */
export function ConsoleShell({
  sessionId,
  store = turnStore,
  onNewConversation,
  langsmithProjectUrl = publicConfig.langsmithProjectUrl,
  demoControls,
  voice: voiceOverride,
}: ConsoleShellProps) {
  const thread = useStore(store, (s) => s.thread);
  const conversation = useStore(store, (s) => s.conversation);
  const person = useStore(store, (s) => s.conversation.verified);
  const busy = useStore(store, (s) => s.connection.phase === "streaming");
  const limit = useStore(store, (s) => s.limit);
  const [limitNote, setLimitNote] = useState("");
  const clearLimit = useCallback(() => {
    store.getState().clearLimit();
    setLimitNote("You can send again.");
  }, [store]);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional state sync or reset in an effect; refactor deferred (no behaviour change in the lint pass)
    if (busy) setLimitNote("");
  }, [busy]);
  const limitId = limit?.id ?? null;
  const limitS = limit?.retryAfterS ?? null;
  useEffect(() => {
    // Announce the wait once per notice, from retry_after_s (not the ticking clock).
    if (limitId === null) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional state sync or reset in an effect; refactor deferred (no behaviour change in the lint pass)
    setLimitNote(limitS !== null && limitS > 0 ? `You can send again in about ${limitS} second${limitS === 1 ? "" : "s"}.` : "Sending is paused for a moment.");
  }, [limitId]);
  const secondsLeft = useSecondsLeft(limit?.untilMs ?? null, clearLimit);
  // eslint-disable-next-line react-hooks/purity -- intentional read of the clock at render for the countdown; refactor deferred
  const locked = limit !== null && limit.untilMs !== null && limit.untilMs > Date.now() ? "Send unlocks when the countdown ends" : null;
  const inflight = useRef<AbortController | null>(null);
  const messageRef = useRef<HTMLTextAreaElement | null>(null);
  const pinRef = useRef<HTMLInputElement | null>(null);
  const [account, setAccount] = useState("");
  const ownVoice = useVoice(voiceOverride === undefined ? sessionId : null, store);
  const vc = voiceOverride ?? ownVoice;
  const { voice, toggle, available } = vc;
  const live = isLive(voice.state);
  const wasLive = useRef(false);
  useEffect(() => {
    if (wasLive.current && !live) messageRef.current?.focus();
    wasLive.current = live;
  }, [live]);
  const step = useStore(store, (s) => {
    const last = s.conversation.turnOrder.at(-1);
    const turn = last === undefined ? undefined : s.conversation.turns[last];
    if (turn === undefined) return null;
    const summary = trailSummary(turn);
    return summary.state === "running" ? summary.text : null;
  });
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [pickedTurnId, setPickedTurnId] = useState<string | null>(null);
  const inspectorRef = useRef<HTMLButtonElement | null>(null);
  const returnFocus = useRef(false);
  const drawerId = useId();

  useEffect(() => () => inflight.current?.abort(), []);
  useEffect(() => () => inflight.current?.abort(), [sessionId]);
  // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional state sync or reset in an effect; refactor deferred (no behaviour change in the lint pass)
  useEffect(() => setAccount(""), [sessionId]);
  // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional state sync or reset in an effect; refactor deferred (no behaviour change in the lint pass)
  useEffect(() => setPickedTurnId(null), [sessionId]);

  /**
   * What: closes the drawer and returns focus to the Inspector button.
   * Use when: the close button or Escape inside the drawer.
   * Inputs: none.
   * Output: none.
   * Side effects: state change; an effect then focuses the Inspector button (after the drawer
   *   unmounts, so focus never falls to the page body).
   * Role: handler (focus management).
   */
  const closeInspector = useCallback(() => {
    returnFocus.current = true;
    setInspectorOpen(false);
  }, []);
  useEffect(() => {
    if (inspectorOpen || !returnFocus.current) return;
    returnFocus.current = false;
    inspectorRef.current?.focus();
  }, [inspectorOpen]);

  /**
   * What: opens the drawer on one turn.
   * Use when: a reply's Inspect button.
   * Inputs: the turn id.
   * Output: none.
   * Side effects: state change (the drawer focuses its heading when it mounts).
   * Role: handler (per-turn selection).
   */
  const inspect = useCallback((turnId: string) => {
    setPickedTurnId(turnId);
    setInspectorOpen(true);
  }, []);

  /**
   * What: moves keyboard focus after a card press settles, so it never falls to the page body
   *   when the pressed control goes away (WCAG 2.4.3, review PR #24 I2).
   * Use when: a Verify, Confirm or Cancel turn has ended.
   * Inputs: "verify" or "decide".
   * Output: none.
   * Side effects: on the next task, focuses the PIN field of a retry card when one is showing
   *   (verify only), else the message field.
   * Role: helper.
   */
  const refocus = useCallback((after: "verify" | "decide") => {
    setTimeout(() => {
      const pin = pinRef.current;
      if (after === "verify" && pin !== null && pin.isConnected) pin.focus();
      else messageRef.current?.focus();
    }, 0);
  }, []);

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
      disabled: busy || locked !== null,
      account,
      setAccount,
      pinRef,
      verify: (verification: VerificationInput) => run({ verification }).finally(() => refocus("verify")),
      decide: (confirm: ConfirmInput) => run({ confirm }).finally(() => refocus("decide")),
      offerTicket: () => run({ ticket_offer: { accept: true } }).finally(() => refocus("decide")),
    }),
    [busy, locked, run, account, refocus],
  );

  /**
   * What: sends a failed message again from its error row.
   * Use when: the Retry button.
   * Inputs: the error item id.
   * Output: none.
   * Side effects: one streamed request (store.retry removes the error row and its bubble first).
   * Role: handler.
   */
  const retry = useCallback(
    (errorId: string) => {
      if (sessionId === null) return;
      const ctrl = new AbortController();
      inflight.current = ctrl;
      store
        .getState()
        .retry(errorId, sessionId, { signal: ctrl.signal })
        .catch(() => undefined)
        .finally(() => {
          if (inflight.current === ctrl) inflight.current = null;
        });
    },
    [sessionId, store],
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
          <Button
            ref={inspectorRef}
            variant="quiet"
            aria-expanded={inspectorOpen}
            aria-controls={inspectorOpen ? drawerId : undefined}
            onClick={() => (inspectorOpen ? closeInspector() : setInspectorOpen(true))}
            className="max-sm:hidden"
          >
            <PanelRight aria-hidden className="size-3.5" />
            {inspectorOpen ? "Hide inspector" : "Inspector"}
          </Button>
          <ThemeToggle className="size-[34px] rounded-full border-rule bg-raised text-ink" />
        </header>
        <div className="flex flex-1">
        <main className="mx-auto flex w-full min-w-0 max-w-[45rem] flex-1 flex-col px-4 pt-10 pb-6 sm:px-0">
          <div className="flex flex-1 flex-col">
            <ConversationLog
              thread={thread}
              conversation={conversation}
              cards={cards}
              onRetry={retry}
              onResend={send}
              inspect={{ onInspect: inspect, selected: inspectorOpen ? (pickedTurnId ?? conversation.turnOrder.at(-1) ?? null) : null }}
            />
            <div role="status" aria-live="polite" className="sr-only">
              {limitNote || announcement(thread, conversation.turns)}
            </div>
          </div>
          <div className="sticky bottom-0 bg-paper pt-7 pb-[max(1rem,env(safe-area-inset-bottom))]">
            {limit !== null && <LimitBanner notice={limit} seconds={secondsLeft} />}
            {live ? (
              <VoiceDock voice={voice} levels={vc.levels} step={step} agentLine={voice.agentLine} onMute={vc.mute} onStopSpeaking={vc.stopSpeaking} onEnd={vc.end} />
            ) : (
              <>
                <VoiceLine state={voice.state} label={voice.label} caption={null} error={voice.error} />
                <InputBar
                  locked={locked}
                  restore={limit?.draft ? { id: limit.id, text: limit.draft } : null}
                  caption={limit ? limitWords(limit).caption : null}
                  onSend={send}
                  busy={busy}
                  disabled={sessionId === null}
                  placeholder={inputPlaceholder(conversation, sessionId === null)}
                  inputRef={messageRef}
                  voice={available ? { live: false, onToggle: toggle } : undefined}
                />
              </>
            )}
          </div>
        </main>
        {inspectorOpen && (
          <InspectorDrawer
            id={drawerId}
            conversation={conversation}
            pickedTurnId={pickedTurnId}
            onPick={setPickedTurnId}
            onClose={closeInspector}
            langsmithProjectUrl={langsmithProjectUrl}
            langsmithProject={publicConfig.langsmithProject}
            demoControls={demoControls}
          />
        )}
        </div>
      </div>
    </MotionConfig>
  );
}
