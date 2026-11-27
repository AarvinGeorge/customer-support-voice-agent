/**
 * What: tests for the pure SSE event reducer (doc 07 s6.1, s6.2).
 * Use when: `pnpm test`.
 * Inputs: the shared fixtures s1_chat.jsonl and edge_turns.jsonl.
 * Output: pass or fail per rule: one assertion per event type, seq ordering, idempotence,
 *   exactly one turn.completed, purity.
 * Side effects: reads fixture files.
 * Role: proves the doc 09 CS-E-04.S4.T2 Done when for the reducer.
 */
import { describe, expect, it } from "vitest";
import { EVENT_TYPES, type SseEvent, type TurnCompletedEvent } from "@contracts/contracts";
import { FIXTURES, loadFixture } from "@/test-support/fixtures";
import { failTurn, initialConversationState, reduceEvent, replayEvents, type ConversationState } from "./reducer";

const s1 = loadFixture("s1_chat.jsonl");
const edge = loadFixture("edge_turns.jsonl");
const S1 = replayEvents(s1);
const EDGE = replayEvents(edge);

/**
 * What: the turn id of a fixture turn by its suffix (c1..c4, e1..e3).
 * Use when: a test names a fixture turn.
 * Inputs: the fixture events, the suffix.
 * Output: the full turn id; throws if absent.
 * Side effects: none.
 * Role: test helper.
 */
function tid(events: SseEvent[], suffix: string): string {
  const id = events.find((e) => e.turn_id.endsWith(suffix))?.turn_id;
  if (id === undefined) throw new Error(`no turn ending ${suffix}`);
  return id;
}

/**
 * What: returns a TurnView that must exist.
 * Use when: a test reads one turn from a state.
 * Inputs: the state, the turn id.
 * Output: the TurnView; throws if absent.
 * Side effects: none.
 * Role: test helper.
 */
function turn(state: ConversationState, id: string) {
  const t = state.turns[id];
  if (t === undefined) throw new Error(`turn ${id} missing`);
  return t;
}

describe("fixtures cover every doc 07 s6.2 event type", () => {
  it("both fixtures together contain all 14 types", () => {
    const seen = new Set([...s1, ...edge].map((e) => e.type));
    expect([...seen].sort()).toEqual([...EVENT_TYPES].sort());
    expect(FIXTURES).toEqual(["s1_chat.jsonl", "edge_turns.jsonl"]);
  });
});

