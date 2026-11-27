"use client";
/**
 * What: React hook that owns one VoiceClient per console session and exposes its snapshot and a
 *   toggle for the mic button.
 * Use when: the console shell renders the mic button and the voice line.
 * Inputs: the session id (null: no session, no client) and the turn store.
 * Output: VoiceController ({ voice, toggle, end, mute, stopSpeaking, levels, available }).
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
const IDLE: VoiceSnapshot = Object.freeze({ state: "idle", label: VOICE_LABELS.idle, caption: null, error: null, muted: false, endsAt: null });

/**
 * What: what the console needs to drive a call.
 * Use when: typing useVoice's result, or a stand-in (the dev replay, tests).
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: the shell's voice seam (CS-E-05.S4.T1).
 */
export interface VoiceController {
  voice: VoiceSnapshot;
  toggle: () => void;
  end: () => void;
  mute: () => void;
  stopSpeaking: () => void;
  levels: () => { mic: number; agent: number };
  available: boolean;
}

/**
 * What: levels when there is no client.
 * Use when: no session.
 * Inputs: none.
 * Output: zeros.
 * Side effects: none.
 * Role: constant function.
 */
const NO_LEVELS = () => ({ mic: 0, agent: 0 });

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
 * Output: VoiceController.
 * Side effects: see module.
 * Role: see module.
 */
export function useVoice(sessionId: string | null, store: StoreApi<TurnStoreState>): VoiceController {
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

  const end = useCallback(() => client?.end(), [client]);
  const mute = useCallback(() => client?.toggleMute(), [client]);
  const stopSpeaking = useCallback(() => client?.stopSpeaking(), [client]);
  const levels = useMemo(() => (client === null ? NO_LEVELS : client.getLevels), [client]);
  return { voice, toggle, end, mute, stopSpeaking, levels, available: client !== null };
}
