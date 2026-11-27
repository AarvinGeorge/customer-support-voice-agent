/**
 * What: unit tests for the observability drawer's pure view logic (metric strip, step tree,
 *   Gate 1 words and numbers, Gate 2 claims, decisions, selection, LangSmith link).
 * Use when: `pnpm test`.
 * Inputs: the S1 and S4 contract fixtures replayed through the reducer.
 * Output: vitest results.
 * Side effects: none.
 * Role: CS-E-06.S3.T1 Done when: an S1 turn fills every strip value from turn.completed.usage;
 *   S4 shows the score against the threshold in words and numbers.
 */
import { describe, expect, it } from "vitest";
import type { SseEvent } from "@contracts/contracts";

import { loadFixture } from "@/test-support/fixtures";
import { replayEvents, type TurnView } from "./reducer";
import {
  decisionRows,
  inspectorSelection,
  inspectorSteps,
  langsmithRunUrl,
  metricStrip,
  retrievalView,
  turnHeading,
} from "./inspector";

const s1 = replayEvents(loadFixture("s1_chat.jsonl"));
const s4 = replayEvents(loadFixture("s4_unsure.jsonl"));

/**
 * What: the n-th turn (0-based) of a replayed conversation.
 * Use when: picking a fixture turn.
 * Inputs: the conversation and an index.
 * Output: the TurnView.
 * Side effects: throws when the index is missing.
 * Role: test helper.
 */
function nth(state: typeof s1, i: number): TurnView {
  const id = state.turnOrder[i];
  const turn = id === undefined ? undefined : state.turns[id];
  if (turn === undefined) throw new Error(`no turn ${i}`);
  return turn;
}

describe("metricStrip", () => {
  it("fills every strip value of an S1 turn from turn.completed.usage", () => {
    const strip = metricStrip(nth(s1, 0));
    expect(strip).not.toBeNull();
    const byKey = Object.fromEntries((strip ?? []).map((m) => [m.key, m]));
    expect(Object.keys(byKey)).toEqual([
      "latency", "first_status", "ttft", "cost", "tokens_in", "tokens_out", "models", "fallback", "served_by",
    ]);
    expect(byKey.latency?.value).toBe("3.90 s");
    expect(byKey.first_status?.value).toBe("0.04 s");
    expect(byKey.ttft?.value).toBe("3.90 s");
    expect(byKey.cost?.value).toBe("$0.0180");
    expect(byKey.tokens_in?.value).toBe("5,500");
    expect(byKey.tokens_in?.note).toBe("2,100 cached");
    expect(byKey.tokens_out?.value).toBe("100");
    expect(byKey.models?.value).toBe("claude-sonnet-4-5");
    expect(byKey.fallback?.value).toBe("not used");
    expect(byKey.served_by?.value).toBe("agent_a");
    for (const m of strip ?? []) expect(m.value).not.toBe("");
  });

  it("says n/a for a missing TTFT and none for a turn that used no model", () => {
    const committed = nth(s1, 2);
    const strip = metricStrip({
      ...committed,
      completed: committed.completed && { ...committed.completed, usage: { ...committed.completed.usage, ttft_ms: null } },
    });
    const byKey = Object.fromEntries((strip ?? []).map((m) => [m.key, m]));
    expect(byKey.ttft?.value).toBe("n/a");
    expect(byKey.models?.value).toBe("none");
  });

  it("returns null while the turn is still streaming", () => {
    const live = replayEvents(loadFixture("s1_chat.jsonl").slice(0, 3));
    expect(metricStrip(nth(live, 0))).toBeNull();
  });
});

describe("retrievalView (Gate 1)", () => {
  it("S4: shows the score against the thresholds in words and numbers for each band", () => {
    const words = [0, 1, 2].map((i) => {
      const gate = nth(s4, i).gates.find((g) => g.gate === "retrieval");
      if (!gate) throw new Error("no gate");
      return retrievalView(gate);
    });
    expect(words[0]?.numbers).toBe("0.12 vs tau_low 0.30 · tau_high 0.45");
    expect(words[0]?.words).toBe("Score 0.12 is below tau_low 0.30: no match, so the agent declined.");
    expect(words[1]?.words).toBe("Score 0.38 is between tau_low 0.30 and tau_high 0.45: weak match, so the agent asked to clarify.");
    expect(words[2]?.words).toBe("Score 0.41 is between tau_low 0.30 and tau_high 0.45: weak match, so the agent escalated.");
    expect(words[2]?.calibration).toBe("provisional");
    expect(words[2]?.provisional).toBe(true);
  });

  it("S1: a pass at or above tau_high", () => {
    const gate = nth(s1, 0).gates.find((g) => g.gate === "retrieval");
    if (!gate) throw new Error("no gate");
    const v = retrievalView(gate);
    expect(v.words).toBe("Score 0.71 is at or above tau_high 0.45: strong match, so the agent answered.");
    expect(v.chunks).toBe(3);
    expect(v.meter).toEqual({ score: 0.71, tauLow: 0.3, tauHigh: 0.45, tone: "success" });
  });
});

