/**
 * What: the Zustand store for the console conversation: reducer state, the unified thread (user
 *   items and turns, chat and voice), connection state, the one-shot demo fault, and sendTurn
 *   (streamTurn wired into the reducer; a turn that ends without turn.completed is failed).
 * Use when: React components read conversation state (useTurnStore) or send a turn.
 * Inputs: SseEvents via apply, TurnRequests via sendTurn, the fault toggle via armFault.
 * Output: store state and actions.
 * Side effects: sendTurn makes one network request; apply reads the clock (the receipt time
 *   of a confirmation card); nothing is persisted.
 * Role: the only stateful wrapper around the pure reducer, for chat turns and voice relay turns
 *   (sendVoiceAsk), so the thread is one (D-002).
 */
import { createStore, useStore, type StoreApi } from "zustand";
import type { SseEvent, TurnRequest, VoiceAskRequest } from "@contracts/contracts";
import { ApiError, NetworkError, type ApiDeps, type ContractViolationError } from "@/lib/api/client";
import { streamSse, streamTurn, type DemoFault, type StreamOptions, type StreamResult } from "@/lib/api/stream";
import { failTurn, failureMessage, initialConversationState, reduceEvent, type ConversationState } from "./reducer";

/**
 * What: the typed failure of the last sendTurn.
 * Use when: showing a banner, countdown or the drop message.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: union of the transport's rejections.
 */
export type TurnError = ApiError | ContractViolationError | NetworkError;

/**
 * What: one entry of the conversation thread: something the customer said (typed in chat or a
 *   final voice transcript) or one agent turn (rendered from conversation.turns).
 * Use when: rendering the conversation log in order.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view model; one thread for chat and voice (doc 06 s16, D-002). `id` is a stable React key.
 */
export type ThreadItem =
  | { kind: "user"; id: string; text: string; mode: "chat" | "voice" }
  | {
      /** What the voice agent said aloud, keyed by its Realtime item id (CS-E-05.S4.T3). */
      kind: "agent-voice";
      id: string;
      itemId: string;
      /** The spoken transcript received so far (the whole utterance once done). */
      text: string;
      /** What the customer heard: `text` unless interrupted, then cut where the audio stopped. */
      heard: string;
      interrupted: boolean;
      /** The voice/ask turn this utterance read back (its `say`), or null for other speech. */
      turnId: string | null;
    }
  | { kind: "turn"; id: string; turnId: string }
  | {
      kind: "error";
      id: string;
      /** The typed text to send again on Retry (null for card inputs, which are never kept: PIN). */
      text: string | null;
      /** The user item this send added, removed on Retry so the bubble is not repeated. */
      userItemId: string | null;
      message: string;
      retryable: boolean;
    };

/**
 * What: the error row words for a send that failed before its first event.
 * Use when: sendTurn's catch saw no event.
 * Inputs: the rejection.
 * Output: { message, retryable }.
 * Side effects: none.
 * Role: ApiError uses the server's envelope message and retryable flag (doc 07 s1.3) plus the
 *   wait in seconds; network and stream failures use the same words as a failed turn (doc 07 s8.1:
 *   the browser never retries by itself, the customer may).
 */
function preTurnError(err: unknown): { message: string; retryable: boolean } {
  if (err instanceof ApiError) {
    const wait = err.retryAfterS !== null && err.retryAfterS > 0 ? ` Try again in ${err.retryAfterS} s.` : "";
    return { message: `${err.body.message}${wait}`, retryable: err.body.retryable };
  }
  if (err instanceof NetworkError) return { message: failureMessage(err.reason), retryable: true };
  return { message: failureMessage("contract"), retryable: true };
}

/**
 * What: per-call options for sendTurn.
 * Use when: aborting a turn, or tests injecting fetch and watchdog windows.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: pass-through subset of StreamOptions.
 */
export interface SendTurnOptions {
  signal?: AbortSignal;
  deps?: ApiDeps;
  noBytesMs?: number;
  turnMs?: number;
}

/**
 * What: the store's state and actions.
 * Use when: typing selectors.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: store shape.
 */
