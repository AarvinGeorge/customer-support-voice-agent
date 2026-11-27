/**
 * GENERATED FILE. Do not edit. Source: packages/contracts/src/contracts (doc 07 r4).
 * Regenerate: cd packages/contracts && uv run python scripts/gen_ts.py
 * What: TypeScript types and constants for every contract shape the web app sends or reads.
 * Use when: typing API bodies, SSE events and limits in apps/web.
 * Inputs: none.
 * Output: interfaces, the SseEvent union, literal unions and constants.
 * Side effects: none.
 * Role: web mirror of packages/contracts; CI fails if it drifts from the Python models.
 */

export const CONTRACT_VERSION = "2026-11-26.r4";

/** What: body of POST /api/access. Use when: the visitor enters the demo code. */
export interface AccessRequest {
  code: string;
}

/** What: one committed action in the last 90 days. Use when: MeContext.recent_actions. */
export interface ActionHistoryItem {
  action_type: "refund" | "billing_email" | "grace_extension" | "notification_prefs";
  label: string;
  effective_date: string;
  committed_by: "customer" | "operator";
  amount_cents?: number | null;
}

/** What: outcome of a commit, submit, cancel or decline. Use when: after ingress confirm code. */
export interface ActionResultEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "action.result";
  pending_action_id?: string | null;
  action_type: string;
  outcome: "committed" | "submitted" | "cancelled" | "declined" | "rules_changed" | "expired" | "failed" | "unconfirmed" | "step_up_failed" | "step_up_locked" | "act_alone_applied";
  changed?: boolean | null;
  reason_codes: Array<string>;
  policy_refs: Array<PolicyRef>;
  result_text: string;
  ticket?: TicketRef | null;
}

/** What: body of GET /api/readyz. Use when: readiness checks. */
export interface AgentReady {
  status: string;
  contract_version: string;
  redis: string;
  postgres: string;
  core: string;
  knowledge: string;
  demo_faults: boolean;
}

/** What: arguments the Realtime model sends to ask_support_agent. Use when: the browser relays. */
export interface AskSupportAgentArgs {
  question: string;
}

/** What: result of a cancel. Use when: cancel 200. Inputs: id, state, changed. */
export interface CancelResult {
  pending_action_id: string;
  state: "cancelled";
  changed: false;
}

/** What: charge reference by last four digits and optional date. */
export interface ChargeRefEnding {
  ending: string;
  on?: string | null;
}

/** What: charge reference by id. Use when: prepare_refund had a charge_id. */
export interface ChargeRefId {
  id: string;
}

/** What: one billing charge as the agent sees it. Use when: list_charges, ambiguity candidates. */
export interface ChargeView {
  charge_id: string;
  invoice_number?: string | null;
  label: string;
  charged_at: string;
  charge_date: string;
  amount_cents: number;
  currency: string;
  description: string;
  status: "paid" | "failed" | "refunded";
  refunded_at?: string | null;
}

/** What: body of GET /v1/me/charges. Use when: list_charges. Inputs: charges, ambiguous. */
export interface ChargesResponse {
  charges: Array<ChargeView>;
  ambiguous: boolean;
}

/** What: one search hit with scores and relevance. Use when: SearchResponse.chunks. */
export interface Chunk {
  chunk_id: string;
  article_slug: string;
  article_title: string;
  section_path: Array<string>;
  text: string;
  tiers: Array<string>;
  error_codes: Array<string>;
  flags: Array<"injection_suspect">;
  last_updated: string;
  doc_version: number;
  content_hash: string;
  source_url: string;
  citation: ChunkCitation;
  scores: ChunkScores;
  relevant: boolean;
}

/** What: how to cite a chunk in text and speech. Use when: Chunk.citation. */
export interface ChunkCitation {
  ref: string;
  label: string;
  spoken: string;
}

/** What: one chunk without scores (GET /v1/chunks/{id}). Use when: the drawer chunk proxy. */
export interface ChunkPublic {
  chunk_id: string;
  article_slug: string;
  article_title: string;
  section_path: Array<string>;
  text: string;
  tiers: Array<string>;
  error_codes: Array<string>;
  flags: Array<"injection_suspect">;
  last_updated: string;
  doc_version: number;
  content_hash: string;
  source_url: string;
  citation: ChunkCitation;
}

