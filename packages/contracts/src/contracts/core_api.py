"""What: core internal API shapes (doc 07 s3) used by the thin slice.
Use when: core builds a response, the agent parses one, or the core stub returns an example.
Inputs: none.
Output: Claims, MeContext, Profile, ChargeView, TicketRef, TicketView, PrepareResult, PendingPublic,
PendingStatus, CommitResult, SubmitResult, CancelResult, TicketCreate and request bodies.
Side effects: none.
Role: the agent-to-core wire contract. Step-up, billing email, grace extension, notification
preferences and operator shapes are designed only (doc 09 s1.2) and left out."""
from typing import Annotated, Any, Literal

from pydantic import Field

from contracts.base import StrictModel

Role = Literal["owner", "billing_admin", "member"]
Tier = Literal["free", "pro", "business", "enterprise"]
ContractStatus = Literal["trialing", "active", "past_due", "in_notice", "cancelled"]
TicketStage = Literal["open", "pending_approval", "resolved", "closed"]
ActionType = Literal["refund", "billing_email", "grace_extension"]
PendingKind = Literal["commit", "submit_for_approval"]
PendingState = Literal["prepared", "awaiting_customer", "awaiting_approval", "committed", "cancelled",
                       "expired", "superseded", "rejected", "rules_changed"]
RefundReason = Literal["duplicate", "service_issue", "accidental", "other"]
TicketCategory = Literal["billing", "refund", "access", "sso", "integration", "bug", "account_change",
                         "commitment", "human_requested", "other"]

PENDING_ID_PATTERN = r"^PA-[0-9A-F]{8}$"
CHARGE_ID_PATTERN = r"^CH-[0-9]{8}$"
TICKET_ID_PATTERN = r"^TCK-[0-9]{8}$"


class Permissions(StrictModel):
    """What: what the verified person may do. Use when: inside Claims.
    Inputs: three booleans. Output: model. Side effects: none. Role: doc 07 s3.1 Claims.permissions."""

    billing_read: bool
    billing_actions: bool
    own_notification_prefs: bool


class Claims(StrictModel):
    """What: the verified identity core holds for one agent session.
    Use when: verify succeeds, claims are read, or MeContext is built.
    Inputs: identity, role, tier, contract fields. Output: model. Side effects: none.
    Role: doc 07 s3.1 Claims; the only source of account, role and tier for the agent."""

    verified: Literal[True]
    session_id: str
    user_id: str
    display_name: str
    account_id: str
    account_name: str
    role: Role
    tier: Tier
    contract_status: ContractStatus
    grace_until: str | None = None
    permissions: Permissions
    expires_at: str


class UnverifiedClaims(StrictModel):
    """What: claims answer for a session that is not verified.
    Use when: GET /v1/sessions/{id}/claims for an unverified or unknown session.
    Inputs: lock and attempt counters. Output: model. Side effects: none. Role: doc 07 s3.1."""

    verified: Literal[False]
    verify_locked: bool
    failed_verifies: int
    attempts_left: int


class VerifyRequest(StrictModel):
    """What: body of POST /v1/identity/verify. Use when: ingress verification code calls core.
    Inputs: account_id, pin. Output: model. Side effects: none.
    Role: doc 07 s3.1 (no tool calls this; the PIN never reaches the model)."""

    account_id: str = Field(min_length=1, max_length=20)
    pin: str = Field(pattern=r"^[0-9]{4}$")


class Profile(StrictModel):
    """What: account and person profile. Use when: get_account_summary, MeContext.
    Inputs: profile fields. Output: model. Side effects: none. Role: doc 07 s3.2 Profile."""

    account_id: str
    account_name: str
    display_name: str
    role: Role
    tier: Tier
    contract_status: ContractStatus
    grace_until: str | None = None
    seats: int
    billing_email_masked: str | None = None
    notification_prefs: dict[str, bool]


class ChargeView(StrictModel):
    """What: one billing charge as the agent sees it. Use when: list_charges, ambiguity candidates.
    Inputs: charge fields. Output: model. Side effects: none.
    Role: doc 07 s3.2 ChargeView; description is free text and is wrapped as untrusted."""

    charge_id: str = Field(pattern=CHARGE_ID_PATTERN)
    invoice_number: str | None = None
    label: str
    charged_at: str
    charge_date: str
    amount_cents: int
    currency: str
    description: str
    status: Literal["paid", "failed", "refunded"]
    refunded_at: str | None = None


