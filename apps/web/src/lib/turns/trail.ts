/**
 * What: pure view helpers for the step trail: the summary line, one row per step, gate scores in
 *   words, and the outcome phrase of a turn.
 * Use when: StepTrajectory renders a TurnView (CS-E-04.S5.T1).
 * Inputs: TurnView and StepView from the reducer; GateEvent.
 * Output: plain strings and small view records.
 * Side effects: none.
 * Role: the trail's copy rules in one tested place (doc 06 s16, F1 s4.1 and s4.2, doc 07 s6.2);
 *   React only lays them out.
 */
import type { GateEvent } from "@contracts/contracts";
import type { StepView, TurnView } from "./reducer";

/**
 * What: a turn duration as seconds with one decimal ("2.3 s").
 * Use when: the settled summary line.
 * Inputs: milliseconds.
 * Output: a string.
 * Side effects: none.
 * Role: formatter.
 */
export function formatSeconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`;
}

/**
 * What: a step latency as seconds with two decimals ("0.31 s"), at least 0.01 s.
 * Use when: the right-aligned latency of a step row.
 * Inputs: milliseconds.
 * Output: a string.
 * Side effects: none.
 * Role: formatter.
 */
export function formatStepLatency(ms: number): string {
  return `${(Math.max(ms, 10) / 1000).toFixed(2)} s`;
}

/**
 * What: a score with two decimals.
 * Use when: gate words.
 * Inputs: a number.
 * Output: a string such as "0.41".
 * Side effects: none.
 * Role: formatter.
 */
const score = (n: number): string => n.toFixed(2);

/**
 * What: the color role a piece of trail text uses; always paired with words.
 * Use when: styling a row detail or meter.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: maps to DESIGN.md tokens (muted, ember, success, warning, error).
 */
export type TrailTone = "muted" | "ember" | "success" | "warning" | "error";

/**
 * What: a gate in words: short visible text and a full accessible label.
 * Use when: a step row carries a gate.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record.
 */
export interface GateWords {
  text: string;
  label: string;
}

/**
 * What: past-tense words for grounding verdicts.
 * Use when: gateWords for a grounding gate.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table.
 */
const GROUNDING_WORDS: Readonly<Record<string, string>> = Object.freeze({
  pass: "passed",
  fail: "failed",
  skipped: "skipped",
  error: "could not run",
});

/**
 * What: one gate as words. Retrieval: "score / threshold, decision", where the threshold is
 *   tau_high when the score reached tau_low (the band it missed or met), else tau_low; e.g.
 *   "0.41 / 0.45, escalate". Grounding: "grounding passed, 1 of 1 claims".
 * Use when: a step row or an accessible label shows a gate.
 * Inputs: the gate event.
 * Output: { text, label }.
 * Side effects: none.
 * Role: doc 07 s6.2 gate fields and s6.5 routing rendered as text, never color alone.
 */
export function gateWords(g: GateEvent): GateWords {
  if (g.gate === "grounding") {
    const verdict = GROUNDING_WORDS[g.verdict] ?? g.verdict;
    const claims = g.claims_total != null && g.claims_total > 0 ? `, ${g.claims_supported ?? 0} of ${g.claims_total} claims` : "";
    const text = `grounding ${verdict}${claims}`;
    return { text, label: `Answer check against policy ${verdict}${claims}` };
  }
  const top = g.top_cosine;
  if (top == null) {
    return { text: `${g.verdict}, ${g.decision}`, label: `Retrieval ${g.verdict}: ${g.decision}` };
  }
  const low = g.tau_low ?? null;
  const high = g.tau_high ?? null;
  const threshold = low !== null && top < low ? low : (high ?? low);
  const text = threshold === null ? `${score(top)}, ${g.verdict}, ${g.decision}` : `${score(top)} / ${score(threshold)}, ${g.decision}`;
  const parts = [`Retrieval score ${score(top)}`];
  if (high !== null) parts.push(`answer threshold ${score(high)}`);
  if (low !== null) parts.push(`floor ${score(low)}`);
  return { text, label: `${parts.join(", ")}: ${g.verdict}, ${g.decision}` };
}

/**
 * What: the icon role of a step, chosen from its name and kind.
 * Use when: StepTrajectory picks a lucide icon.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: keeps icon choice testable without React.
 */
export type StepIcon =
  | "verify"
  | "account"
  | "billing"
  | "search"
  | "wallet"
  | "commit"
  | "escalation"
  | "ticket"
  | "model"
  | "ask"
  | "other";

/**
 * What: the icon for a step (F1 s4.1 icon list).
 * Use when: stepRow.
 * Inputs: the step's name and kind (either may be missing if step.started was lost).
 * Output: a StepIcon.
 * Side effects: none.
 * Role: mapping.
 */
function iconFor(name: string | undefined, kind: string | undefined): StepIcon {
  if (name === "search_kb") return "search";
  if (name === "list_charges") return "billing";
  if (name === "get_account_summary" || kind === "memory") return "account";
  if (name === "request_verification") return "ask";
  if (name === "create_ticket") return "ticket";
  if (name?.startsWith("prepare_")) return "wallet";
  switch (kind) {
    case "verify":
      return "verify";
    case "commit":
    case "submit":
    case "cancel":
      return "commit";
    case "escalation":
      return "escalation";
    case "model":
      return "model";
    default:
      return "other";
  }
}

/**
 * What: the retrieval meter for a row: score and both thresholds on a 0 to 1 bar.
 * Use when: a row carries a retrieval gate with a score.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record.
 */
export interface GateMeter {
  score: number;
  tauLow: number | null;
  tauHigh: number | null;
  tone: TrailTone;
}

/**
 * What: one rendered step row.
 * Use when: StepTrajectory lays out the expanded trail.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record. `detail` is shown in mono; `statusWords` is the screen-reader status.
 */
export interface StepRowView {
  id: string;
  icon: StepIcon;
  title: string;
  detail: string;
  detailLabel: string | null;
  tone: TrailTone;
  latency: string;
  running: boolean;
  statusWords: string;
  meter: GateMeter | null;
}

/**
 * What: the step's own detail when it completed ok and has no gate: the rule result for a
 *   prepare step, the model for a model step, else the backend's detail.
 * Use when: stepRow.
 * Inputs: the StepView.
 * Output: { detail, tone }.
 * Side effects: none.
 * Role: helper.
 */
function okDetail(step: StepView): { detail: string; tone: TrailTone } {
  const done = step.completed;
  const rule = done?.rule;
  if (rule) {
    const tone: TrailTone = rule.decision === "declined" ? "error" : rule.decision === "needs_approval" ? "warning" : "muted";
    return { detail: `${rule.decision.replace("_", " ")} · ${rule.reason_code} · ${rule.policy_ref.article} §${rule.policy_ref.section}`, tone };
  }
  if (done?.model) return { detail: done.model, tone: "muted" };
  return { detail: done?.detail ?? "", tone: "muted" };
}

/**
 * What: one step as a row: title (past tense once completed, present tense with an ellipsis
 *   while running), detail, tone, latency, and the gate words and meter when a gate is attached.
 * Use when: rendering the expanded trail.
 * Inputs: the StepView and its TurnView (a failed turn stops a running step).
 * Output: StepRowView.
 * Side effects: none.
 * Role: F1 s4.2 row rules.
 */
export function stepRow(step: StepView, turn: TurnView): StepRowView {
  const done = step.completed;
  const started = step.started;
  const base = { id: step.stepId, icon: iconFor(started?.name, started?.kind), meter: null as GateMeter | null, detailLabel: null as string | null };
  if (done === null) {
    const title = `${started?.label ?? "Working"}...`;
    if (turn.phase !== "streaming") {
      return { ...base, title, detail: "stopped", tone: "error", latency: "", running: false, statusWords: "stopped" };
    }
    return { ...base, title, detail: "running", tone: "ember", latency: "", running: true, statusWords: "running" };
  }
  const latency = formatStepLatency(done.ms);
  const title = done.title;
  const why = done.detail ?? "";
  switch (done.status) {
    case "retrying":
      return { ...base, title, detail: why ? `retrying: ${why}` : `retrying, attempt ${done.attempt + 1}`, tone: "warning", latency, running: false, statusWords: "retrying" };
    case "error":
      return { ...base, title, detail: why ? `failed: ${why}` : "failed", tone: "error", latency, running: false, statusWords: "failed" };
    case "timeout":
      return { ...base, title, detail: "timed out", tone: "error", latency, running: false, statusWords: "timed out" };
    case "blocked":
      return { ...base, title, detail: why || "blocked by policy", tone: "warning", latency, running: false, statusWords: "blocked" };
    case "skipped":
      return { ...base, title, detail: "skipped", tone: "muted", latency, running: false, statusWords: "skipped" };
    default:
      break;
  }
  const retrieval = step.gates.find((g) => g.gate === "retrieval");
  const grounding = step.gates.find((g) => g.gate === "grounding");
  if (retrieval) {
    const w = gateWords(retrieval);
    const meter: GateMeter | null =
      retrieval.top_cosine == null
        ? null
        : {
            score: retrieval.top_cosine,
            tauLow: retrieval.tau_low ?? null,
            tauHigh: retrieval.tau_high ?? null,
            tone: retrieval.verdict === "pass" ? "success" : retrieval.verdict === "weak" || retrieval.verdict === "uncalibrated" ? "warning" : "error",
          };
    return { ...base, title, detail: w.text, detailLabel: w.label, tone: "muted", latency, running: false, statusWords: "done", meter };
  }
  const own = okDetail(step);
  if (grounding) {
    const w = gateWords(grounding);
    const detail = own.detail ? `${own.detail} · ${w.text}` : w.text;
    const tone: TrailTone = grounding.verdict === "fail" || grounding.verdict === "error" ? "error" : own.tone;
    return { ...base, title, detail, detailLabel: w.label, tone, latency, running: false, statusWords: "done" };
  }
  return { ...base, title, detail: own.detail, tone: own.tone, latency, running: false, statusWords: "done" };
}

/**
 * What: short past-tense phrases for a committed action, by action type.
 * Use when: outcomePhrase for end_reason committed.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table.
 */
const COMMITTED: Readonly<Record<string, string>> = Object.freeze({
  refund: "Refund issued",
  billing_email: "Billing email changed",
  grace_extension: "Grace period extended",
  notification_prefs: "Notifications updated",
});

/**
 * What: phrases for end reasons that need no event lookup.
 * Use when: outcomePhrase.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (doc 07 s6.2 EndReason).
 */
const END_REASON: Readonly<Record<string, string>> = Object.freeze({
  answered: "Answered",
  clarify: "Asked to clarify",
  verification_requested: "Verification requested",
  verified: "Verified",
  submitted: "Sent for approval",
  cancelled: "Cancelled",
  escalated: "Escalated",
  declined_off_topic: "Declined, off topic",
  cap_reached: "Limit reached",
  limit_reached: "Limit reached",
  error: "Couldn't finish",
  swept: "Expired",
});

/**
 * What: the outcome phrase of a turn ("Refund ready to confirm", "Escalated", ...).
 * Use when: the settled summary line.
 * Inputs: the TurnView.
 * Output: a phrase, or "" while still streaming.
 * Side effects: none.
 * Role: F1 s4.1 outcome phrase from the last decisive event.
 */
export function outcomePhrase(turn: TurnView): string {
  if (turn.phase === "failed") return "Couldn't finish";
  const done = turn.completed;
  if (done === null) return "";
  switch (done.end_reason) {
    case "readback": {
      const label = turn.confirmation?.confirm_label ?? "";
      if (label.startsWith("Confirm ")) {
        const what = label.slice("Confirm ".length);
        return `${what.charAt(0).toUpperCase()}${what.slice(1)} ready to confirm`;
      }
      return label ? `Ready: ${label.toLowerCase()}` : "Ready to confirm";
    }
    case "committed": {
      const result = turn.actionResults.find((r) => r.outcome === "committed" || r.outcome === "act_alone_applied");
      return (result && COMMITTED[result.action_type]) ?? "Done";
    }
    case "deadline":
      return done.final.escalated ? "Escalated" : "Couldn't finish";
    default:
      return END_REASON[done.end_reason] ?? "Done";
  }
}

/**
 * What: the trail's summary line state.
 * Use when: StepTrajectory renders its disclosure button.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view record.
 */
export interface TrailSummary {
  state: "running" | "done" | "failed";
  text: string;
  stepCount: number;
  failedCount: number;
}

/**
 * What: the summary line: while running, the live status message or the latest running step's
 *   label ("Checking your billing history..."), else "Working..."; once settled,
 *   "n steps[, k failed], s · outcome"; a failed turn reads "n steps · Couldn't finish".
 * Use when: StepTrajectory renders.
 * Inputs: the TurnView.
 * Output: TrailSummary.
 * Side effects: none.
 * Role: F1 s4.1 summary rules.
 */
export function trailSummary(turn: TurnView): TrailSummary {
  const steps = turn.stepOrder.map((id) => turn.steps[id]).filter((s): s is StepView => s !== undefined);
  const stepCount = steps.length;
  const failedCount = steps.filter((s) => s.completed?.status === "error" || s.completed?.status === "timeout").length;
  const count = `${stepCount} ${stepCount === 1 ? "step" : "steps"}`;
  if (turn.phase === "streaming") {
    const running = [...steps].reverse().find((s) => s.completed === null && s.started !== null);
    const text = turn.status?.message ?? (running?.started ? `${running.started.label}...` : "Working...");
    return { state: "running", text, stepCount, failedCount };
  }
  if (turn.phase === "failed") return { state: "failed", text: `${count} · ${outcomePhrase(turn)}`, stepCount, failedCount };
  const failed = failedCount > 0 ? `, ${failedCount} failed` : "";
  const latency = turn.completed ? `, ${formatSeconds(turn.completed.usage.latency_ms)}` : "";
  return { state: "done", text: `${count}${failed}${latency} · ${outcomePhrase(turn)}`, stepCount, failedCount };
}
