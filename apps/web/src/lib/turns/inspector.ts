/**
 * What: pure view logic for the observability drawer (the Inspector): the metric strip from
 *   turn.completed.usage, the step tree with per-step latency, model usage, Gate 1 and Gate 2
 *   and rule details, the decisions list, the selected turn and the LangSmith run link.
 * Use when: components/console/inspector-drawer.tsx renders a turn.
 * Inputs: TurnView values from the reducer (our own SSE events only, doc 07 s6.2).
 * Output: plain view objects and strings.
 * Side effects: none.
 * Role: CS-E-06.S3.T1 (doc 06 s8, s16; doc 07 s6.2 `gate`, `Usage`, s1.2); copy lives here so it
 *   is unit tested apart from layout. The trace detail tab and /api/traces are designed only.
 */
import type { GateEvent, PolicyRef } from "@contracts/contracts";

import type { StepView, TurnView } from "./reducer";
import { formatStepLatency, stepRow, type GateMeter, type StepIcon, type TrailTone } from "./trail";

/**
 * What: the keys of the metric strip, in display order.
 * Use when: typing and ordering metrics.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: type.
 */
export type MetricKey =
  | "latency"
  | "first_status"
  | "ttft"
  | "cost"
  | "tokens_in"
  | "tokens_out"
  | "models"
  | "fallback"
  | "served_by";

/**
 * What: one metric of the strip.
 * Use when: rendering the strip.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view type; `note` is a secondary value (cached tokens), `tone` colours a word value.
 */
export interface Metric {
  key: MetricKey;
  label: string;
  value: string;
  note: string | null;
  tone: TrailTone;
}

/**
 * What: an integer with thousands separators ("5,500").
 * Use when: token counts.
 * Inputs: n.
 * Output: a string.
 * Side effects: none.
 * Role: formatter (fixed en-US so tests and screenshots agree).
 */
export function formatCount(n: number): string {
  return n.toLocaleString("en-US");
}

/**
 * What: milliseconds as seconds with two decimals, or "n/a" when unknown.
 * Use when: strip timings.
 * Inputs: ms or null/undefined.
 * Output: "3.90 s" or "n/a".
 * Side effects: none.
 * Role: formatter.
 */
function seconds(ms: number | null | undefined): string {
  return ms == null ? "n/a" : `${(ms / 1000).toFixed(2)} s`;
}

/**
 * What: the metric strip for a finished turn, every value read from turn.completed.usage
 *   (served_by from turn.started).
 * Use when: the drawer's strip.
 * Inputs: the turn.
 * Output: Metric[] in display order, or null while the turn has no turn.completed.
 * Side effects: none.
 * Role: Done-when source (S1 fills every value).
 */
export function metricStrip(turn: TurnView): Metric[] | null {
  const done = turn.completed;
  if (done === null) return null;
  const u = done.usage;
  const m = (key: MetricKey, label: string, value: string, note: string | null = null, tone: TrailTone = "muted"): Metric => ({
    key,
    label,
    value,
    note,
    tone,
  });
  return [
    m("latency", "End-to-end", seconds(u.latency_ms)),
    m("first_status", "First status", seconds(u.first_status_ms)),
    m("ttft", "First token (TTFT)", seconds(u.ttft_ms)),
    m("cost", "Est. cost", `$${u.cost_usd_est.toFixed(4)}`),
    m("tokens_in", "Tokens in", formatCount(u.input_tokens), `${formatCount(u.cache_read_tokens)} cached`),
    m("tokens_out", "Tokens out", formatCount(u.output_tokens)),
    m("models", u.models_used.length > 1 ? "Models" : "Model", u.models_used.length > 0 ? u.models_used.join(", ") : "none"),
    m("fallback", "Fallback", u.fallback_used ? "used" : "not used", null, u.fallback_used ? "warning" : "muted"),
    m("served_by", "Served by", turn.started?.served_by ?? "unknown"),
  ];
}

/**
 * What: words for a retrieval verdict.
 * Use when: Gate 1 sentences.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (doc 07 s6.2 retrieval verdicts).
 */
const RETRIEVAL_VERDICT: Readonly<Record<string, string>> = Object.freeze({
  pass: "strong match",
  weak: "weak match",
  none: "no match",
  error: "search error",
  uncalibrated: "thresholds not calibrated",
});

