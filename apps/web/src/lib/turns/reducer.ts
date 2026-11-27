/**
 * What: the pure reducer from doc 07 s6.2 SSE events to conversation state (turns, steps, gates,
 *   answer text, cards, session facts).
 * Use when: applying every streamed or replayed event; the Zustand store wraps it.
 * Inputs: the previous ConversationState and one SseEvent.
 * Output: the next ConversationState (the same object when the event is ignored).
 * Side effects: none (no I/O, no mutation of its inputs).
 * Role: single source of truth for what an event means in the UI, shared by chat and voice.
 */
import type {
  ActionResultEvent,
  ConfirmationRequiredEvent,
  ErrorEvent,
  EscalatedEvent,
  GateEvent,
  ModelFallbackEvent,
  PendingPublic,
  SseEvent,
  StatusEvent,
  StepCompletedEvent,
  StepStartedEvent,
  TurnCompletedEvent,
  TurnStartedEvent,
  VerificationRequiredEvent,
  VerificationResultEvent,
  VerifiedPerson,
} from "@contracts/contracts";

/**
 * What: one step chip: its start and end events and the gates attached to it.
 * Use when: rendering step chips and their expanded rows (CS-E-04.S5.T1).
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view model; `started` is null if step.completed arrived without its step.started.
 */
export interface StepView {
  stepId: string;
  started: StepStartedEvent | null;
  completed: StepCompletedEvent | null;
  gates: GateEvent[];
}

/**
 * What: everything one turn has streamed so far.
 * Use when: rendering one assistant message, its chips, cards and the drawer.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view model. `phase` is "completed" after the one accepted turn.completed, "failed" when the
 *   stream ended without one (drop, abort, watchdog, unreadable event; see failTurn); `text` is the
 *   streamed tokens, replaced by final.text at completion (deterministic lines arrive only there);
 *   `status` is the live status line, cleared by the first token and by completion.
 */
export interface TurnView {
  turnId: string;
  sessionId: string;
  traceId: string;
  mode: "chat" | "voice";
  lastSeq: number;
  phase: "streaming" | "completed" | "failed";
  started: TurnStartedEvent | null;
  stepOrder: string[];
  steps: Record<string, StepView>;
  gates: GateEvent[];
  status: StatusEvent | null;
  text: string;
  verificationRequired: VerificationRequiredEvent | null;
  verificationResult: VerificationResultEvent | null;
  confirmation: ConfirmationRequiredEvent | null;
  actionResults: ActionResultEvent[];
  escalation: EscalatedEvent | null;
  fallbacks: ModelFallbackEvent[];
  errors: ErrorEvent[];
  completed: TurnCompletedEvent | null;
  failure: TurnFailure | null;
}

/**
 * What: why a turn's stream ended without turn.completed: a NetworkError reason, or "contract"
 *   for an event the client could not read.
 * Use when: calling failTurn.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: client-side terminal reasons (doc 07 s8.1 browser watchdogs; no automatic retry).
 */
export type TurnFailureReason = "network" | "dropped" | "no_bytes_timeout" | "turn_timeout" | "aborted" | "contract";

/**
 * What: the terminal failure recorded on a turn that never completed.
 * Use when: rendering the "Couldn't finish" trail and the error row.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view model. `retryable` is always true: the browser never retries by itself (doc 07 s8.1),
 *   the customer may send again.
 */
export interface TurnFailure {
  reason: TurnFailureReason;
  retryable: boolean;
  message: string;
}

/**
 * What: plain words for each failure reason.
 * Use when: failTurn builds the failure.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (does not claim whether anything changed; the server's receipt says that).
 */
const FAILURE_MESSAGES: Readonly<Record<TurnFailureReason, string>> = Object.freeze({
  network: "The connection was lost before this reply finished.",
  dropped: "The connection dropped before this reply finished.",
  no_bytes_timeout: "The reply stopped arriving.",
  turn_timeout: "This reply took too long.",
  aborted: "This reply was stopped.",
  contract: "The reply could not be read.",
});