describe("replaying both fixtures: one assertion per event type", () => {
  it("turn.started: turns in wire order with replica and input kind", () => {
    expect(S1.turnOrder).toEqual(["c1", "c2", "c3", "c4"].map((x) => tid(s1, x)));
    expect(turn(S1, tid(s1, "c2")).started?.served_by).toBe("agent_b");
    expect(turn(S1, tid(s1, "c2")).started?.input_kind).toBe("verification");
    expect(S1.sessionId).toBe(s1[0]?.session_id);
  });

  it("step.started: steps kept in order with present-tense labels", () => {
    const c1 = turn(S1, tid(s1, "c1"));
    expect(c1.stepOrder).toEqual([1, 2, 3, 4].map((n) => `${c1.turnId}:${n}`));
    expect(c1.steps[`${c1.turnId}:2`]?.started?.label).toBe("Searching help articles");
  });

  it("step.completed: status, title, model tokens and retry or timeout outcomes", () => {
    const c1 = turn(S1, tid(s1, "c1"));
    const first = c1.steps[`${c1.turnId}:1`]?.completed;
    expect(first?.title).toBe("Thought it through");
    expect(first?.input_tokens).toBe(2100);
    const e2 = turn(EDGE, tid(edge, "e2"));
    expect(e2.steps[`${e2.turnId}:2`]?.completed?.status).toBe("retrying");
    expect(e2.steps[`${e2.turnId}:3`]?.completed?.attempt).toBe(2);
    const e3 = turn(EDGE, tid(edge, "e3"));
    expect(e3.steps[`${e3.turnId}:1`]?.completed?.status).toBe("timeout");
    const c2 = turn(S1, tid(s1, "c2"));
    expect(c2.steps[`${c2.turnId}:6`]?.completed?.rule?.decision).toBe("allowed");
  });

  it("gate: attached to its step and listed on the turn, with scores and claims", () => {
    const c1 = turn(S1, tid(s1, "c1"));
    const g = c1.steps[`${c1.turnId}:2`]?.gates[0];
    expect([g?.gate, g?.top_cosine, g?.tau_low, g?.tau_high, g?.decision]).toEqual(["retrieval", 0.71, 0.3, 0.45, "answer"]);
    const c4 = turn(S1, tid(s1, "c4"));
    expect(c4.gates.map((x) => x.gate)).toEqual(["retrieval", "grounding"]);
    expect(c4.gates[1]?.claims_supported).toBe(1);
  });

  it("status: shown while streaming, cleared by the first token and by turn.completed", () => {
    const e1Events = edge.filter((e) => e.turn_id === tid(edge, "e1"));
    const upToStatus = replayEvents(e1Events.filter((e) => e.seq <= 5));
    expect(turn(upToStatus, tid(edge, "e1")).status?.kind).toBe("slow");
    const afterToken = replayEvents(e1Events.filter((e) => e.seq <= 9));
    expect(turn(afterToken, tid(edge, "e1")).status).toBeNull();
    expect(turn(EDGE, tid(edge, "e2")).status).toBeNull();
  });

  it("token: chunks concatenate to the final answer text", () => {
    const c4id = tid(s1, "c4");
    const streamed = s1
      .filter((e): e is Extract<SseEvent, { type: "token" }> => e.type === "token" && e.turn_id === c4id)
      .map((e) => e.text)
      .join("");
    const partial = replayEvents(s1.filter((e) => !(e.turn_id === c4id && e.type === "turn.completed")));
    expect(turn(partial, c4id).text).toBe(streamed);
    expect(turn(S1, c4id).text).toBe(turn(S1, c4id).completed?.final.text);
  });

  it("verification.required: stored on the turn and opens the session card", () => {
    const c1Only = replayEvents(s1.filter((e) => e.turn_id === tid(s1, "c1")));
    expect(turn(c1Only, tid(s1, "c1")).verificationRequired?.attempts_left).toBe(3);
    expect(c1Only.awaitingVerification?.fields).toEqual(["account_id", "pin"]);
  });

  it("verification.result: verified person set and the card closes", () => {
    expect(turn(S1, tid(s1, "c2")).verificationResult?.status).toBe("verified");
    expect(S1.verified?.display_name).toBe("Priya Raman");
    expect(S1.awaitingVerification).toBeNull();
  });

  it("confirmation.required: read-back card with its label and the session pending action", () => {
    const upToC2 = replayEvents(s1.filter((e) => [tid(s1, "c1"), tid(s1, "c2")].includes(e.turn_id)));
    expect(turn(upToC2, tid(s1, "c2")).confirmation?.confirm_label).toBe("Confirm refund");
    expect(upToC2.pendingAction?.pending_action_id).toBe("PA-3F2A9C1B");
  });

  it("action.result: committed outcome recorded and the pending action clears", () => {
    expect(turn(S1, tid(s1, "c3")).actionResults.map((a) => a.outcome)).toEqual(["committed"]);
    expect(S1.pendingAction).toBeNull();
  });

  it("escalated: reason and ticket on the turn", () => {
    const e2 = turn(EDGE, tid(edge, "e2"));
    expect(e2.escalation?.reason).toBe("tool_failure");
    expect(e2.escalation?.ticket?.short_id).toBe("8213");
    expect(EDGE.ticketOffer).toBeNull();
  });

  it("model.fallback: from and to models recorded for the badge", () => {
    const fb = turn(EDGE, tid(edge, "e1")).fallbacks;
    expect(fb.map((f) => [f.from_model, f.to_model, f.reason])).toEqual([["claude-sonnet-4-5", "gpt-4.1-mini", "fault_llm_429"]]);
  });

  it("error: in-stream error kept and the turn still completes", () => {
    const e3 = turn(EDGE, tid(edge, "e3"));
    expect(e3.errors.map((e) => [e.class, e.code])).toEqual([["turn_deadline_exceeded", "TURN_DEADLINE"]]);
    expect(e3.phase).toBe("completed");
  });

  it("turn.completed: every turn completes once with its end reason", () => {
    const ends = (st: ConversationState) => st.turnOrder.map((id) => [turn(st, id).phase, turn(st, id).completed?.end_reason]);
    expect(ends(S1)).toEqual([
      ["completed", "verification_requested"],
      ["completed", "readback"],
      ["completed", "committed"],
      ["completed", "answered"],
    ]);
    expect(ends(EDGE)).toEqual([
      ["completed", "answered"],
      ["completed", "escalated"],
      ["completed", "deadline"],
    ]);
  });
});

