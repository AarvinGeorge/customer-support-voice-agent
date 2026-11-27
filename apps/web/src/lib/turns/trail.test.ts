/**
 * What: tests for the step trail view helpers (summary line, rows, gate words, outcome phrase).
 * Use when: `pnpm test`.
 * Inputs: the S1, S4 and edge fixtures replayed through the reducer.
 * Output: pass or fail per rule.
 * Side effects: reads fixture files.
 * Role: proves the copy rules of CS-E-04.S5.T1 (doc 06 s16, F1 s4.1 and s4.2) without React.
 */
import { describe, expect, it } from "vitest";
import type { GateEvent, SseEvent } from "@contracts/contracts";
import { loadFixture } from "@/test-support/fixtures";
import { failTurn, replayEvents, type ConversationState, type TurnView } from "./reducer";
import { formatSeconds, formatStepLatency, gateWords, outcomePhrase, stepRow, trailSummary } from "./trail";

const s1 = loadFixture("s1_chat.jsonl");
const s4 = loadFixture("s4_unsure.jsonl");
const edge = loadFixture("edge_turns.jsonl");

/**
 * What: the TurnView of a fixture turn by turn id suffix.
 * Use when: a test names a turn.
 * Inputs: the state, the events, the suffix.
 * Output: the TurnView; throws if absent.
 * Side effects: none.
 * Role: test helper.
 */
function turnBy(state: ConversationState, events: SseEvent[], suffix: string): TurnView {
  const id = events.find((e) => e.turn_id.endsWith(suffix))?.turn_id;
  const t = id === undefined ? undefined : state.turns[id];
  if (t === undefined) throw new Error(`turn ${suffix} missing`);
  return t;
}

const S1 = replayEvents(s1);
const S4 = replayEvents(s4);
const EDGE = replayEvents(edge);

describe("formatting", () => {
  it("formats turn seconds with one decimal and step latency with two", () => {
    expect(formatSeconds(3900)).toBe("3.9 s");
    expect(formatSeconds(420)).toBe("0.4 s");
    expect(formatStepLatency(310)).toBe("0.31 s");
    expect(formatStepLatency(5)).toBe("0.01 s");
  });
});

describe("gateWords", () => {
  it("S4: shows the score against tau_high and the decision as text", () => {
    const last = s4.filter((e): e is GateEvent => e.type === "gate").at(-1) as GateEvent;
    const w = gateWords(last);
    expect(w.text).toBe("0.41 / 0.45, escalate");
    expect(w.label).toBe("Retrieval score 0.41, answer threshold 0.45, floor 0.30: weak, escalate");
  });

  it("compares a score below tau_low against tau_low", () => {
    const first = s4.find((e): e is GateEvent => e.type === "gate") as GateEvent;
    expect(gateWords(first).text).toBe("0.12 / 0.30, decline");
  });

  it("S1: a passing retrieval gate and a grounding gate in words", () => {
    const gates = s1.filter((e): e is GateEvent => e.type === "gate");
    expect(gateWords(gates[0] as GateEvent).text).toBe("0.71 / 0.45, answer");
    const grounding = gates.find((g) => g.gate === "grounding") as GateEvent;
    expect(gateWords(grounding).text).toBe("grounding passed, 1 of 1 claims");
  });

  it("M7: an uncalibrated gate with no thresholds says so in words", () => {
    const g = { ...(s4.find((e): e is GateEvent => e.type === "gate") as GateEvent), verdict: "uncalibrated", decision: "clarify" as const, top_cosine: 0.5, tau_low: null, tau_high: null };
    expect(gateWords(g).text).toBe("0.50, uncalibrated, clarify");
  });

  it("a skipped grounding gate", () => {
    const g = edge.find((e): e is GateEvent => e.type === "gate") as GateEvent;
    expect(gateWords(g).text).toBe("grounding skipped");
  });
});

describe("outcomePhrase", () => {
  it("names the outcome of each S1 turn", () => {
    expect(outcomePhrase(turnBy(S1, s1, "c1"))).toBe("Verification requested");
    expect(outcomePhrase(turnBy(S1, s1, "c2"))).toBe("Refund ready to confirm");
    expect(outcomePhrase(turnBy(S1, s1, "c3"))).toBe("Refund issued");
    expect(outcomePhrase(turnBy(S1, s1, "c4"))).toBe("Answered");
  });

  it("names S4 and edge outcomes, and a failed turn", () => {
    expect(outcomePhrase(turnBy(S4, s4, "f1"))).toBe("Declined, off topic");
    expect(outcomePhrase(turnBy(S4, s4, "f2"))).toBe("Asked to clarify");
    expect(outcomePhrase(turnBy(S4, s4, "f3"))).toBe("Escalated");
    expect(outcomePhrase(turnBy(EDGE, edge, "e3"))).toBe("Escalated");
    const c1 = s1.filter((e) => e.turn_id.endsWith("c1"));
    const partial = replayEvents(c1.slice(0, 4));
    const failed = failTurn(partial, c1[0]?.turn_id as string, "dropped");
    expect(outcomePhrase(failed.turns[c1[0]?.turn_id as string] as TurnView)).toBe("Couldn't finish");
  });
});

