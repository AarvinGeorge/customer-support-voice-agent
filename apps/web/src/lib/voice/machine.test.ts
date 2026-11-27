/**
 * What: tests for the pure voice state table (voiceMachine).
 * Use when: `pnpm test`.
 * Inputs: none.
 * Output: Vitest results.
 * Side effects: none.
 * Role: CS-E-05.S3.T3 Done when, part "pure state table"; every state has a text label (06 s17).
 */
import { describe, expect, it } from "vitest";

import { VOICE_EVENTS, VOICE_LABELS, VOICE_STATES, isLive, nextVoiceState, type VoiceEvent, type VoiceState } from "./machine";

/**
 * What: runs a list of events from a start state.
 * Use when: path tests.
 * Inputs: start state, events.
 * Output: the list of states after each event.
 * Side effects: none.
 * Role: helper.
 */
function walk(start: VoiceState, events: VoiceEvent[]): VoiceState[] {
  const out: VoiceState[] = [];
  let s = start;
  for (const e of events) {
    s = nextVoiceState(s, e);
    out.push(s);
  }
  return out;
}

describe("voiceMachine", () => {
  it("has the seven contract states plus idle, each with a non-empty text label", () => {
    expect([...VOICE_STATES].sort()).toEqual(["connecting", "ended", "error", "idle", "interrupted", "listening", "speaking", "thinking"]);
    for (const s of VOICE_STATES) expect(VOICE_LABELS[s].length).toBeGreaterThan(0);
    expect(new Set(Object.values(VOICE_LABELS)).size).toBe(VOICE_STATES.length);
  });

  it("a normal call: connect, the user asks, the holding phrase plays, the tool runs, the say plays, back to listening", () => {
    expect(
      walk("idle", ["start", "connected", "user_speech", "user_done", "audio_started", "tool_call", "audio_stopped", "audio_started", "audio_stopped", "end"]),
    ).toEqual(["connecting", "listening", "listening", "listening", "speaking", "thinking", "thinking", "speaking", "listening", "ended"]);
  });

  it("barge-in while speaking is interrupted; the end of the user's speech returns to listening", () => {
    expect(walk("speaking", ["user_speech", "audio_stopped", "user_done"])).toEqual(["interrupted", "interrupted", "listening"]);
    expect(walk("speaking", ["user_speech", "tool_call"])).toEqual(["interrupted", "thinking"]);
  });

  it("a failure from any live state is error, and a new start from error or ended connects again", () => {
    for (const s of ["connecting", "listening", "thinking", "speaking", "interrupted"] as const) expect(nextVoiceState(s, "fail")).toBe("error");
    expect(nextVoiceState("error", "start")).toBe("connecting");
    expect(nextVoiceState("ended", "start")).toBe("connecting");
  });

  it("end from any state that is not idle is ended; an event the table does not name keeps the state", () => {
    for (const s of VOICE_STATES) if (s !== "idle") expect(nextVoiceState(s, "end")).toBe("ended");
    expect(nextVoiceState("idle", "tool_call")).toBe("idle");
    expect(nextVoiceState("ended", "audio_started")).toBe("ended");
    expect(nextVoiceState("connecting", "tool_call")).toBe("connecting");
    for (const e of VOICE_EVENTS) expect(VOICE_STATES).toContain(nextVoiceState("thinking", e));
  });

  it("isLive is true only while a call is open", () => {
    expect(VOICE_STATES.filter(isLive).sort()).toEqual(["connecting", "interrupted", "listening", "speaking", "thinking"]);
  });
});