/**
 * What: words for a gate decision, as what the agent did.
 * Use when: Gate 1 and Gate 2 sentences.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (doc 07 s6.2 `decision`).
 */
const DECISION_WORDS: Readonly<Record<GateEvent["decision"], string>> = Object.freeze({
  answer: "answered",
  clarify: "asked to clarify",
  escalate: "escalated",
  decline: "declined",
  skip: "skipped the check",
});

/**
 * What: a score with two decimals.
 * Use when: gate numbers.
 * Inputs: n.
 * Output: "0.41".
 * Side effects: none.
 * Role: formatter.
 */
const score = (n: number): string => n.toFixed(2);

/**
 * What: Gate 1 shown as numbers, a sentence, the calibration status and a meter.
 * Use when: a retrieval gate row and its decision line.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view type.
 */
export interface RetrievalView {
  numbers: string;
  words: string;
  verdict: string;
  decision: string;
  calibration: string | null;
  provisional: boolean;
  chunks: number;
  codeMatch: boolean;
  meter: GateMeter | null;
  tone: TrailTone;
}

/**
 * What: the Gate 1 sentence, built from the verdict, code_match and the numbers so it never
 *   contradicts them (doc 07 s4.1: a code match or unknown code decides before the score bands,
 *   an error is not evidence about the docs, uncalibrated has no thresholds).
 * Use when: retrievalView.
 * Inputs: the gate event, its score and thresholds, and the verdict and decision words.
 * Output: one sentence.
 * Side effects: none.
 * Role: helper; the event carries no reason field, so the reason is inferred from verdict,
 *   code_match and whether the score is below tau_low.
 */
function retrievalWords(g: GateEvent, top: number | null, low: number | null, high: number | null, verdict: string, decision: string): string {
  if (g.verdict === "pass" && g.code_match === true) return `Matched the error code exactly: ${verdict}, so the agent ${decision}.`;
  if (g.verdict === "error") return `Search error, so no score decided this: the agent ${decision}.`;
  if (g.verdict === "uncalibrated") {
    return `${top === null ? "No score" : `Score ${score(top)}`}, thresholds not calibrated: the agent ${decision}.`;
  }
  if (top === null) return `No score: ${verdict}, so the agent ${decision}.`;
  if (g.verdict === "none" && low !== null && top >= low) {
    return `Unknown error code (score ${score(top)} did not decide this): ${verdict}, so the agent ${decision}.`;
  }
  let band: string;
  if (low !== null && top < low) band = `below tau_low ${score(low)}`;
  else if (high !== null && top >= high) band = `at or above tau_high ${score(high)}`;
  else if (low !== null && high !== null) band = `between tau_low ${score(low)} and tau_high ${score(high)}`;
  else band = "without thresholds";
  return `Score ${score(top)} is ${band}: ${verdict}, so the agent ${decision}.`;
}

/**
 * What: Gate 1 (retrieval) in numbers ("0.41 vs tau_low 0.30 · tau_high 0.45") and in words
 *   ("Score 0.41 is between tau_low 0.30 and tau_high 0.45: weak match, so the agent escalated.").
 * Use when: rendering a retrieval gate.
 * Inputs: the gate event (gate "retrieval").
 * Output: RetrievalView.
 * Side effects: none.
 * Role: Done-when source (S4 shows the score against the threshold in words and numbers).
 */
export function retrievalView(g: GateEvent): RetrievalView {
  const top = g.top_cosine ?? null;
  const low = g.tau_low ?? null;
  const high = g.tau_high ?? null;
  const verdict = RETRIEVAL_VERDICT[g.verdict] ?? g.verdict;
  const decision = DECISION_WORDS[g.decision];
  const tone: TrailTone = g.verdict === "pass" ? "success" : g.verdict === "weak" || g.verdict === "uncalibrated" ? "warning" : "error";
  const thresholds = [low !== null ? `tau_low ${score(low)}` : null, high !== null ? `tau_high ${score(high)}` : null].filter(Boolean).join(" · ");
  let numbers: string;
  if (top === null) numbers = thresholds || "no score";
  else numbers = thresholds ? `${score(top)} vs ${thresholds}` : score(top);
  const words = retrievalWords(g, top, low, high, verdict, decision);
  const calibration = g.calibration_status ?? null;
  return {
    numbers,
    words,
    verdict: g.verdict,
    decision: g.decision,
    calibration,
    provisional: calibration === "provisional",
    chunks: g.chunk_ids.length,
    codeMatch: g.code_match === true,
    meter: top === null ? null : { score: top, tauLow: low, tauHigh: high, tone },
    tone,
  };
}