class ChargesResponse(StrictModel):
    """What: body of GET /v1/me/charges. Use when: list_charges. Inputs: charges, ambiguous.
    Output: model. Side effects: none. Role: doc 07 s3.2."""

    charges: list[ChargeView]
    ambiguous: bool


class TicketRef(StrictModel):
    """What: short reference to a ticket. Use when: any response or event names a ticket.
    Inputs: id, short id, display, stage, priority, queue, target label, verified.
    Output: model. Side effects: none. Role: doc 07 s3.2 TicketRef (format TCK-26118213, short 8213)."""

    ticket_id: str = Field(pattern=TICKET_ID_PATTERN)
    short_id: str = Field(pattern=r"^[0-9]{4}$")
    display: str
    stage: TicketStage
    priority: Literal["P1", "P2", "P3", "P4"]
    queue: Literal["billing", "technical", "account", "approvals"]
    target_label: str
    verified: bool


class TicketView(TicketRef):
    """What: ticket with its customer-visible details. Use when: list_tickets.
    Inputs: TicketRef fields plus category, summary, outcome, notes, times.
    Output: model. Side effects: none. Role: doc 07 s3.2 TicketView (summary and notes untrusted)."""

    category: str
    summary: str
    outcome: str | None = None
    outcome_note: str | None = None
    created_at: str
    updated_at: str


class TicketsResponse(StrictModel):
    """What: body of GET /v1/me/tickets. Use when: list_tickets. Inputs: tickets.
    Output: model. Side effects: none. Role: doc 07 s3.2."""

    tickets: list[TicketView]


class ActionHistoryItem(StrictModel):
    """What: one committed action in the last 90 days. Use when: MeContext.recent_actions.
    Inputs: type, label, date, actor, amount. Output: model. Side effects: none. Role: doc 07 s3.1."""

    action_type: Literal["refund", "billing_email", "grace_extension", "notification_prefs"]
    label: str
    effective_date: str
    committed_by: Literal["customer", "operator"]
    amount_cents: int | None = None


class PolicyRef(StrictModel):
    """What: pointer to the policy article section behind a decision. Use when: prepare results,
    rule rows, action results. Inputs: article, section, title. Output: model. Side effects: none.
    Role: doc 07 s3.3 PolicyRef."""

    article: str
    section: str
    title: str


class PendingPublic(StrictModel):
    """What: the customer-facing view of a pending action (confirmation card).
    Use when: MeContext.pending_action, confirmation.required, FinalAnswer.pending_action.
    Inputs: id, type, kind, state, read-back texts, card fields. Output: model. Side effects: none.
    Role: doc 07 s3.3 PendingPublic including the r4 card fields."""

    pending_action_id: str = Field(pattern=PENDING_ID_PATTERN)
    action_type: ActionType
    kind: PendingKind
    state: Literal["prepared", "awaiting_customer", "awaiting_approval"]
    readback_text: str
    readback_say: str
    amount_cents: int | None = None
    title: str
    invoice_ref: str | None = None
    charge_date: str | None = None
    reason_code: str
    requires_step_up: bool
    expires_at: str | None = None
    ticket: TicketRef | None = None


class MeContext(StrictModel):
    """What: account memory the agent loads at verify and every turn start.
    Use when: ingress step 11 memory refresh. Inputs: claims, profile, tickets, actions, pending.
    Output: model. Side effects: none. Role: doc 07 s3.1 MeContext."""

    claims: Claims
    profile: Profile
    open_tickets: list[TicketRef] = Field(max_length=10)
    recent_actions: list[ActionHistoryItem] = Field(max_length=20)
    pending_action: PendingPublic | None = None
    generated_at: str


class ChargeRefId(StrictModel):
    """What: charge reference by id. Use when: prepare_refund had a charge_id.
    Inputs: id. Output: model. Side effects: none. Role: doc 07 s3.3 charge_ref variant."""

    id: str = Field(pattern=CHARGE_ID_PATTERN)


class ChargeRefEnding(StrictModel):
    """What: charge reference by last four digits and optional date.
    Use when: prepare_refund had ending and maybe on. Inputs: ending, on.
    Output: model. Side effects: none. Role: doc 07 s3.3 charge_ref variant."""

    ending: str = Field(pattern=r"^[0-9]{4}$")
    on: str | None = None


