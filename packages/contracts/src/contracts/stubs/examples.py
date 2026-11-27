"""What: example core bodies for the S1 persona (Priya, Pro owner, duplicate $96.00 charge on
21 November 2026), built from the doc 07 s3 examples and validated by the models.
Use when: the core stub answers, fixtures are rendered, or a unit test needs a realistic body.
Inputs: none.
Output: model instances (CLAIMS, PROFILE, CHARGES, TICKET_REF, PREPARE_ALLOWED, PENDING_PUBLIC, ...).
Side effects: none.
Role: one set of example values shared by the stub and the SSE fixtures, so they agree.
Values are illustrative; lane A's seeds (data/seeds/personas.yaml) own the real persona data."""
from contracts.core_api import (
    ActionHistoryItem,
    CancelResult,
    ChargeView,
    Claims,
    CommitResult,
    MeContext,
    PendingPublic,
    PendingStatus,
    Permissions,
    PolicyRef,
    PrepareResult,
    Profile,
    Readback,
    SubmitResult,
    TicketCreateResponse,
    TicketRef,
    TicketView,
)

SESSION_ID = "019a7c2e-5b10-7000-8000-00000000a001"
ACCOUNT_ID = "QS-10423"
STUB_PIN = "4821"
PENDING_ID = "PA-3F2A9C1B"
POLICY_VERSION = "pv-stub-0001"

PERMISSIONS = Permissions(billing_read=True, billing_actions=True, own_notification_prefs=True)

CLAIMS = Claims(verified=True, session_id=SESSION_ID, user_id="U-0001", display_name="Priya Raman",
                account_id=ACCOUNT_ID, account_name="Acme Analytics", role="owner", tier="pro",
                contract_status="active", grace_until=None, permissions=PERMISSIONS,
                expires_at="2026-11-27T15:00:00Z")

PROFILE = Profile(account_id=ACCOUNT_ID, account_name="Acme Analytics", display_name="Priya Raman",
                  role="owner", tier="pro", contract_status="active", grace_until=None, seats=12,
                  billing_email_masked="p***@acme.com",
                  notification_prefs={"product_updates": True, "billing_notices": True,
                                      "incident_alerts": True, "weekly_digest": False})

CHARGES = [
    ChargeView(charge_id="CH-26112101", invoice_number="INV-2026-0412", label="invoice ending 0412",
               charged_at="2026-11-21T09:14:03Z", charge_date="2026-11-21", amount_cents=9600,
               currency="usd", description="Quillstack Pro, 12 seats, November", status="paid"),
    ChargeView(charge_id="CH-26112102", invoice_number="INV-2026-0413", label="invoice ending 0413",
               charged_at="2026-11-21T09:14:41Z", charge_date="2026-11-21", amount_cents=9600,
               currency="usd", description="Quillstack Pro, 12 seats, November", status="paid"),
]

POLICY_REF = PolicyRef(article="refund-policy", section="2", title="Refunds on the Pro plan")

TICKET_REF = TicketRef(ticket_id="TCK-26118213", short_id="8213", display="ticket ending 8213",
                       stage="open", priority="P3", queue="billing", target_label="invoice ending 0412",
                       verified=True)

TICKET_VIEW = TicketView(**TICKET_REF.model_dump(), category="billing",
                         summary="Customer asked about a duplicate charge on 21 November",
                         outcome=None, outcome_note=None, created_at="2026-11-26T18:02:00Z",
                         updated_at="2026-11-26T18:02:00Z")

READBACK = Readback(
    chat=("I can refund $96.00 for the duplicate charge on 21 November (invoice ending 0413). "
          "Shall I go ahead?"),
    voice=("I can refund ninety-six dollars for the duplicate charge on the twenty-first of November, "
           "invoice ending zero four one three. Shall I go ahead?"))

PREPARE_ALLOWED = PrepareResult(
    decision="allowed", reason_codes=["PRO_WITHIN_LIMITS"], policy_refs=[POLICY_REF], message=None,
    pending_action_id=PENDING_ID, pending_kind="commit", state="prepared",
    expires_at="2026-11-27T13:12:00Z", requires_step_up=False, readback=READBACK,
    details={"charge_id": "CH-26112102", "amount_cents": 9600}, ticket=None,
    policy_version=POLICY_VERSION, changed=False)

PENDING_PUBLIC = PendingPublic(
    pending_action_id=PENDING_ID, action_type="refund", kind="commit", state="prepared",
    readback_text=READBACK.chat, readback_say=READBACK.voice, amount_cents=9600,
    title="Refund $96.00 for the charge on 21 November", invoice_ref="invoice ending 0413",
    charge_date="2026-11-21", reason_code="PRO_WITHIN_LIMITS", requires_step_up=False,
    expires_at="2026-11-27T13:12:00Z", ticket=None)

PENDING_STATUS = PendingStatus(pending_action_id=PENDING_ID, action_type="refund", state="prepared",
                               decision="allowed", expires_at="2026-11-27T13:12:00Z", ticket=None,
                               result=None)

COMMIT_RESULT = CommitResult(
    status="committed", pending_action_id=PENDING_ID, replayed=False, changed=True,
    summary="Refunded $96.00 for charge ending 0413. It reaches your card in 5 to 10 business days.",
    summary_say=("Done. I've refunded ninety-six dollars for the charge ending zero four one three. "
                 "It reaches your card in five to ten business days."),
    details={"charge_id": "CH-26112102", "amount_cents": 9600, "refund_id": "RF-26112701"})

SUBMIT_RESULT = SubmitResult(
    status="awaiting_approval", pending_action_id=PENDING_ID, replayed=False, changed=True,
    ticket=TICKET_REF.model_copy(update={"stage": "pending_approval", "queue": "approvals"}),
    summary=("I've sent this to our team for approval: ticket ending 8213. You'll see the outcome when "
             "you ask about your ticket."),
    summary_say=("I've sent this to our team for approval, ticket ending eight two one three. You'll "
                 "hear the outcome when you ask about your ticket."))

CANCEL_RESULT = CancelResult(pending_action_id=PENDING_ID, state="cancelled", changed=False)

TICKET_CREATED = TicketCreateResponse(ticket=TICKET_REF, merged=False, created=True)

RECENT_ACTIONS = [ActionHistoryItem(action_type="notification_prefs", label="Turned off weekly digest",
                                    effective_date="2026-11-02", committed_by="customer")]

ME_CONTEXT = MeContext(claims=CLAIMS, profile=PROFILE, open_tickets=[TICKET_REF],
                       recent_actions=RECENT_ACTIONS, pending_action=None,
                       generated_at="2026-11-27T13:00:00Z")