/**
 * What: the conversation of one session as the UI sees it.
 * Use when: reading store state.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: view model. Session facts: `verified` (verification.result verified), `verifyLocked`,
 *   `awaitingVerification` (the open card request), `pendingAction` (set by confirmation.required,
 *   then set to turn.completed final.pending_action, the server's word), `ticketOffer` (an
 *   escalation with offer_ticket_button, cleared when the next turn starts).
 */
export interface ConversationState {
  sessionId: string | null;
  turnOrder: string[];
  turns: Record<string, TurnView>;
  verified: VerifiedPerson | null;
  verifyLocked: boolean;
  awaitingVerification: VerificationRequiredEvent | null;
  pendingAction: PendingPublic | null;
  ticketOffer: EscalatedEvent | null;
}

/**
 * What: the empty conversation.
 * Use when: a new session starts, or a test replays from scratch.
 * Inputs: none.
 * Output: a frozen ConversationState.
 * Side effects: none.
 * Role: constant initial state.
 */
export const initialConversationState: ConversationState = Object.freeze({
  sessionId: null,
  turnOrder: [],
  turns: {},
  verified: null,
  verifyLocked: false,
  awaitingVerification: null,
  pendingAction: null,
  ticketOffer: null,
}) as ConversationState;

/**
 * What: a fresh TurnView from an event's envelope.
 * Use when: the first event of a turn arrives (normally turn.started).
 * Inputs: the event.
 * Output: a TurnView with lastSeq 0.
 * Side effects: none.
 * Role: helper.
 */
function newTurn(ev: SseEvent): TurnView {
  return {
    turnId: ev.turn_id,
    sessionId: ev.session_id,
    traceId: ev.trace_id,
    mode: ev.mode,
    lastSeq: 0,
    phase: "streaming",
    started: null,
    stepOrder: [],
    steps: {},
    gates: [],
    status: null,
    text: "",
    verificationRequired: null,
    verificationResult: null,
    confirmation: null,
    actionResults: [],
    escalation: null,
    fallbacks: [],
    errors: [],
    completed: null,
    failure: null,
  };
}

/**
 * What: returns the turn with one step replaced (created if absent, appended to stepOrder).
 * Use when: step.started, step.completed or gate touch a step.
 * Inputs: the turn, the step id, an updater from the old StepView to the new one.
 * Output: a new TurnView.
 * Side effects: none.
 * Role: helper.
 */
function withStep(turn: TurnView, stepId: string, update: (s: StepView) => StepView): TurnView {
  const prev = turn.steps[stepId];
  const base: StepView = prev ?? { stepId, started: null, completed: null, gates: [] };
  return {
    ...turn,
    stepOrder: prev ? turn.stepOrder : [...turn.stepOrder, stepId],
    steps: { ...turn.steps, [stepId]: update(base) },
  };
}

/**
 * What: the per-turn effect of one event.
 * Use when: reduceEvent accepted the event.
 * Inputs: the turn (lastSeq already advanced), the event.
 * Output: the new TurnView.
 * Side effects: none.
 * Role: one case per doc 07 s6.2 event type; the switch is exhaustive over SseEvent.
 */
function applyToTurn(turn: TurnView, ev: SseEvent): TurnView {
  switch (ev.type) {
    case "turn.started":
      return { ...turn, started: ev };
    case "step.started":
      return withStep(turn, ev.step_id, (s) => ({ ...s, started: ev }));
    case "step.completed":
      return withStep(turn, ev.step_id, (s) => ({ ...s, completed: ev }));
    case "gate":
      return { ...withStep(turn, ev.step_id, (s) => ({ ...s, gates: [...s.gates, ev] })), gates: [...turn.gates, ev] };
    case "status":
      return { ...turn, status: ev };
    case "token":
      return { ...turn, text: turn.text + ev.text, status: null };
    case "verification.required":
      return { ...turn, verificationRequired: ev };
    case "verification.result":
      return { ...turn, verificationResult: ev };
    case "confirmation.required":
      return { ...turn, confirmation: ev };
    case "action.result":
      return { ...turn, actionResults: [...turn.actionResults, ev] };
    case "escalated":
      return { ...turn, escalation: ev };
    case "model.fallback":
      return { ...turn, fallbacks: [...turn.fallbacks, ev] };
    case "error":
      return { ...turn, errors: [...turn.errors, ev] };
    case "turn.completed":
      return { ...turn, completed: ev, phase: "completed", status: null, text: ev.final.text };
    default: {
      const unreachable: never = ev;
      return unreachable;
    }
  }
}