/** What: ranking scores of one chunk. Use when: Chunk.scores, drawer display. */
export interface ChunkScores {
  cosine: number;
  vector_rank?: number | null;
  keyword_rank?: number | null;
  rrf_score: number;
  fused_rank: number;
}

/** What: one cited help-article chunk. Use when: FinalAnswer.citations. */
export interface Citation {
  chunk_id: string;
  label: string;
  ref: string;
  spoken: string;
  url: string;
  section_title: string;
  excerpt: string;
  score: number;
}

/** What: the verified identity core holds for one agent session. */
export interface Claims {
  verified: true;
  session_id: string;
  user_id: string;
  display_name: string;
  account_id: string;
  account_name: string;
  role: "owner" | "billing_admin" | "member";
  tier: "free" | "pro" | "business" | "enterprise";
  contract_status: "trialing" | "active" | "past_due" | "in_notice" | "cancelled";
  grace_until?: string | null;
  permissions: Permissions;
  expires_at: string;
}

/** What: result of a commit (or operator approve). Use when: commit 200, PendingStatus.result. */
export interface CommitResult {
  status: "committed";
  pending_action_id: string;
  replayed: boolean;
  changed: true;
  summary: string;
  summary_say: string;
  details: Record<string, unknown>;
}

/** What: Gate 1 verdict and its evidence. Use when: SearchResponse.confidence, gate event. */
export interface Confidence {
  verdict: "pass" | "weak" | "none" | "error" | "uncalibrated";
  reason: "exact_code_match" | "above_threshold" | "weak_match" | "below_threshold" | "unknown_error_code" | "embedding_unavailable" | "retrieval_unavailable" | "no_calibration";
  top_cosine?: number | null;
  tau_low?: number | null;
  tau_high?: number | null;
  support_count: number;
  code_match: boolean;
  legs: Legs;
  calibration_id?: string | null;
  calibration_status: "calibrated" | "provisional" | "none";
  calibration_stale: boolean;
}

/** What: a Confirm or Cancel press on the confirmation card. */
export interface ConfirmInput {
  pending_action_id: string;
  decision: "confirm" | "cancel";
  step_up_pin?: string | null;
}

/** What: show the confirmation card for a pending action. Use when: route readback. */
export interface ConfirmationRequiredEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "confirmation.required";
  pending: PendingPublic;
  confirm_label: string;
}

/** What: body of core GET /readyz. Use when: readiness checks. */
export interface CoreReady {
  status: string;
  contract_version: string;
  policy_version: string;
  demo_faults: boolean;
  seed_date: string;
}

/** What: arguments of create_ticket. Use when: the customer asks for a human, a commitment is */
export interface CreateTicketArgs {
  category: "billing" | "refund" | "access" | "sso" | "integration" | "bug" | "account_change" | "commitment" | "human_requested" | "other";
  summary: string;
  steps_tried?: Array<string>;
  blocking?: boolean;
}

/** What: fields on every SSE event. Use when: base of every event class. */
export interface Envelope {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
}

/** What: the body of every error (JSON key "class" maps to class_). */
export interface ErrorBody {
  "class": "provider_timeout" | "provider_rate_limited" | "provider_spend_cap" | "provider_5xx" | "provider_auth" | "retrieval_failed" | "core_client_error" | "core_conflict" | "core_unavailable" | "db_unavailable" | "redis_unavailable" | "rate_limited" | "admission_rejected" | "turn_deadline_exceeded" | "access_denied" | "not_found" | "validation";
  code: string;
  message: string;
  retryable: boolean;
  changed: boolean | null;
  retry_after_s?: number | null;
  fault_injected: boolean;
  trace_id?: string | null;
  details: Record<string, unknown>;
}

/** What: wrapper {"error": ErrorBody}. */
export interface ErrorEnvelope {
  error: ErrorBody;
}

/** What: an error inside the stream (always followed by turn.completed). */
export interface ErrorEvent {
  "class": "provider_timeout" | "provider_rate_limited" | "provider_spend_cap" | "provider_5xx" | "provider_auth" | "retrieval_failed" | "core_client_error" | "core_conflict" | "core_unavailable" | "db_unavailable" | "redis_unavailable" | "rate_limited" | "admission_rejected" | "turn_deadline_exceeded" | "access_denied" | "not_found" | "validation";
  code: string;
  message: string;
  retryable: boolean;
  changed: boolean | null;
  retry_after_s?: number | null;
  fault_injected: boolean;
  trace_id: string;
  details: Record<string, unknown>;
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "error";
}