export interface TurnStoreState {
  conversation: ConversationState;
  /** The conversation in display order; turn items are appended when a turn's first event lands. */
  thread: ThreadItem[];
  connection: { phase: "idle" | "streaming"; error: TurnError | null };
  /** The fault the drawer toggle armed for the next turn request, or null (doc 07 s10). */
  armedFault: DemoFault | null;
  apply: (event: SseEvent) => void;
  /** Adds a user bubble; a voice final passes its Realtime item id, so a repeat is ignored. */
  addUserMessage: (text: string, mode: "chat" | "voice", itemId?: string) => void;
  /** Adds or replaces the assistant voice item with this item id (same place in the thread). */
  upsertAgentVoice: (item: Omit<Extract<ThreadItem, { kind: "agent-voice" }>, "kind" | "id">) => void;
  reset: () => void;
  retry: (errorId: string, sessionId: string, opts?: SendTurnOptions) => Promise<StreamResult | null>;
  armFault: (fault: DemoFault | null) => void;
  takeFault: () => DemoFault | null;
  sendTurn: (sessionId: string, request: TurnRequest, opts?: SendTurnOptions) => Promise<StreamResult>;
  /** Posts one voice relay turn to voice/ask; same reducer, failure handling and thread as sendTurn. */
  sendVoiceAsk: (sessionId: string, request: VoiceAskRequest, opts?: SendTurnOptions) => Promise<StreamResult>;
}

/**
 * What: creates an independent turn store.
 * Use when: tests (one store per test) and the app singleton below.
 * Inputs: none.
 * Output: a vanilla Zustand StoreApi.
 * Side effects: none until sendTurn is called.
 * Role: store factory. sendTurn takes the armed fault before the request (so it goes on exactly
 *   one request, even one that fails), adds the typed text to the thread, streams events into
 *   apply (only events of its own session), and on a rejection fails every turn it saw that did
 *   not complete (failTurn) or, when no event arrived, adds an error item; it records the typed
 *   error and rethrows it; it never retries by itself (doc 07 s9). reset() aborts every in-flight
 *   send and bumps an epoch, so a late event or rejection from before the reset changes nothing
 *   (review PR #20, I1).
 */
