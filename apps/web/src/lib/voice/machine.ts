/**
 * What: the pure voice state table (voiceMachine): states, events, transitions and the text
 *   label of every state.
 * Use when: VoiceClient maps a Realtime event or a local action to a state; the voice UI shows
 *   the label (06 s17: every state has words, not only a visual).
 * Inputs: a state and an event.
 * Output: the next state.
 * Side effects: none.
 * Role: CS-E-05.S3.T3 state table. `idle` is the state before the first call; an event the
 *   table does not name for a state keeps the state (no throw), because Realtime events arrive
 *   in any order around a barge-in.
 */

/**
 * What: the voice states, in display order.
 * Use when: iterating states (tests, labels).
 * Inputs: none.
 * Output: a readonly tuple.
 * Side effects: none.
 * Role: constant.
 */
export const VOICE_STATES = ["idle", "connecting", "listening", "thinking", "speaking", "interrupted", "error", "ended"] as const;

/**
 * What: one voice state.
 * Use when: typing state.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: type.
 */
export type VoiceState = (typeof VOICE_STATES)[number];

/**
 * What: the machine events. start (mic click), connected (session.created on the data
 *   channel), user_speech (speech_started), user_done (speech_stopped), tool_call (a completed
 *   ask_support_agent call), audio_started (agent audio or the say response began),
 *   audio_stopped (agent audio stopped or was cleared), fail, end (End or the 10-minute timer).
 * Use when: typing events.
 * Inputs: none.
 * Output: a readonly tuple.
 * Side effects: none.
 * Role: constant.
 */
export const VOICE_EVENTS = ["start", "connected", "user_speech", "user_done", "tool_call", "audio_started", "audio_stopped", "fail", "end"] as const;

/**
 * What: one machine event.
 * Use when: typing events.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: type.
 */
export type VoiceEvent = (typeof VOICE_EVENTS)[number];

/**
 * What: the transition table. Missing pairs keep the state.
 * Use when: nextVoiceState.
 * Inputs: none.
 * Output: a frozen map state to (event to state).
 * Side effects: none.
 * Role: the table itself. Thinking ignores audio_stopped (the holding phrase can end while the
 *   relay is still running) and leaves only when the say response starts. A barge-in while
 *   speaking is interrupted until the user stops talking or the next call or audio starts.
 */
export const VOICE_TRANSITIONS: Readonly<Record<VoiceState, Readonly<Partial<Record<VoiceEvent, VoiceState>>>>> = Object.freeze({
  idle: { start: "connecting" },
  connecting: { connected: "listening", fail: "error", end: "ended" },
  listening: { tool_call: "thinking", audio_started: "speaking", fail: "error", end: "ended" },
  thinking: { audio_started: "speaking", fail: "error", end: "ended" },
  speaking: { user_speech: "interrupted", audio_stopped: "listening", tool_call: "thinking", fail: "error", end: "ended" },
  interrupted: { user_done: "listening", tool_call: "thinking", audio_started: "speaking", fail: "error", end: "ended" },
  error: { start: "connecting", end: "ended" },
  ended: { start: "connecting", end: "ended" },
});

/**
 * What: the text label of every state.
 * Use when: the voice control shows the state in words.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (06 s17).
 */
export const VOICE_LABELS: Readonly<Record<VoiceState, string>> = Object.freeze({
  idle: "Voice off",
  connecting: "Connecting",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
  interrupted: "Interrupted",
  error: "Voice unavailable",
  ended: "Call ended",
});

/**
 * What: the next state for an event.
 * Use when: every machine event.
 * Inputs: the state, the event.
 * Output: the next state (the same state when the table has no entry).
 * Side effects: none.
 * Role: the machine.
 */
export function nextVoiceState(state: VoiceState, event: VoiceEvent): VoiceState {
  return VOICE_TRANSITIONS[state][event] ?? state;
}

/**
 * What: whether a call is open (connecting or connected).
 * Use when: the mic button decides between start and end.
 * Inputs: the state.
 * Output: boolean.
 * Side effects: none.
 * Role: helper.
 */
export function isLive(state: VoiceState): boolean {
  return state !== "idle" && state !== "error" && state !== "ended";
}