/** What: the turn escalated to a human. Use when: any escalate route or core-created ticket. */
export interface EscalatedEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "escalated";
  reason: "not_in_docs" | "grounding_failed" | "retrieval_unavailable" | "tool_failure" | "limit_reached" | "deadline" | "verify_locked" | "needs_commitment" | "customer_asked_human" | "model_unavailable";
  ticket?: TicketRef | null;
  origin: "code" | "model" | "core";
  offer_ticket_button: boolean;
}

/** What: the turn's answer for chat (text) and voice (say). Use when: turn.completed. */
export interface FinalAnswer {
  text: string;
  say: string;
  citations: Array<Citation>;
  escalated: boolean;
  ticket?: TicketRef | null;
  pending_action?: PendingPublic | null;
  confidence?: FinalConfidence | null;
}

/** What: gate summary on the final answer. Use when: FinalAnswer.confidence. */
export interface FinalConfidence {
  retrieval_verdict?: string | null;
  top_cosine?: number | null;
  tau_low?: number | null;
  tau_high?: number | null;
  grounding: "pass" | "fail" | "skipped" | "not_run";
}

/** What: the JSON the browser returns to Realtime as function_call_output. */
export interface FunctionCallOutput {
  say: string;
  escalated: boolean;
  ticket?: SpokenTicket | null;
  awaiting?: "confirm" | "verification" | null;
}

/** What: one claim checked by Gate 2. Use when: gate event claims list. */
export interface GateClaim {
  text: string;
  supported: boolean;
  source_id?: string | null;
}

/** What: a Gate 1 (retrieval) or Gate 2 (grounding) decision. Use when: after search or draft. */
export interface GateEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "gate";
  step_id: string;
  gate: "retrieval" | "grounding";
  verdict: string;
  decision: "answer" | "clarify" | "escalate" | "decline" | "skip";
  top_cosine?: number | null;
  tau_low?: number | null;
  tau_high?: number | null;
  calibration_status?: string | null;
  code_match?: boolean | null;
  chunk_ids: Array<string>;
  claims_total?: number | null;
  claims_supported?: number | null;
  claims?: Array<GateClaim> | null;
  commitment?: boolean | null;
}

/** What: arguments of get_account_summary (none). Use when: the model reads the profile. */
export interface GetAccountSummaryArgs {
}

/** What: status of the vector and keyword legs. Use when: Confidence.legs. */
export interface Legs {
  vector: "ok" | "empty" | "failed";
  keyword: "ok" | "empty" | "failed";
}

/** What: arguments of list_charges. Use when: a billing role asks about charges. */
export interface ListChargesArgs {
  ending?: string | null;
  on?: string | null;
  since_days?: number;
}

/** What: arguments of list_tickets. Use when: the customer asks about tickets. */
export interface ListTicketsArgs {
  status?: "open" | "all";
  ending?: string | null;
}

/** What: body of GET /api/me. Use when: the web app boots. Inputs: flags. */
export interface Me {
  visitor: true;
  operator: boolean;
  demo_faults_enabled: boolean;
}

/** What: account memory the agent loads at verify and every turn start. */
export interface MeContext {
  claims: Claims;
  profile: Profile;
  open_tickets: Array<TicketRef>;
  recent_actions: Array<ActionHistoryItem>;
  pending_action?: PendingPublic | null;
  generated_at: string;
}

/** What: the fallback model took over. Use when: ModelFallbackMiddleware switches. */
export interface ModelFallbackEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "model.fallback";
  from_model: string;
  to_model: string;
  reason: "rate_limit" | "timeout" | "5xx" | "spend_cap" | "auth" | "fault_llm_429";
}

/** What: the customer-facing view of a pending action (confirmation card). */
export interface PendingPublic {
  pending_action_id: string;
  action_type: "refund" | "billing_email" | "grace_extension";
  kind: "commit" | "submit_for_approval";
  state: "prepared" | "awaiting_customer" | "awaiting_approval";
  readback_text: string;
  readback_say: string;
  amount_cents?: number | null;
  title: string;
  invoice_ref?: string | null;
  charge_date?: string | null;
  reason_code: string;
  requires_step_up: boolean;
  expires_at?: string | null;
  ticket?: TicketRef | null;
}