describe("retrievalView words for every doc 07 s4.1 reason (I1)", () => {
  const base = nth(s1, 0).gates.find((g) => g.gate === "retrieval");
  if (!base) throw new Error("no gate");
  const gate = (o: Partial<typeof base>) => ({ ...base, tau_low: 0.3, tau_high: 0.45, code_match: false, ...o });

  it("exact_code_match: pass with a low cosine never compares the score", () => {
    const v = retrievalView(gate({ verdict: "pass", decision: "answer", top_cosine: 0.2, code_match: true }));
    expect(v.words).toBe("Matched the error code exactly: strong match, so the agent answered.");
  });
  it("unknown_error_code: none with a high cosine does not blame the band", () => {
    const v = retrievalView(gate({ verdict: "none", decision: "escalate", top_cosine: 0.6 }));
    expect(v.words).toBe("Unknown error code (score 0.60 did not decide this): no match, so the agent escalated.");
  });
  it("below_threshold: none under tau_low", () => {
    const v = retrievalView(gate({ verdict: "none", decision: "decline", top_cosine: 0.12 }));
    expect(v.words).toBe("Score 0.12 is below tau_low 0.30: no match, so the agent declined.");
  });
  it("below_threshold: none with no chunks", () => {
    const v = retrievalView(gate({ verdict: "none", decision: "escalate", top_cosine: null }));
    expect(v.words).toBe("No score: no match, so the agent escalated.");
  });
  it("weak_match", () => {
    const v = retrievalView(gate({ verdict: "weak", decision: "clarify", top_cosine: 0.38 }));
    expect(v.words).toBe("Score 0.38 is between tau_low 0.30 and tau_high 0.45: weak match, so the agent asked to clarify.");
  });
  it("above_threshold", () => {
    const v = retrievalView(gate({ verdict: "pass", decision: "answer", top_cosine: 0.62 }));
    expect(v.words).toBe("Score 0.62 is at or above tau_high 0.45: strong match, so the agent answered.");
  });
  it("embedding_unavailable: error never says the docs lack the answer or compares a score", () => {
    const v = retrievalView(gate({ verdict: "error", decision: "escalate", top_cosine: 0.5 }));
    expect(v.words).toBe("Search error, so no score decided this: the agent escalated.");
  });
  it("retrieval_unavailable: error with no score", () => {
    const v = retrievalView(gate({ verdict: "error", decision: "escalate", top_cosine: null }));
    expect(v.words).toBe("Search error, so no score decided this: the agent escalated.");
  });
  it("no_calibration: uncalibrated has no thresholds to compare", () => {
    const v = retrievalView(gate({ verdict: "uncalibrated", decision: "clarify", top_cosine: 0.41, tau_low: null, tau_high: null }));
    expect(v.words).toBe("Score 0.41, thresholds not calibrated: the agent asked to clarify.");
  });
});

describe("inspectorSteps", () => {
  it("lists each step with its tool name, latency, model usage, gates and rule", () => {
    const steps = inspectorSteps(nth(s1, 1));
    expect(steps.map((s) => s.name)).toEqual(["identity_verify", "me_context", "agent", "list_charges", "agent", "prepare_refund"]);
    const model = steps.find((s) => s.model !== null);
    expect(model?.model).toBe("claude-sonnet-4-5 · 3,900 in (3,300 cached) · 45 out");
    const prep = steps.find((s) => s.rule !== null);
    expect(prep?.rule).toEqual({
      decision: "allowed",
      reasonCode: "PRO_WITHIN_LIMITS",
      policy: "refund-policy §2, Refunds on the Pro plan",
      tone: "success",
    });
    expect(steps.every((s) => s.latency.endsWith(" s"))).toBe(true);
  });

  it("carries the Gate 2 claims with supported words", () => {
    const steps = inspectorSteps(nth(s1, 3));
    const grounded = steps.find((s) => s.grounding !== null);
    expect(grounded?.grounding?.summary).toBe("passed, 1 of 1 claims supported");
    expect(grounded?.grounding?.claims).toEqual([
      { text: "Refunds reach your card in 5 to 10 business days", supported: true, source: "refund-policy#2" },
    ]);
  });
});