/**
 * What: words for a grounding verdict.
 * Use when: Gate 2 summaries.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table.
 */
const GROUNDING_VERDICT: Readonly<Record<string, string>> = Object.freeze({
  pass: "passed",
  fail: "failed",
  skipped: "skipped",
  error: "could not run",
});

/**
 * What: Gate 2 shown as a summary and the checked claims.
 * Use when: a grounding gate row.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view type.
 */
export interface GroundingView {
  summary: string;
  claims: { text: string; supported: boolean; source: string | null }[];
  commitment: boolean;
  tone: TrailTone;
}

/**
 * What: Gate 2 (grounding) as "passed, 1 of 1 claims supported" plus each claim.
 * Use when: rendering a grounding gate.
 * Inputs: the gate event (gate "grounding").
 * Output: GroundingView.
 * Side effects: none.
 * Role: helper.
 */
export function groundingView(g: GateEvent): GroundingView {
  const verdict = GROUNDING_VERDICT[g.verdict] ?? g.verdict;
  const claims = g.claims_total != null ? `, ${g.claims_supported ?? 0} of ${g.claims_total} claims supported` : "";
  return {
    summary: `${verdict}${claims}`,
    claims: (g.claims ?? []).map((c) => ({ text: c.text, supported: c.supported, source: c.source_id ?? null })),
    commitment: g.commitment === true,
    tone: g.verdict === "pass" ? "success" : g.verdict === "skipped" ? "muted" : "error",
  };
}

/**
 * What: a policy reference in words ("refund-policy §2, Refunds on the Pro plan").
 * Use when: rule and action lines.
 * Inputs: the PolicyRef.
 * Output: a string.
 * Side effects: none.
 * Role: formatter.
 */
function policyWords(p: PolicyRef): string {
  return `${p.article} §${p.section}${p.title ? `, ${p.title}` : ""}`;
}

/**
 * What: one row of the step tree.
 * Use when: rendering the tree.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view type; `name` is the code name (tool or model node) shown in mono.
 */
export interface InspectorStep {
  id: string;
  icon: StepIcon;
  title: string;
  name: string;
  kind: string;
  latency: string;
  status: string;
  tone: TrailTone;
  detail: string | null;
  model: string | null;
  retrieval: RetrievalView | null;
  grounding: GroundingView | null;
  rule: { decision: string; reasonCode: string; policy: string; tone: TrailTone } | null;
}

/**
 * What: the step tree of a turn, one entry per step in event order.
 * Use when: the drawer's step list.
 * Inputs: the turn.
 * Output: InspectorStep[].
 * Side effects: none.
 * Role: builds on trail.ts stepRow (title, icon, status) and adds the raw fields the drawer shows.
 */
export function inspectorSteps(turn: TurnView): InspectorStep[] {
  return turn.stepOrder
    .map((id) => turn.steps[id])
    .filter((s): s is StepView => s !== undefined)
    .map((step) => {
      const row = stepRow(step, turn);
      const done = step.completed;
      const retrieval = step.gates.findLast((g) => g.gate === "retrieval");
      const grounding = step.gates.find((g) => g.gate === "grounding");
      const rule = done?.rule ?? null;
      const model =
        done?.model != null
          ? `${done.model} · ${formatCount(done.input_tokens ?? 0)} in (${formatCount(done.cache_read_tokens ?? 0)} cached) · ${formatCount(done.output_tokens ?? 0)} out`
          : null;
      return {
        id: step.stepId,
        icon: row.icon,
        title: row.title,
        name: step.started?.name ?? "",
        kind: step.started?.kind ?? "",
        latency: done ? formatStepLatency(done.ms) : row.statusWords,
        status: done?.status ?? row.statusWords,
        tone: row.tone,
        detail: done?.detail ?? (done === null ? row.detail : null),
        model,
        retrieval: retrieval ? retrievalView(retrieval) : null,
        grounding: grounding ? groundingView(grounding) : null,
        rule: rule
          ? {
              decision: rule.decision.replace("_", " "),
              reasonCode: rule.reason_code,
              policy: policyWords(rule.policy_ref),
              tone: rule.decision === "declined" ? "error" : rule.decision === "needs_approval" ? "warning" : "success",
            }
          : null,
      };
    });
}