/** What: state of a pending action. Use when: status check after a commit or submit failure. */
export interface PendingStatus {
  pending_action_id: string;
  action_type: string;
  state: "prepared" | "awaiting_customer" | "awaiting_approval" | "committed" | "cancelled" | "expired" | "superseded" | "rejected" | "rules_changed";
  decision: "allowed" | "needs_approval";
  expires_at?: string | null;
  ticket?: TicketRef | null;
  result?: CommitResult | null;
}

/** What: what the verified person may do. Use when: inside Claims. */
export interface Permissions {
  billing_read: boolean;
  billing_actions: boolean;
  own_notification_prefs: boolean;
}

/** What: one demo persona on the landing page. Use when: GET /api/demo/personas. */
export interface Persona {
  name: string;
  role: "owner" | "billing_admin" | "member";
  tier: "free" | "pro" | "business" | "enterprise";
  contract_status: "trialing" | "active" | "past_due" | "in_notice" | "cancelled";
  account_id: string;
  pin: string;
  scenarios: Array<"S1" | "S2" | "S3" | "S4" | "S5" | "rules">;
  description: string;
}

/** What: pointer to the policy article section behind a decision. Use when: prepare results, */
export interface PolicyRef {
  article: string;
  section: string;
  title: string;
}

/** What: arguments of prepare_refund. Use when: the customer asks for a refund. */
export interface PrepareRefundArgs {
  charge_id?: string | null;
  ending?: string | null;
  on?: string | null;
  reason: "duplicate" | "service_issue" | "accidental" | "other";
  note: string;
}

/** What: core's decision on a prepare. Use when: any prepare response. */
export interface PrepareResult {
  decision: "allowed" | "needs_approval" | "declined";
  reason_codes: Array<string>;
  policy_refs: Array<PolicyRef>;
  message?: string | null;
  pending_action_id?: string | null;
  pending_kind?: "commit" | "submit_for_approval" | null;
  state?: "prepared" | "awaiting_customer" | null;
  expires_at?: string | null;
  requires_step_up: boolean;
  readback?: Readback | null;
  details: Record<string, unknown>;
  ticket?: TicketRef | null;
  policy_version: string;
  changed: false;
}

/** What: what a prepare tool returns to the model (before wrapping). Use when: ToolGuard wraps it. */
export interface PrepareToolResult {
  decision: "allowed" | "needs_approval" | "declined";
  reason_codes: Array<string>;
  message?: string | null;
  policy_refs: Array<PolicyRef>;
  ticket?: TicketRef | null;
}

/** What: account and person profile. Use when: get_account_summary, MeContext. */
export interface Profile {
  account_id: string;
  account_name: string;
  display_name: string;
  role: "owner" | "billing_admin" | "member";
  tier: "free" | "pro" | "business" | "enterprise";
  contract_status: "trialing" | "active" | "past_due" | "in_notice" | "cancelled";
  grace_until?: string | null;
  seats: number;
  billing_email_masked?: string | null;
  notification_prefs: Record<string, boolean>;
}

/** What: how knowledge read the query. Use when: SearchResponse.query. */
export interface QueryInfo {
  raw: string;
  normalized: string;
  codes: Array<string>;
  unknown_codes: Array<string>;
}

/** What: read-back text for chat and voice. Use when: PrepareResult.readback. */
export interface Readback {
  chat: string;
  voice: string;
}

/** What: body of POST /v1/actions/refund/prepare. Use when: prepare_refund wrapper. */
export interface RefundPrepareRequest {
  charge_ref: ChargeRefId | ChargeRefEnding | null;
  reason: "duplicate" | "service_issue" | "accidental" | "other";
  note: string;
}

/** What: arguments of request_verification. Use when: unverified and account data or an action */
export interface RequestVerificationArgs {
  reason: "account_lookup" | "action" | "ticket_status";
}

/** What: arguments of search_kb. Use when: the model searches help articles. */
export interface SearchKbArgs {
  query: string;
  error_code?: string | null;
}

/** What: what search_kb returns to the model (before wrapping). Use when: ToolGuard wraps it. */
export interface SearchKbToolResult {
  verdict: "pass" | "weak" | "none" | "error" | "uncalibrated";
  instruction?: string | null;
  chunks: Array<string>;
}

/** What: body of POST /v1/search. Use when: search_kb wrapper or calibration script. */
export interface SearchRequest {
  query: string;
  error_code?: string | null;
  tier: "public" | "free" | "pro" | "business" | "enterprise";
  verified: boolean;
  k?: number;
  calibration?: boolean;
}