export function createTurnStore(): StoreApi<TurnStoreState> {
  let userSeq = 0;
  let epoch = 0;
  const inflight = new Set<AbortController>();
  /**
   * What: a new user thread item with a per-store unique id.
   * Use when: addUserMessage or sendTurn with text.
   * Inputs: the text, the mode.
   * Output: a ThreadItem of kind "user".
   * Side effects: advances the store's user counter.
   * Role: helper.
   */
  const userItem = (text: string, mode: "chat" | "voice"): ThreadItem => {
    userSeq += 1;
    return { kind: "user", id: `user-${userSeq}`, text, mode };
  };
  /**
   * What: streams one turn into the store and applies the shared failure handling.
   * Use when: sendTurn (chat) and sendVoiceAsk (voice relay).
   * Inputs: session id; start (opens the stream with the given StreamOptions); text (the typed
   *   text: added to the thread and kept for Retry, or null); chat (true takes the armed fault;
   *   voice relay turns never carry it); the caller's options.
   * Output: the StreamResult, or the transport's typed rejection (rethrown).
   * Side effects: see createTurnStore's Role; network through start.
   * Role: one copy of the failure rules (failTurn on every seen turn, else an error item), so a
   *   voice turn fails exactly like a chat turn (CS-E-05.S3.T3).
   */
  const runStream = async (
    sessionId: string,
    start: (o: StreamOptions) => Promise<StreamResult>,
    text: string | null,
    chat: boolean,
    opts: SendTurnOptions,
  ): Promise<StreamResult> => {
    const get = api.getState;
    const set = api.setState;
    const fault = chat ? get().takeFault() : null;
    const mine = epoch;
    const ctrl = new AbortController();
    const outer = opts.signal;
    if (outer?.aborted) ctrl.abort();
    else outer?.addEventListener("abort", () => ctrl.abort(), { once: true });
    inflight.add(ctrl);
    const user = text ? userItem(text, "chat") : null;
    set({ thread: user ? [...get().thread, user] : get().thread, connection: { phase: "streaming", error: null } });
    const seen = new Set<string>();
    try {
      const result = await start({
        ...opts,
        signal: ctrl.signal,
        fault,
        onEvent: (e) => {
          if (epoch !== mine || e.session_id !== sessionId) return;
          seen.add(e.turn_id);
          get().apply(e);
        },
      });
      if (epoch === mine) set({ connection: { phase: "idle", error: null } });
      return result;
    } catch (err) {
      if (epoch !== mine) throw err;
      if (err instanceof NetworkError && err.turnId !== null && get().conversation.turns[err.turnId]) seen.add(err.turnId);
      const reason = err instanceof NetworkError ? err.reason : "contract";
      let conversation = get().conversation;
      for (const turnId of seen) conversation = failTurn(conversation, turnId, reason);
      let thread = get().thread;
      if (seen.size === 0 && reason !== "aborted") {
        const { message, retryable } = preTurnError(err);
        userSeq += 1;
        thread = [...thread, { kind: "error", id: `error-${userSeq}`, text, userItemId: user?.id ?? null, message, retryable }];
      }
      set({ conversation, thread, connection: { phase: "idle", error: err as TurnError } });
      throw err;
    } finally {
      inflight.delete(ctrl);
    }
  };
  const api = createStore<TurnStoreState>()((set, get) => ({
    conversation: initialConversationState,
    thread: [],
    connection: { phase: "idle", error: null },
    armedFault: null,
    apply: (event) => {
      const prev = get().conversation;
      const next = reduceEvent(prev, event, Date.now());
      if (next === prev) return;
      const added = next.turnOrder.slice(prev.turnOrder.length).map((turnId): ThreadItem => ({ kind: "turn", id: turnId, turnId }));
      set(added.length > 0 ? { conversation: next, thread: [...get().thread, ...added] } : { conversation: next });
    },
    addUserMessage: (text, mode, itemId) => {
      if (itemId === undefined) {
        set({ thread: [...get().thread, userItem(text, mode)] });
        return;
      }
      const id = `voice-${itemId}`;
      if (get().thread.some((i) => i.id === id)) return;
      set({ thread: [...get().thread, { kind: "user", id, text, mode }] });
    },
    upsertAgentVoice: (item) => {
      const id = `voice-${item.itemId}`;
      const next: ThreadItem = { kind: "agent-voice", id, ...item };
      const thread = get().thread;
      const at = thread.findIndex((i) => i.id === id);
      set({ thread: at < 0 ? [...thread, next] : thread.map((i, n) => (n === at ? next : i)) });
    },
    reset: () => {
      epoch += 1;
      for (const ctrl of inflight) ctrl.abort();
      inflight.clear();
      set({ conversation: initialConversationState, thread: [], connection: { phase: "idle", error: null }, armedFault: null });
    },
    retry: async (errorId, sessionId, opts = {}) => {
      const item = get().thread.find((i) => i.id === errorId);
      if (item?.kind !== "error" || item.text === null || !item.retryable) return null;
      set({ thread: get().thread.filter((i) => i.id !== errorId && i.id !== item.userItemId) });
      return get().sendTurn(sessionId, { client_turn_id: crypto.randomUUID(), text: item.text }, opts);
    },
    armFault: (fault) => set({ armedFault: fault }),
    takeFault: () => {
      const fault = get().armedFault;
      if (fault !== null) set({ armedFault: null });
      return fault;
    },
    sendTurn: (sessionId, request, opts = {}) =>
      runStream(sessionId, (o) => streamTurn(sessionId, request, o), request.text ?? null, true, opts),
    sendVoiceAsk: (sessionId, request, opts = {}) =>
      runStream(sessionId, (o) => streamSse(`/api/sessions/${encodeURIComponent(sessionId)}/voice/ask`, request, o), null, false, opts),
  }));
  return api;
}

/**
 * What: the app's single turn store.
 * Use when: the console page and its components.
 * Inputs: none.
 * Output: the StoreApi.
 * Side effects: none at import.
 * Role: singleton.
 */
export const turnStore = createTurnStore();

/**
 * What: React hook that selects from the app turn store.
 * Use when: a client component needs conversation or connection state.
 * Inputs: a selector.
 * Output: the selected slice; re-renders when it changes.
 * Side effects: subscribes the component to the store.
 * Role: React binding.
 */
export function useTurnStore<T>(selector: (state: TurnStoreState) => T): T {
  return useStore(turnStore, selector);
}