/**
 * What: the keys of the decisions list.
 * Use when: typing decision rows.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: type.
 */
export type DecisionKey = "identity" | "gate1" | "gate2" | "rule" | "action" | "escalation" | "fallback";

/**
 * What: one decision row: what was decided in this turn, in words.
 * Use when: the Decisions section.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view type.
 */
export interface DecisionRow {
  key: DecisionKey;
  label: string;
  text: string;
  tone: TrailTone;
}

/**
 * What: words for a role and a plan tier.
 * Use when: the identity row.
 * Inputs: none.
 * Output: frozen maps.
 * Side effects: none.
 * Role: copy tables.
 */
const ROLE: Readonly<Record<string, string>> = Object.freeze({ owner: "owner", billing_admin: "billing admin", member: "member" });
const TIER: Readonly<Record<string, string>> = Object.freeze({ free: "Free", pro: "Pro", business: "Business", enterprise: "Enterprise" });

/**
 * What: words for an escalation reason.
 * Use when: the escalation row.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (doc 07 s6.2 `escalated.reason`).
 */
const ESCALATION: Readonly<Record<string, string>> = Object.freeze({
  not_in_docs: "not in the help articles",
  grounding_failed: "answer check failed",
  retrieval_unavailable: "search unavailable",
  tool_failure: "a tool failed",
  limit_reached: "limit reached",
  deadline: "turn deadline",
  verify_locked: "verification locked",
  needs_commitment: "needs a person's commitment",
  customer_asked_human: "customer asked for a person",
  model_unavailable: "models unavailable",
});

/**
 * What: words for a fallback reason.
 * Use when: the fallback row.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table.
 */
const FALLBACK_REASON: Readonly<Record<string, string>> = Object.freeze({
  rate_limit: "rate limit",
  timeout: "timeout",
  "5xx": "provider error",
  spend_cap: "spend cap",
  auth: "auth error",
  fault_llm_429: "injected 429",
});

/**
 * What: the decisions of one turn: identity, Gate 1, Gate 2, rules, action outcomes, escalation
 *   and fallback, each in one line of words.
 * Use when: the Decisions section.
 * Inputs: the turn.
 * Output: DecisionRow[] (rows without data are left out, except fallback, always present).
 * Side effects: none.
 * Role: helper.
 */