/** What: body of a 200 from POST /v1/search. Use when: search_kb wrapper, drawer. */
export interface SearchResponse {
  request_id: string;
  trace_id: string;
  corpus_version: string;
  embedding_model: string;
  query: QueryInfo;
  allowed_tiers: Array<string>;
  chunks: Array<Chunk>;
  confidence: Confidence;
  timing_ms: TimingMs;
}

/** What: body of a 201 from POST /api/sessions. Use when: starting a conversation. */
export interface SessionCreated {
  session_id: string;
  created_at: string;
}

/** What: ticket reference for speech. Use when: FunctionCallOutput.ticket. */
export interface SpokenTicket {
  spoken: string;
  full_id: string;
}

/** What: a transient status line. Use when: slow model, retry, policy check. */
export interface StatusEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "status";
  kind: "slow" | "retrying" | "checking_policy";
  message: string;
}

/** What: a step chip ends. Use when: after each step. Inputs: status, ms, past-tense title, */
export interface StepCompletedEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "step.completed";
  step_id: string;
  status: "ok" | "error" | "retrying" | "blocked" | "timeout" | "skipped";
  ms: number;
  title: string;
  detail?: string | null;
  summary: Record<string, unknown>;
  attempt: number;
  model?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  rule?: StepRule | null;
}

/** What: the rule outcome shown on a prepare step. Use when: step.completed for prepare_*. */
export interface StepRule {
  decision: "allowed" | "needs_approval" | "declined";
  reason_code: string;
  policy_ref: PolicyRef;
}

/** What: a step chip starts. Use when: before a tool, model, gate or ingress step. */
export interface StepStartedEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "step.started";
  step_id: string;
  kind: "tool" | "model" | "gate" | "verify" | "commit" | "submit" | "cancel" | "escalation" | "repair" | "memory";
  name: string;
  label: string;
  args_redacted: Record<string, unknown>;
}

/** What: result of sending an action for approval. Use when: submit 200. */
export interface SubmitResult {
  status: "awaiting_approval";
  pending_action_id: string;
  replayed: boolean;
  changed: true;
  ticket: TicketRef;
  summary: string;
  summary_say: string;
}

/** What: body of POST /v1/tickets. Use when: create_ticket tool or agent code escalation. */
export interface TicketCreate {
  category: "billing" | "refund" | "access" | "sso" | "integration" | "bug" | "account_change" | "commitment" | "human_requested" | "other";
  summary: string;
  steps_tried?: Array<string>;
  blocking?: boolean;
  confidence?: number | null;
  transcript_ref?: string | null;
  origin: "model" | "code";
  escalation_reason?: string | null;
}

/** What: body of a 200 from POST /v1/tickets. Use when: ticket create. */
export interface TicketCreateResponse {
  ticket: TicketRef;
  merged: boolean;
  created: boolean;
}

/** What: the Create ticket button after "both models unavailable". */
export interface TicketOffer {
  accept: true;
}

/** What: short reference to a ticket. Use when: any response or event names a ticket. */
export interface TicketRef {
  ticket_id: string;
  short_id: string;
  display: string;
  stage: "open" | "pending_approval" | "resolved" | "closed";
  priority: "P1" | "P2" | "P3" | "P4";
  queue: "billing" | "technical" | "account" | "approvals";
  target_label: string;
  verified: boolean;
}

/** What: ticket with its customer-visible details. Use when: list_tickets. */
export interface TicketView {
  ticket_id: string;
  short_id: string;
  display: string;
  stage: "open" | "pending_approval" | "resolved" | "closed";
  priority: "P1" | "P2" | "P3" | "P4";
  queue: "billing" | "technical" | "account" | "approvals";
  target_label: string;
  verified: boolean;
  category: string;
  summary: string;
  outcome?: string | null;
  outcome_note?: string | null;
  created_at: string;
  updated_at: string;
}

/** What: body of GET /v1/me/tickets. Use when: list_tickets. Inputs: tickets. */
export interface TicketsResponse {
  tickets: Array<TicketView>;
}

/** What: search timings in milliseconds. Use when: SearchResponse.timing_ms. */
export interface TimingMs {
  embed: number;
  sql: number;
  total: number;
}

/** What: answer text chunk (only after Gate 2 passed or skipped). Use when: flushing a draft. */
export interface TokenEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "token";
  text: string;
}