describe("decisionRows", () => {
  it("S1 turn 2: identity, Gate 1 absent, rule and fallback", () => {
    const rows = decisionRows(nth(s1, 1));
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r.text]));
    expect(byKey.identity).toBe("verified: Priya Raman, owner, Pro, active");
    expect(byKey.rule).toBe("allowed · PRO_WITHIN_LIMITS · refund-policy §2, Refunds on the Pro plan");
    expect(byKey.fallback).toBe("not used this turn");
  });

  it("S1 turn 3: the action outcome with reason codes and policy refs", () => {
    const rows = decisionRows(nth(s1, 2));
    const action = rows.find((r) => r.key === "action");
    expect(action?.text).toBe("refund committed, changed · PRO_WITHIN_LIMITS · refund-policy §2, Refunds on the Pro plan");
  });

  it("S4 turn 3: Gate 1 in words and the escalation with its ticket", () => {
    const rows = decisionRows(nth(s4, 2));
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r.text]));
    expect(byKey.gate1).toBe("weak, 0.41 against tau_high 0.45 (tau_low 0.30), escalated");
    expect(byKey.escalation).toBe("not in the help articles · ticket ending 8214 · opened by code");
  });

  it("names a model fallback", () => {
    const turn = nth(s1, 0);
    const fb = {
      ...(loadFixture("s1_chat.jsonl")[0] as SseEvent),
      type: "model.fallback",
      from_model: "claude-sonnet-4-5",
      to_model: "gpt-5.4-mini",
      reason: "rate_limit",
    } as Extract<SseEvent, { type: "model.fallback" }>;
    const rows = decisionRows({ ...turn, fallbacks: [fb] });
    expect(rows.find((r) => r.key === "fallback")?.text).toBe("claude-sonnet-4-5 to gpt-5.4-mini after rate limit");
  });
});

describe("selection and heading", () => {
  it("defaults to the latest turn and follows an explicit pick", () => {
    expect(inspectorSelection(s1.turnOrder, null)).toEqual({ turnId: s1.turnOrder[3], index: 3, total: 4 });
    expect(inspectorSelection(s1.turnOrder, s1.turnOrder[1] ?? null)).toEqual({ turnId: s1.turnOrder[1], index: 1, total: 4 });
    expect(inspectorSelection(s1.turnOrder, "gone")).toEqual({ turnId: s1.turnOrder[3], index: 3, total: 4 });
    expect(inspectorSelection([], null)).toBeNull();
  });

  it("describes the turn", () => {
    expect(turnHeading(nth(s1, 1))).toBe("chat · input verification · ended readback");
    expect(turnHeading(nth(s4, 0))).toBe("chat · input text · ended declined_off_topic");
  });
});

describe("langsmithRunUrl", () => {
  it("builds the run link from the turn id (run id = turn id, spike 7)", () => {
    expect(langsmithRunUrl("019a-turn", "https://smith.langchain.com/o/t1/projects/p/p1/")).toBe(
      "https://smith.langchain.com/o/t1/projects/p/p1/r/019a-turn?poll=true",
    );
  });

  it("drops a query string and fragment from the project URL (I2)", () => {
    expect(langsmithRunUrl("019a-turn", "https://smith.langchain.com/o/t/projects/p/p1?tab=0#x")).toBe(
      "https://smith.langchain.com/o/t/projects/p/p1/r/019a-turn?poll=true",
    );
  });

  it("returns null for an unparseable URL", () => {
    expect(langsmithRunUrl("t", "https://")).toBeNull();
  });

  it("returns null without a configured https project URL", () => {
    expect(langsmithRunUrl("t", undefined)).toBeNull();
    expect(langsmithRunUrl("t", "")).toBeNull();
    expect(langsmithRunUrl("t", "javascript:alert(1)")).toBeNull();
  });
});