describe("ordering, idempotence and the single turn.completed", () => {
  const c4id = tid(s1, "c4");
  const c4 = s1.filter((e) => e.turn_id === c4id);

  it("ignores an event whose seq is not above the last applied seq (out of order)", () => {
    const late = c4.find((e) => e.seq === 11);
    const before = replayEvents(c4.filter((e) => e.seq <= 12 && e.seq !== 11));
    if (late === undefined) throw new Error("fixture changed");
    const after = reduceEvent(before, late);
    expect(after).toBe(before);
    expect(turn(after, c4id).lastSeq).toBe(12);
  });

  it("applies duplicates once: replaying every event twice equals replaying once", () => {
    const doubled = s1.flatMap((e) => [e, e]);
    expect(replayEvents(doubled)).toEqual(S1);
    const doubledEdge = [...edge, ...edge];
    expect(replayEvents(doubledEdge)).toEqual(EDGE);
  });

  it("accepts exactly one turn.completed; later events for that turn are ignored", () => {
    const done = turn(S1, c4id).completed as TurnCompletedEvent;
    const second: TurnCompletedEvent = { ...done, seq: done.seq + 1, end_reason: "error", final: { ...done.final, text: "other" } };
    const token: SseEvent = { ...done, type: "token", seq: done.seq + 2, text: "late" } as SseEvent;
    const after = replayEvents([second, token], S1);
    expect(after).toBe(S1);
    expect(turn(after, c4id).completed?.end_reason).toBe("answered");
  });

  it("ignores events from another session", () => {
    const foreign = edge[0] as SseEvent;
    expect(reduceEvent(S1, foreign)).toBe(S1);
  });

  it("creates a step from step.completed when its step.started never arrived", () => {
    const c3 = s1.filter((e) => e.turn_id === tid(s1, "c3") && e.type !== "step.started");
    const st = replayEvents(c3);
    const t = turn(st, tid(s1, "c3"));
    expect(t.stepOrder).toEqual([`${t.turnId}:1`]);
    expect(t.steps[`${t.turnId}:1`]?.started).toBeNull();
    expect(t.steps[`${t.turnId}:1`]?.completed?.title).toBe("Issued refund");
  });

  it("does not mutate the input state (pure)", () => {
    const snapshot = structuredClone(S1);
    replayEvents(edge, initialConversationState);
    replayEvents(s1.slice(0, 5), S1);
    expect(S1).toEqual(snapshot);
    expect(initialConversationState).toEqual({
      sessionId: null,
      turnOrder: [],
      turns: {},
      verified: null,
      verifyLocked: false,
      awaitingVerification: null,
      pendingAction: null,
      ticketOffer: null,
    });
  });

  it("raises the ticket offer on a model_unavailable escalation and clears it on the next turn", () => {
    const e2id = tid(edge, "e2");
    const esc = edge.find((e) => e.turn_id === e2id && e.type === "escalated");
    if (esc?.type !== "escalated") throw new Error("fixture changed");
    const upTo = replayEvents(edge.filter((e) => e.turn_id === e2id && e.seq < esc.seq));
    const offered = reduceEvent(upTo, { ...esc, reason: "model_unavailable", ticket: null, offer_ticket_button: true });
    expect(offered.ticketOffer?.reason).toBe("model_unavailable");
    const next = edge.find((e) => e.turn_id === tid(edge, "e3") && e.type === "turn.started") as SseEvent;
    expect(reduceEvent(offered, next).ticketOffer).toBeNull();
  });

  it("marks the session locked on verification.result locked and closes the card", () => {
    const c1Only = replayEvents(s1.filter((e) => e.turn_id === tid(s1, "c1")));
    const res = s1.find((e) => e.type === "verification.result");
    if (res?.type !== "verification.result") throw new Error("fixture changed");
    const locked = reduceEvent(c1Only, { ...res, status: "locked", person: null, attempts_left: 0 });
    expect(locked.verifyLocked).toBe(true);
    expect(locked.awaitingVerification).toBeNull();
    expect(locked.verified).toBeNull();
  });
});

describe("failTurn: a turn that never completed reaches a terminal state (review PR #16, I1)", () => {
  const c1 = tid(s1, "c1");
  const partial = replayEvents(s1.filter((e) => e.turn_id === c1).slice(0, 4));

  it("moves a streaming turn to failed with a retryable failure and clears the live status", () => {
    const next = failTurn(partial, c1, "turn_timeout");
    expect(turn(next, c1).phase).toBe("failed");
    expect(turn(next, c1).failure).toEqual({ reason: "turn_timeout", retryable: true, message: "This reply took too long." });
    expect(turn(next, c1).status).toBeNull();
  });

  it("ignores later events for a failed turn", () => {
    const failed = failTurn(partial, c1, "dropped");
    const later = s1.filter((e) => e.turn_id === c1).slice(4);
    expect(replayEvents(later, failed)).toBe(failed);
  });

  it("leaves a completed turn and an unknown turn unchanged", () => {
    expect(failTurn(S1, c1, "dropped")).toBe(S1);
    expect(failTurn(partial, "no-such-turn", "dropped")).toBe(partial);
  });

  it("names every failure reason in plain words", () => {
    const words = (["network", "dropped", "no_bytes_timeout", "turn_timeout", "aborted", "contract"] as const).map(
      (r) => turn(failTurn(partial, c1, r), c1).failure?.message,
    );
    expect(new Set(words).size).toBe(6);
    expect(words.every((w) => typeof w === "string" && w.length > 0)).toBe(true);
  });
});