/** What: one stored transcript item (chat or voice). Use when: GET /transcript. */
export interface TranscriptItem {
  id: string;
  turn_id?: string | null;
  role: "user" | "assistant";
  mode: "chat" | "voice";
  source: "server" | "client";
  text: string;
  interrupted: boolean;
  created_at: string;
}

/** What: one client-reported voice transcript item. Use when: TranscriptPost.items. */
export interface TranscriptItemIn {
  realtime_item_id: string;
  role: "user" | "assistant";
  text: string;
  interrupted: boolean;
  client_ts: string;
}

/** What: body of POST /transcript. Use when: the browser posts voice chatter. */
export interface TranscriptPost {
  voice_session_id: string;
  items: Array<TranscriptItemIn>;
}

/** What: last event of every stream (also after error). Use when: ingress step 16 or replay. */
export interface TurnCompletedEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "turn.completed";
  final: FinalAnswer;
  usage: Usage;
  end_reason: "answered" | "clarify" | "readback" | "verification_requested" | "verified" | "committed" | "submitted" | "cancelled" | "escalated" | "declined_off_topic" | "cap_reached" | "limit_reached" | "deadline" | "error" | "swept";
  replayed: boolean;
}

/** What: body of POST /api/sessions/{sid}/turns. */
export interface TurnRequest {
  client_turn_id: string;
  text?: string | null;
  confirm?: ConfirmInput | null;
  verification?: VerificationInput | null;
  ticket_offer?: TicketOffer | null;
}

/** What: first event of every turn. Use when: ingress step 9. Inputs: replica, input kind, */
export interface TurnStartedEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "turn.started";
  served_by: string;
  input_kind: "text" | "confirm" | "verification" | "ticket_offer" | "voice";
  contract_version: string;
}

/** What: claims answer for a session that is not verified. */
export interface UnverifiedClaims {
  verified: false;
  verify_locked: boolean;
  failed_verifies: number;
  attempts_left: number;
}

/** What: the turn's token, cost and latency totals. Use when: turn.completed, drawer. */
export interface Usage {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd_est: number;
  first_status_ms?: number | null;
  ttft_ms?: number | null;
  latency_ms: number;
  model_calls: number;
  tool_calls: number;
  models_used: Array<string>;
  fallback_used: boolean;
}

/** What: the masked verification card values. Use when: TurnRequest.verification. */
export interface VerificationInput {
  account_id: string;
  pin: string;
}

/** What: ask the browser to show the verification card. Use when: route verify, retry, typed PIN. */
export interface VerificationRequiredEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "verification.required";
  reason: "account_lookup" | "action" | "ticket_status" | "typed_pin_rejected" | "retry";
  prompt_text: string;
  attempts_left: number;
  fields: Array<"account_id" | "pin">;
}

/** What: outcome of a verification attempt. Use when: after core verify. */
export interface VerificationResultEvent {
  v: 1;
  seq: number;
  session_id: string;
  turn_id: string;
  trace_id: string;
  ts: string;
  mode: "chat" | "voice";
  type: "verification.result";
  status: "verified" | "failed" | "locked" | "conflict" | "unavailable";
  person?: VerifiedPerson | null;
  attempts_left?: number | null;
  ticket?: TicketRef | null;
}

/** What: who the session is verified as, for display. Use when: verification.result. */
export interface VerifiedPerson {
  display_name: string;
  role: "owner" | "billing_admin" | "member";
  tier: "free" | "pro" | "business" | "enterprise";
  contract_status: "trialing" | "active" | "past_due" | "in_notice" | "cancelled";
  account_id: string;
}

/** What: body of POST /v1/identity/verify. Use when: ingress verification code calls core. */
export interface VerifyRequest {
  account_id: string;
  pin: string;
}

/** What: body of POST /api/sessions/{sid}/voice/ask (the ask_support_agent relay). */
export interface VoiceAskRequest {
  client_turn_id: string;
  voice_session_id: string;
  realtime_call_id: string;
  question: string;
  user_item_id?: string | null;
  user_transcript?: string | null;
  transcript_final: boolean;
  last_assistant_interrupted: boolean;
}

/** What: body of POST /voice/token. Use when: the browser starts a Realtime call. */
export interface VoiceToken {
  client_secret: string;
  expires_at: string;
  model: string;
  voice_session_id: string;
  max_session_s: number;
}