describe("trailSummary", () => {
  it("settles to n steps, seconds and outcome", () => {
    expect(trailSummary(turnBy(S1, s1, "c2"))).toMatchObject({ state: "done", text: "6 steps, 4.2 s · Refund ready to confirm" });
    expect(trailSummary(turnBy(S1, s1, "c3")).text).toBe("1 step, 0.4 s · Refund issued");
  });

  it("counts failed steps", () => {
    expect(trailSummary(turnBy(EDGE, edge, "e2")).text).toBe("4 steps, 1 failed, 1.9 s · Escalated");
  });

  it("while running shows the running step's label, then the status message", () => {
    const c2 = s1.filter((e) => e.turn_id.endsWith("c2"));
    const running = replayEvents(c2.slice(0, 9)); // list_charges started
    const t = running.turns[c2[0]?.turn_id as string] as TurnView;
    expect(trailSummary(t)).toMatchObject({ state: "running", text: "Checking your billing history..." });
    const c4 = s1.filter((e) => e.turn_id.endsWith("c4"));
    const held = replayEvents(c4.slice(0, 9)); // status checking_policy
    expect(trailSummary(held.turns[c4[0]?.turn_id as string] as TurnView).text).toBe("Checking the answer against policy...");
    const justStarted = replayEvents(c4.slice(0, 1));
    expect(trailSummary(justStarted.turns[c4[0]?.turn_id as string] as TurnView).text).toBe("Working...");
  });

  it("a failed turn reads Couldn't finish and keeps no live row", () => {
    const c2 = s1.filter((e) => e.turn_id.endsWith("c2"));
    const id = c2[0]?.turn_id as string;
    const failed = failTurn(replayEvents(c2.slice(0, 9)), id, "no_bytes_timeout");
    const t = failed.turns[id] as TurnView;
    expect(trailSummary(t)).toMatchObject({ state: "failed", text: "4 steps · Couldn't finish" });
    const last = t.steps[t.stepOrder.at(-1) as string];
    expect(stepRow(last!, t).running).toBe(false);
    expect(stepRow(last!, t).detail).toBe("stopped");
  });
});

describe("stepRow", () => {
  it("S1 rows: past-tense title, detail, latency; the gate replaces the search detail", () => {
    const t = turnBy(S1, s1, "c1");
    const rows = t.stepOrder.map((id) => stepRow(t.steps[id]!, t));
    expect(rows.map((r) => r.title)).toEqual(["Thought it through", "Searched help articles", "Thought it through", "Asked you to verify"]);
    expect(rows[0]).toMatchObject({ detail: "claude-sonnet-4-5", latency: "1.80 s", icon: "model" });
    expect(rows[1]).toMatchObject({ detail: "0.71 / 0.45, answer", icon: "search" });
    expect(rows[1]?.meter).toEqual({ score: 0.71, tauLow: 0.3, tauHigh: 0.45, tone: "success" });
  });

  it("a prepare step shows the rule decision, reason code and policy section", () => {
    const t = turnBy(S1, s1, "c2");
    const prep = t.stepOrder.map((id) => stepRow(t.steps[id]!, t)).find((r) => r.title === "Prepared refund");
    expect(prep).toMatchObject({ detail: "allowed · PRO_WITHIN_LIMITS · refund-policy §2", tone: "muted", icon: "wallet" });
  });

  it("retrying and failed steps say so in words", () => {
    const t = turnBy(EDGE, edge, "e2");
    const rows = t.stepOrder.map((id) => stepRow(t.steps[id]!, t));
    expect(rows[1]).toMatchObject({ detail: "retrying: Billing system did not respond", tone: "warning" });
    expect(rows[2]).toMatchObject({ detail: "failed: Billing system did not respond", tone: "error" });
    const e3 = turnBy(EDGE, edge, "e3");
    expect(stepRow(e3.steps[e3.stepOrder[0] as string]!, e3)).toMatchObject({ detail: "timed out", tone: "error" });
  });

  it("a running step uses its present-tense label and has no latency", () => {
    const c2 = s1.filter((e) => e.turn_id.endsWith("c2"));
    const t = replayEvents(c2.slice(0, 9)).turns[c2[0]?.turn_id as string] as TurnView;
    const row = stepRow(t.steps[t.stepOrder.at(-1) as string]!, t);
    expect(row).toMatchObject({ title: "Checking your billing history...", detail: "running", running: true, latency: "", tone: "ember" });
  });
});
