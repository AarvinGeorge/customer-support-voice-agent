/**
 * What: the Zustand store for the console conversation: reducer state, connection state, the
 *   one-shot demo fault, and sendTurn (streamTurn wired into the reducer).
 * Use when: React components read conversation state (useTurnStore) or send a turn.
 * Inputs: SseEvents via apply, TurnRequests via sendTurn, the fault toggle via armFault.
 * Output: store state and actions.
 * Side effects: sendTurn makes one network request; nothing is persisted.
 * Role: the only stateful wrapper around the pure reducer; chat now, the voice relay later.
 */
import { createStore, useStore, type StoreApi } from "zustand";
import type { SseEvent, TurnRequest } from "@contracts/contracts";
import type { ApiError, ApiDeps, ContractViolationError, NetworkError } from "@/lib/api/client";
import { streamTurn, type DemoFault, type StreamResult } from "@/lib/api/stream";
import { initialConversationState, reduceEvent, type ConversationState } from "./reducer";

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
  connection: { phase: "idle" | "streaming"; error: TurnError | null };
  /** The fault the drawer toggle armed for the next turn request, or null (doc 07 s10). */
  armedFault: DemoFault | null;
  apply: (event: SseEvent) => void;
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
 *   one request, even one that fails), streams events into apply, records the typed error and
 *   rethrows it; it never retries (doc 07 s9).
 */
export function createTurnStore(): StoreApi<TurnStoreState> {
  return createStore<TurnStoreState>()((set, get) => ({
    conversation: initialConversationState,
    connection: { phase: "idle", error: null },
    armedFault: null,
    apply: (event) => {
      const prev = get().conversation;
      const next = reduceEvent(prev, event);
      if (next !== prev) set({ conversation: next });
    },
    reset: () => set({ conversation: initialConversationState, connection: { phase: "idle", error: null }, armedFault: null }),
    armFault: (fault) => set({ armedFault: fault }),
    takeFault: () => {
      const fault = get().armedFault;
      if (fault !== null) set({ armedFault: null });
      return fault;
    },
    sendTurn: async (sessionId, request, opts = {}) => {
      const fault = get().takeFault();
      set({ connection: { phase: "streaming", error: null } });
      try {
        const result = await streamTurn(sessionId, request, { ...opts, fault, onEvent: (e) => get().apply(e) });
        set({ connection: { phase: "idle", error: null } });
        return result;
      } catch (err) {
        set({ connection: { phase: "idle", error: err as TurnError } });
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