/** What: every SSE event, discriminated by `type` (doc 07 s6.2). */
export type SseEvent =
  | TurnStartedEvent
  | StepStartedEvent
  | StepCompletedEvent
  | GateEvent
  | StatusEvent
  | TokenEvent
  | VerificationRequiredEvent
  | VerificationResultEvent
  | ConfirmationRequiredEvent
  | ActionResultEvent
  | EscalatedEvent
  | ModelFallbackEvent
  | ErrorEvent
  | TurnCompletedEvent;

/** What: why a turn ended (doc 07 s6.2). */
export type EndReason = "answered" | "clarify" | "readback" | "verification_requested" | "verified" | "committed" | "submitted" | "cancelled" | "escalated" | "declined_off_topic" | "cap_reached" | "limit_reached" | "deadline" | "error" | "swept";

/** What: SSE event type names. */
export type EventType = SseEvent["type"];

export const EVENT_TYPES = [
  "turn.started",
  "step.started",
  "step.completed",
  "gate",
  "status",
  "token",
  "verification.required",
  "verification.result",
  "confirmation.required",
  "action.result",
  "escalated",
  "model.fallback",
  "error",
  "turn.completed"
] as const;

export const END_REASONS = [
  "answered",
  "clarify",
  "readback",
  "verification_requested",
  "verified",
  "committed",
  "submitted",
  "cancelled",
  "escalated",
  "declined_off_topic",
  "cap_reached",
  "limit_reached",
  "deadline",
  "error",
  "swept"
] as const;

export const ERROR_CLASSES = [
  "provider_timeout",
  "provider_rate_limited",
  "provider_spend_cap",
  "provider_5xx",
  "provider_auth",
  "retrieval_failed",
  "core_client_error",
  "core_conflict",
  "core_unavailable",
  "db_unavailable",
  "redis_unavailable",
  "rate_limited",
  "admission_rejected",
  "turn_deadline_exceeded",
  "access_denied",
  "not_found",
  "validation"
] as const;

export const AGENT_ERROR_CODES = {
  "ACCESS_DENIED": 401,
  "ACCESS_ATTEMPTS": 429,
  "ORIGIN_MISMATCH": 403,
  "NOT_FOUND": 404,
  "VALIDATION_ERROR": 422,
  "TURN_IN_PROGRESS": 409,
  "DUPLICATE_CALL": 409,
  "VOICE_SESSIONS": 429,
  "VOICE_SESSION_EXPIRED": 429,
  "VOICE_UNAVAILABLE": 503,
  "SESSION_TURNS": 429,
  "IP_HOURLY": 429,
  "IP_SESSIONS": 429,
  "SESSION_POSTS": 429,
  "BUSY": 503,
  "REDIS_UNAVAILABLE": 503
} as const;

export const UNTRUSTED_WRAPPER_TAGS = [
  "doc",
  "policy_chunk",
  "tool_result",
  "account_context",
  "voice_transcript",
  "system",
  "tool"
] as const;

