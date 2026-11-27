"use client";
/**
 * What: React hook that owns one VoiceClient per console session and exposes its snapshot and a
 *   toggle for the mic button.
 * Use when: the console shell renders the mic button and the voice line.
 * Inputs: the session id (null: no session, no client) and the turn store.
 * Output: { voice: VoiceSnapshot, toggle, available }.
 * Side effects: creates a VoiceClient per session; ends the call on unmount or session change.
 * Role: React binding for CS-E-05.S3.T3; toggle calls connect synchronously inside the click
 *   so getUserMedia runs in the user gesture.
 */
import { useCallback, useEffect, useMemo, useSyncExternalStore } from "react";
import type { StoreApi } from "zustand";
import type { TurnStoreState } from "@/lib/turns/store";

import { createVoiceClient, type VoiceSnapshot } from "./client";
import { VOICE_LABELS, isLive } from "./machine";

/**
 * What: the snapshot shown when there is no client.
 * Use when: no session.
 * Inputs: none.
 * Output: a frozen snapshot.
 * Side effects: none.
 * Role: constant (stable reference for useSyncExternalStore).
 */
const IDLE: VoiceSnapshot = Object.freeze({ state: "idle", label: VOICE_LABELS.idle, caption: null, error: null });

/**
 * What: a subscribe function that never notifies.
 * Use when: no client.
 * Inputs: a listener (ignored).
 * Output: an unsubscribe no-op.
 * Side effects: none.
 * Role: helper.
 */
const noSubscribe = () => () => undefined;

/**
 * What: the hook.
 * Use when: see module.
 * Inputs: sessionId, store.
 * Output: { voice, toggle, available }.
 * Side effects: see module.
 * Role: see module.
 */
export function useVoice(sessionId: string | null, store: StoreApi<TurnStoreState>) {
  const client = useMemo(() => (sessionId === null ? null : createVoiceClient({ sessionId, store })), [sessionId, store]);
  useEffect(() => () => client?.end(), [client]);
  const voice = useSyncExternalStore(client?.subscribe ?? noSubscribe, client?.getSnapshot ?? (() => IDLE), () => IDLE);

  /**
   * What: starts or ends the call.
   * Use when: the mic button is pressed.
   * Inputs: none.
   * Output: none.
   * Side effects: connect (mic, token, SDP) or end (teardown).
   * Role: handler; connect is called synchronously from the click.
   */
  const toggle = useCallback(() => {
    if (client === null) return;
    if (isLive(client.getSnapshot().state)) client.end();
    else void client.connect();
  }, [client]);

  return { voice, toggle, available: client !== null };
}