/**
 * What: the session-level effect of one event.
 * Use when: reduceEvent accepted the event.
 * Inputs: the conversation (turn already updated), the event.
 * Output: the new ConversationState.
 * Side effects: none.
 * Role: keeps the verification card, verified person, pending action and ticket offer in step.
 */
function applyToSession(state: ConversationState, ev: SseEvent): ConversationState {
  switch (ev.type) {
    case "turn.started":
      return state.ticketOffer === null ? state : { ...state, ticketOffer: null };
    case "verification.required":
      return { ...state, awaitingVerification: ev };
    case "verification.result":
      if (ev.status === "verified") return { ...state, verified: ev.person ?? null, awaitingVerification: null };
      if (ev.status === "locked") return { ...state, verifyLocked: true, awaitingVerification: null };
      if (ev.status === "conflict") return { ...state, awaitingVerification: null };
      return state;
    case "confirmation.required":
      return { ...state, pendingAction: ev.pending };
    case "escalated":
      return ev.offer_ticket_button ? { ...state, ticketOffer: ev } : state;
    case "turn.completed":
      return { ...state, pendingAction: ev.final.pending_action ?? null };
    default:
      return state;
  }
}

/**
 * What: applies one SSE event to the conversation.
 * Use when: every event from streamTurn, the voice relay or a replay.
 * Inputs: the previous state, the event.
 * Output: the next state, or the same object when the event is ignored: another session's event,
 *   a seq not above the turn's last applied seq (duplicate or out of order), or any event for a
 *   turn that already accepted its one turn.completed or was failed.
 * Side effects: none.
 * Role: the reducer.
 */
export function reduceEvent(state: ConversationState, ev: SseEvent): ConversationState {
  if (state.sessionId !== null && ev.session_id !== state.sessionId) return state;
  const prev = state.turns[ev.turn_id];
  if (prev !== undefined && (prev.phase !== "streaming" || ev.seq <= prev.lastSeq)) return state;
  const base = prev ?? newTurn(ev);
  const turn = applyToTurn({ ...base, lastSeq: ev.seq }, ev);
  const next: ConversationState = {
    ...state,
    sessionId: state.sessionId ?? ev.session_id,
    turnOrder: prev ? state.turnOrder : [...state.turnOrder, ev.turn_id],
    turns: { ...state.turns, [ev.turn_id]: turn },
  };
  return applyToSession(next, ev);
}

/**
 * What: folds a list of events into a state.
 * Use when: replaying fixtures, or rehydrating from stored events.
 * Inputs: events in arrival order, the start state (default the empty conversation).
 * Output: the final state.
 * Side effects: none.
 * Role: convenience over reduceEvent.
 */
export function replayEvents(events: readonly SseEvent[], start: ConversationState = initialConversationState): ConversationState {
  return events.reduce(reduceEvent, start);
}

/**
 * What: moves a turn whose stream ended without turn.completed to the terminal "failed" phase.
 * Use when: sendTurn (or the voice relay) rejects after the turn started: drop, abort, watchdog
 *   expiry or an unreadable event (review PR #16, I1).
 * Inputs: the state, the turn id, the failure reason.
 * Output: the next state; the same object when the turn is unknown or not streaming.
 * Side effects: none.
 * Role: local reducer input (not an SSE event); clears the live status so no step stays live.
 */
export function failTurn(state: ConversationState, turnId: string, reason: TurnFailureReason): ConversationState {
  const prev = state.turns[turnId];
  if (prev === undefined || prev.phase !== "streaming") return state;
  const failure: TurnFailure = { reason, retryable: true, message: FAILURE_MESSAGES[reason] };
  return { ...state, turns: { ...state.turns, [turnId]: { ...prev, phase: "failed", status: null, failure } } };
}