export function decisionRows(turn: TurnView): DecisionRow[] {
  const rows: DecisionRow[] = [];
  const v = turn.verificationResult;
  if (v !== null) {
    const p = v.person;
    const who = p ? `: ${p.display_name}, ${ROLE[p.role] ?? p.role}, ${TIER[p.tier] ?? p.tier}, ${p.contract_status}` : "";
    const left = v.attempts_left != null ? `, ${v.attempts_left} attempts left` : "";
    rows.push({ key: "identity", label: "Identity", text: `${v.status}${who}${left}`, tone: v.status === "verified" ? "success" : "warning" });
  } else if (turn.verificationRequired !== null) {
    rows.push({ key: "identity", label: "Identity", text: `asked to verify (${turn.verificationRequired.reason.replace("_", " ")})`, tone: "muted" });
  }
  const g1 = turn.gates.findLast((g) => g.gate === "retrieval");
  if (g1) {
    const r = retrievalView(g1);
    const top = g1.top_cosine;
    const high = g1.tau_high;
    const low = g1.tau_low;
    const against = top != null && high != null ? `, ${score(top)} against tau_high ${score(high)}${low != null ? ` (tau_low ${score(low)})` : ""}` : "";
    rows.push({ key: "gate1", label: "Gate 1", text: `${g1.verdict}${against}, ${DECISION_WORDS[g1.decision]}`, tone: r.tone });
  }
  const g2 = turn.gates.find((g) => g.gate === "grounding");
  if (g2) {
    const w = groundingView(g2);
    rows.push({ key: "gate2", label: "Gate 2", text: w.summary, tone: w.tone });
  }
  for (const id of turn.stepOrder) {
    const rule = turn.steps[id]?.completed?.rule;
    if (rule) {
      rows.push({
        key: "rule",
        label: "Rule",
        text: `${rule.decision.replace("_", " ")} · ${rule.reason_code} · ${policyWords(rule.policy_ref)}`,
        tone: rule.decision === "declined" ? "error" : rule.decision === "needs_approval" ? "warning" : "success",
      });
    }
  }
  for (const a of turn.actionResults) {
    const changed = a.changed === true ? ", changed" : a.changed === false ? ", nothing changed" : "";
    const parts = [`${a.action_type.replace("_", " ")} ${a.outcome.replace(/_/g, " ")}${changed}`, ...a.reason_codes, ...a.policy_refs.map(policyWords)];
    const ok = a.outcome === "committed" || a.outcome === "submitted" || a.outcome === "act_alone_applied";
    rows.push({ key: "action", label: "Action", text: parts.join(" · "), tone: ok ? "success" : a.outcome === "cancelled" ? "muted" : "warning" });
  }
  const e = turn.escalation;
  if (e !== null) {
    const parts = [ESCALATION[e.reason] ?? e.reason, e.ticket ? e.ticket.display : "no ticket yet", `opened by ${e.origin}`];
    rows.push({ key: "escalation", label: "Escalation", text: parts.join(" · "), tone: "warning" });
  }
  const fb = turn.fallbacks.at(-1);
  rows.push(
    fb
      ? { key: "fallback", label: "Fallback", text: `${fb.from_model} to ${fb.to_model} after ${FALLBACK_REASON[fb.reason] ?? fb.reason}`, tone: "warning" }
      : { key: "fallback", label: "Fallback", text: "not used this turn", tone: "muted" },
  );
  return rows;
}

/**
 * What: one line describing a turn ("chat · input verification · ended readback").
 * Use when: the drawer's step tree heading.
 * Inputs: the turn.
 * Output: a string.
 * Side effects: none.
 * Role: formatter.
 */
export function turnHeading(turn: TurnView): string {
  const parts: string[] = [turn.mode];
  if (turn.started) parts.push(`input ${turn.started.input_kind}`);
  if (turn.completed) parts.push(`ended ${turn.completed.end_reason}`);
  else if (turn.phase === "failed") parts.push("failed");
  else parts.push("running");
  return parts.join(" · ");
}

/**
 * What: which turn the drawer shows.
 * Use when: the drawer header ("Turn 2 of 4") and its previous and next buttons.
 * Inputs: the conversation's turnOrder and the picked turn id (null follows the latest turn).
 * Output: { turnId, index, total }, or null with no turns.
 * Side effects: none.
 * Role: per-turn selection; an unknown id (after New conversation) falls back to the latest.
 */
export function inspectorSelection(order: readonly string[], picked: string | null): { turnId: string; index: number; total: number } | null {
  if (order.length === 0) return null;
  const at = picked === null ? -1 : order.indexOf(picked);
  const index = at === -1 ? order.length - 1 : at;
  return { turnId: order[index] as string, index, total: order.length };
}

/**
 * What: the LangSmith link of a turn's root run: `{projectUrl}/r/{turn_id}?poll=true` (spike 7
 *   proved the root run id equals the turn id).
 * Use when: the drawer footer.
 * Inputs: the turn id and the public project URL (NEXT_PUBLIC_LANGSMITH_PROJECT_URL, the
 *   LangSmith page of the LANGSMITH_PROJECT project; never a key).
 * Output: the URL, or null when the project URL is unset or not https.
 * Side effects: none.
 * Role: the drawer's way into the full trace (the trace proxy is designed only, D-035).
 */
export function langsmithRunUrl(turnId: string, projectUrl: string | undefined): string | null {
  if (!projectUrl) return null;
  let url: URL;
  try {
    url = new URL(projectUrl);
  } catch {
    return null;
  }
  if (url.protocol !== "https:") return null;
  url.search = "";
  url.hash = "";
  return `${url.toString().replace(/\/+$/, "")}/r/${encodeURIComponent(turnId)}?poll=true`;
}