/** What: timeouts, retries and limits (doc 07 s8.1), seconds unless named otherwise. */
export const LIMITS = {
  "CHAT_TURN_DEADLINE_S": 45,
  "VOICE_TURN_DEADLINE_S": 20,
  "ESCALATION_RESERVE_S": 3,
  "TURN_LOCK_TTL_S": 60,
  "SWEEP_GRACE_S": 10,
  "MODEL_ATTEMPT_TIMEOUT_S": 10,
  "MODEL_FIRST_TOKEN_TIMEOUT_S": 6,
  "MODEL_PRIMARY_RETRIES": 1,
  "MODEL_RETRY_BACKOFF_S": 0.5,
  "MODEL_FALLBACK_RETRIES": 0,
  "MODEL_MIN_ATTEMPT_S": 2,
  "SLOW_STATUS_AFTER_S": 4,
  "GATE2_VERIFIER_TIMEOUT_S": 3,
  "SCOPE_CLASSIFIER_TIMEOUT_S": 2,
  "KNOWLEDGE_SEARCH_TIMEOUT_S": 3.5,
  "KNOWLEDGE_SEARCH_RETRIES": 0,
  "KNOWLEDGE_INTERNAL_BUDGET_S": 3.0,
  "CORE_READ_TIMEOUT_S": 2,
  "CORE_READ_RETRIES": 1,
  "CORE_READ_RETRY_BACKOFF_S": 0.2,
  "CORE_VERIFY_TIMEOUT_S": 2,
  "CORE_VERIFY_RETRIES": 0,
  "CORE_PREPARE_TIMEOUT_S": 3,
  "CORE_PREPARE_RETRIES": 1,
  "CORE_COMMIT_TIMEOUT_S": 5,
  "CORE_STATUS_CHECK_TIMEOUT_S": 2,
  "CORE_COMMIT_RETRIES_AFTER_STATUS": 1,
  "CORE_CANCEL_TIMEOUT_S": 2,
  "CORE_TICKET_TIMEOUT_S": 3,
  "CORE_TICKET_RETRIES": 1,
  "CORE_TICKET_FAILURE_PATH_RETRIES": 0,
  "CORE_MIN_CALL_S": 1,
  "POSTGRES_CHECKPOINTER_TIMEOUT_S": 2,
  "REDIS_TIMEOUT_S": 0.3,
  "VOICE_MINT_TIMEOUT_S": 5,
  "COMPOSE_STOP_GRACE_S": 60,
  "UVICORN_GRACEFUL_SHUTDOWN_S": 55,
  "BROWSER_CHAT_NO_BYTES_S": 20,
  "BROWSER_CHAT_TURN_S": 50,
  "BROWSER_VOICE_RELAY_S": 25,
  "BROWSER_TRANSCRIPT_WAIT_S": 1.5,
  "KEEPALIVE_INTERVAL_S": 10,
  "PENDING_ACTION_TTL_S": 600,
  "STEP_UP_VALIDITY_S": 300,
  "VOICE_SESSION_MAX_S": 600,
  "MODEL_CALLS_PER_TURN": 6,
  "TOOL_CALLS_PER_TURN": 8,
  "SEARCH_KB_PER_TURN": 3,
  "REQUEST_VERIFICATION_PER_TURN": 1,
  "PREPARES_PER_TURN": 1,
  "TURNS_PER_SESSION": 40,
  "MESSAGE_MAX_CHARS": 2000,
  "SAME_TOOL_FAILURES_BEFORE_TICKET": 2,
  "WEAK_RETRIEVAL_CLARIFY_MAX": 1,
  "GATE2_FAILURES_CLARIFY_MAX": 1,
  "VERIFY_FAILURES_PER_SESSION": 3,
  "VERIFY_FAILURES_PER_ACCOUNT": 5,
  "VERIFY_ACCOUNT_WINDOW_S": 900,
  "VERIFY_ACCOUNT_LOCK_S": 900,
  "UNVERIFIED_TICKETS_PER_SESSION": 2,
  "VERIFIED_TICKETS_PER_ACCOUNT_PER_DAY": 5,
  "BILLING_EMAIL_CHANGES_PER_DAY": 1,
  "SESSION_TURNS_PER_MINUTE": 10,
  "IP_TURNS_PER_HOUR": 60,
  "IP_SESSIONS_PER_HOUR": 20,
  "SESSION_POSTS_PER_HOUR": 120,
  "IP_VOICE_TOKENS_PER_HOUR": 3,
  "IP_ACCESS_ATTEMPTS_PER_HOUR": 10,
  "ADMISSION_CONCURRENCY": 8,
  "ADMISSION_WAIT_S": 1,
  "BUSY_RETRY_AFTER_S": 3
} as const;

export const VISIBILITY = {
  "V0": [
    "create_ticket",
    "request_verification",
    "search_kb"
  ],
  "V1": [
    "create_ticket",
    "get_account_summary",
    "list_tickets",
    "prepare_refund",
    "search_kb"
  ],
  "V2": [
    "create_ticket",
    "get_account_summary",
    "list_charges",
    "list_tickets",
    "prepare_refund",
    "search_kb"
  ]
} as const;

export const ASK_SUPPORT_AGENT_TOOL = {
  "type": "function",
  "name": "ask_support_agent",
  "description": "Ask the Quillstack support agent. Use for anything about Quillstack, the customer's account, billing, policies, tickets or actions, and whenever the customer answers a question the agent asked (yes, no, account ID, PIN).",
  "parameters": {
    "type": "object",
    "additionalProperties": false,
    "required": [
      "question"
    ],
    "properties": {
      "question": {
        "type": "string",
        "maxLength": 2000,
        "description": "What the customer said or asked, in their words"
      }
    }
  }
} as const;