class RefundPrepareRequest(StrictModel):
    """What: body of POST /v1/actions/refund/prepare. Use when: prepare_refund wrapper.
    Inputs: charge_ref (or null), reason, note. Output: model. Side effects: none. Role: doc 07 s3.3."""

    charge_ref: ChargeRefId | ChargeRefEnding | None
    reason: RefundReason
    note: str = Field(max_length=300)


class PrepareResult(StrictModel):
    """What: core's decision on a prepare. Use when: any prepare response.
    Inputs: decision, reasons, refs, pending fields, read-back, details, ticket, policy version.
    Output: model. Side effects: none. Role: doc 07 s3.3 PrepareResult; prepare never changes anything. `card` is an
    additive optional field (doc 07 s1.1): the r4 PendingPublic confirmation card of the new pending row, so the agent
    has the card on the read-back turn; None when declined."""

    decision: Literal["allowed", "needs_approval", "declined"]
    reason_codes: list[str]
    policy_refs: list[PolicyRef]
    message: str | None = None
    pending_action_id: str | None = Field(default=None, pattern=PENDING_ID_PATTERN)
    pending_kind: PendingKind | None = None
    state: Literal["prepared", "awaiting_customer"] | None = None
    expires_at: str | None = None
    requires_step_up: bool
    readback: "Readback | None" = None
    details: dict[str, Any]
    ticket: TicketRef | None = None
    policy_version: str
    changed: Literal[False]
    card: PendingPublic | None = None


class Readback(StrictModel):
    """What: read-back text for chat and voice. Use when: PrepareResult.readback.
    Inputs: chat, voice. Output: model. Side effects: none. Role: doc 07 s3.3."""

    chat: str
    voice: str


class CommitResult(StrictModel):
    """What: result of a commit (or operator approve). Use when: commit 200, PendingStatus.result.
    Inputs: status, id, replayed, summary texts, details. Output: model. Side effects: none.
    Role: doc 07 s3.3 CommitResult."""

    status: Literal["committed"]
    pending_action_id: str
    replayed: bool
    changed: Literal[True]
    summary: str
    summary_say: str
    details: dict[str, Any]


class PendingStatus(StrictModel):
    """What: state of a pending action. Use when: status check after a commit or submit failure.
    Inputs: id, type, state, decision, expiry, ticket, result. Output: model. Side effects: none.
    Role: doc 07 s3.3 PendingStatus."""

    pending_action_id: str
    action_type: str
    state: PendingState
    decision: Literal["allowed", "needs_approval"]
    expires_at: str | None = None
    ticket: TicketRef | None = None
    result: CommitResult | None = None


class SubmitResult(StrictModel):
    """What: result of sending an action for approval. Use when: submit 200.
    Inputs: status, id, replayed, ticket, summaries. Output: model. Side effects: none.
    Role: doc 07 s3.3 SubmitResult (a Pending approval ticket was created)."""

    status: Literal["awaiting_approval"]
    pending_action_id: str
    replayed: bool
    changed: Literal[True]
    ticket: TicketRef
    summary: str
    summary_say: str


class CancelResult(StrictModel):
    """What: result of a cancel. Use when: cancel 200. Inputs: id, state, changed.
    Output: model. Side effects: none. Role: doc 07 s3.3 cancel row."""

    pending_action_id: str
    state: Literal["cancelled"]
    changed: Literal[False]


class TicketCreate(StrictModel):
    """What: body of POST /v1/tickets. Use when: create_ticket tool or agent code escalation.
    Inputs: category, summary, steps, blocking, confidence, transcript ref, origin, reason.
    Output: model. Side effects: none. Role: doc 07 s3.4 TicketCreate."""

    category: TicketCategory
    summary: str = Field(min_length=1, max_length=500)
    steps_tried: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=8)
    blocking: bool = False
    confidence: float | None = Field(default=None, ge=0, le=1)
    transcript_ref: str | None = None
    origin: Literal["model", "code"]
    escalation_reason: str | None = None


class TicketCreateResponse(StrictModel):
    """What: body of a 200 from POST /v1/tickets. Use when: ticket create.
    Inputs: ticket, merged, created. Output: model. Side effects: none. Role: doc 07 s3.4."""

    ticket: TicketRef
    merged: bool
    created: bool


class CoreReady(StrictModel):
    """What: body of core GET /readyz. Use when: readiness checks.
    Inputs: status, versions, fault flag, seed date. Output: model. Side effects: none. Role: doc 07 s3.6."""

    status: str
    contract_version: str
    policy_version: str
    demo_faults: bool
    seed_date: str


PrepareResult.model_rebuild()
