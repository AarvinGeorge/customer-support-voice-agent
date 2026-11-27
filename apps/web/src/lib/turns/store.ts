/**
 * What: the Zustand store for the console conversation: reducer state, the unified thread (user
 *   items and turns, chat and voice), connection state, the one-shot demo fault, and sendTurn
 *   (streamTurn wired into the reducer; a turn that ends without turn.completed is failed).
 * Use when: React components read conversation state (useTurnStore) or send a turn.
 * Inputs: SseEvents via apply, TurnRequests via sendTurn, the fault toggle via armFault.
 * Output: store state and actions.
 * Side effects: sendTurn makes one network request; nothing is persisted.
 * Role: the only stateful wrapper around the pure reducer; chat now, the voice relay later.
 */
import { createStore, useStore, type StoreApi } from "zustand";
import type { SseEvent, TurnRequest } from "@contracts/contracts";
import { NetworkError, type ApiError, type ApiDeps, type ContractViolationError } from "@/lib/api/client";
import { streamTurn, type DemoFault, type StreamResult } from "@/lib/api/stream";
import { failTurn, initialConversationState, reduceEvent, type ConversationState } from "./reducer";

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
  | { kind: "turn"; id: string; turnId: string };

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
  addUserMessage: (text: string, mode: "chat" | "voice") => void;
  reset: () => void;
  armFault: (fault: DemoFault | null) => void;
  takeFault: () => DemoFault | null;
  sendTurn: (sessionId: string, request: TurnRequest, opts?: SendTurnOptions) => Promise<StreamResult>;
}

/**
 * What: creates an independent turn store.
 * Use when: tests (one store per test) and the app singleton below.
 * Inputs: none.
 * Output: a vanilla Zustand StoreApi.
 * Side effects: none until sendTurn is called.
 * Role: store factory. sendTurn takes the armed fault before the request (so it goes on exactly
 *   one request, even one that fails), adds the typed text to the thread, streams events into
 *   apply, and on a rejection fails every turn it saw that did not complete (failTurn), records
 *   the typed error and rethrows it; it never retries (doc 07 s9).
 */
export function createTurnStore(): StoreApi<TurnStoreState> {
  let userSeq = 0;
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
  return createStore<TurnStoreState>()((set, get) => ({
    conversation: initialConversationState,
    thread: [],
    connection: { phase: "idle", error: null },
    armedFault: null,
    apply: (event) => {
      const prev = get().conversation;
      const next = reduceEvent(prev, event);
      if (next === prev) return;
      const added = next.turnOrder.slice(prev.turnOrder.length).map((turnId): ThreadItem => ({ kind: "turn", id: turnId, turnId }));
      set(added.length > 0 ? { conversation: next, thread: [...get().thread, ...added] } : { conversation: next });
    },
    addUserMessage: (text, mode) => set({ thread: [...get().thread, userItem(text, mode)] }),
    reset: () =>
      set({ conversation: initialConversationState, thread: [], connection: { phase: "idle", error: null }, armedFault: null }),
    armFault: (fault) => set({ armedFault: fault }),
    takeFault: () => {
      const fault = get().armedFault;
      if (fault !== null) set({ armedFault: null });
      return fault;
    },
    sendTurn: async (sessionId, request, opts = {}) => {
      const fault = get().takeFault();
      const thread = request.text ? [...get().thread, userItem(request.text, "chat")] : get().thread;
      set({ thread, connection: { phase: "streaming", error: null } });
      const seen = new Set<string>();
      try {
        const result = await streamTurn(sessionId, request, {
          ...opts,
          fault,
          onEvent: (e) => {
            seen.add(e.turn_id);
            get().apply(e);
          },
        });
        set({ connection: { phase: "idle", error: null } });
        return result;
      } catch (err) {
        if (err instanceof NetworkError && err.turnId !== null) seen.add(err.turnId);
        const reason = err instanceof NetworkError ? err.reason : "contract";
        let conversation = get().conversation;
        for (const turnId of seen) conversation = failTurn(conversation, turnId, reason);
        set({ conversation, connection: { phase: "idle", error: err as TurnError } });
        throw err;
      }
    },
  }));
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
