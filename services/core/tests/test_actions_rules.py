"""What: unit tests of the pure helpers of the prepare and commit engine (no database).
Use when: changing core/actions.py. Inputs: data/policy.yaml. Output: pytest results. Side effects: none.
Role: CS-E-03.S2.T1 and T5 logic: the commit and submit gate table (doc 07 s3.3, s7.1), read-time expiry, the
confirmation card fields (r4), policy refs with titles, pending ids, the re-check rule and the outcome wording."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from contracts.policy import Readback, load_policy
from contracts.reason_codes import ReasonCode

from core.actions import (card_public, commit_summary, gate, new_pending_id, policy_refs, read_state,
                          rules_still_hold, section_title, submit_summary)
from core.rules.refund import Outcome, RefundEvaluation
from tests.conftest import POLICY

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 27, 9, 30, tzinfo=NY)
LATER = NOW + timedelta(minutes=5)
EARLIER = NOW - timedelta(seconds=1)


def _gate(**over: object) -> str | None:
    """What: call gate with a passing baseline and some fields replaced. Use when: the gate table.
    Inputs: overrides. Output: gate result. Side effects: none. Role: keeps the table rows short."""
    args: dict[str, object] = {"state": "prepared", "expected": "prepared", "expires_at": LATER, "now": NOW,
                               "prepare_turn_id": "turn-1", "turn_id": "turn-2", "requires_step_up": False,
                               "step_up_at": None}
    args.update(over)
    return gate(**args)  # type: ignore[arg-type]


@pytest.mark.parametrize("over,expected", [
    ({}, None),
    ({"state": "awaiting_customer", "expected": "awaiting_customer"}, None),
    ({"expires_at": NOW}, "PENDING_EXPIRED"),                       # boundary: now >= expires_at is expired
    ({"expires_at": EARLIER}, "PENDING_EXPIRED"),
    ({"state": "expired"}, "PENDING_EXPIRED"),
    ({"state": "awaiting_customer"}, "PENDING_NOT_ACTIVE"),          # commit on an approval-path row
    ({"state": "prepared", "expected": "awaiting_customer"}, "PENDING_NOT_ACTIVE"),
    ({"state": "cancelled"}, "PENDING_NOT_ACTIVE"),
    ({"state": "superseded"}, "PENDING_NOT_ACTIVE"),
    ({"state": "rules_changed"}, "PENDING_NOT_ACTIVE"),
    ({"state": "awaiting_approval", "expires_at": None}, "PENDING_NOT_ACTIVE"),
    ({"turn_id": "turn-1"}, "CONFIRMATION_REQUIRED"),                 # same turn as the read-back
    ({"turn_id": "TURN-1"}, "CONFIRMATION_REQUIRED"),                 # uuid text is case-insensitive (review M1)
    ({"prepare_turn_id": "0197B5A4-7C3E-7F00-8000-00000000000A",
      "turn_id": "0197b5a4-7c3e-7f00-8000-00000000000a"}, "CONFIRMATION_REQUIRED"),
    ({"requires_step_up": True}, "STEP_UP_REQUIRED"),
    ({"requires_step_up": True, "step_up_at": NOW}, None),
    ({"expires_at": EARLIER, "turn_id": "turn-1"}, "PENDING_EXPIRED"),  # expiry is checked before the turn rule
])
def test_gate_table(over, expected):
    """What: the commit and submit precondition table. Use when: always. Inputs: one row. Output: assertion.
    Side effects: none. Role: doc 07 s3.3 commit and submit checks, in order (expiry, state, turn, step-up)."""
    assert _gate(**over) == expected


def test_read_state_applies_expiry_without_a_write():
    """What: an open row past its expiry reads as expired; other rows read as stored. Use when: always.
    Inputs: none. Output: assertions. Side effects: none. Role: doc 07 s7.1 'checked at read time'."""
    assert read_state("prepared", NOW, NOW) == "expired"
    assert read_state("awaiting_customer", EARLIER, NOW) == "expired"
    assert read_state("prepared", LATER, NOW) == "prepared"
    assert read_state("awaiting_approval", None, NOW) == "awaiting_approval"
    assert read_state("committed", EARLIER, NOW) == "committed"


def test_card_public_has_the_r4_confirmation_card_fields():
    """What: params.public carries the read-back, amount, card title, invoice ref, charge date and rule code.
    Use when: always. Inputs: none. Output: assertion. Side effects: none. Role: doc 07 s3.3 PendingPublic (r4)."""
    rb = Readback(chat="chat text", voice="voice text")
    pub = card_public(amount_cents=9600, charge_label="invoice ending 0412", charge_date=date(2026, 11, 21),
                      readback=rb, reason_code="PRO_WITHIN_LIMITS")
    assert pub == {"readback_text": "chat text", "readback_say": "voice text", "amount_cents": 9600,
                   "title": "Refund $96.00 for the charge on 21 November", "invoice_ref": "invoice ending 0412",
                   "charge_date": "2026-11-21", "reason_code": "PRO_WITHIN_LIMITS"}


def test_section_title_and_policy_refs():
    """What: a section slug becomes a readable title; refs are deduplicated in code order. Use when: always.
    Inputs: data/policy.yaml. Output: assertions. Side effects: none. Role: doc 07 s3.3 PolicyRef.title."""
    assert section_title("pro-plan-refunds") == "Pro plan refunds"
    assert section_title("past-due-accounts") == "Past due accounts"
    policy = load_policy(POLICY)
    refs = policy_refs(policy, [ReasonCode.PRO_LATE_IN_BAND, ReasonCode.PRO_OVER_CAP])
    assert [r.model_dump() for r in refs] == [{"article": "refund-policy", "section": "pro-plan-refunds",
                                               "title": "Pro plan refunds"}]


def test_new_pending_id_format():
    """What: ids match the schema check PA- plus 8 upper-case hex digits. Use when: always. Inputs: none.
    Output: assertions. Side effects: none. Role: pending_actions.id CHECK."""
    assert new_pending_id(lambda n: 0xAB12) == "PA-0000AB12"
    assert re.fullmatch(r"PA-[0-9A-F]{8}", new_pending_id())


def _ev(decision: str, params: dict[str, object]) -> RefundEvaluation:
    """What: a minimal evaluation for the re-check. Use when: rules_still_hold tests. Inputs: wire decision, params.
    Output: RefundEvaluation. Side effects: none. Role: test builder."""
    outcome = {"allowed": Outcome.ALLOW_AUTO_WITH_CONFIRM, "needs_approval": Outcome.NEEDS_APPROVAL,
               "declined": Outcome.DECLINED}[decision]
    return RefundEvaluation(outcome, (ReasonCode.PRO_WITHIN_LIMITS,), decision, (), params=params)  # type: ignore[arg-type]


def test_rules_still_hold_needs_same_decision_and_same_params():
    """What: the re-check passes only when the decision is the expected one and charge id and amount are unchanged.
    Use when: always. Inputs: none. Output: assertions. Side effects: none.
    Role: doc 07 s3.3 'rules re-evaluated ... and params unchanged'. Stored params also hold reason and public,
    which the re-check ignores."""
    stored = {"charge_id": "CH-00000412", "amount_cents": 9600, "reason": "duplicate", "public": {"title": "x"}}
    same = {"charge_id": "CH-00000412", "amount_cents": 9600}
    assert rules_still_hold(_ev("allowed", same), "allowed", stored)
    assert not rules_still_hold(_ev("needs_approval", same), "allowed", stored)
    assert not rules_still_hold(_ev("declined", {}), "allowed", stored)
    assert not rules_still_hold(_ev("allowed", {**same, "amount_cents": 9700}), "allowed", stored)
    assert rules_still_hold(_ev("needs_approval", same), "needs_approval", stored)


def test_outcome_wording():
    """What: commit and submit summaries name the amount, the charge label and the ticket. Use when: always.
    Inputs: none. Output: assertions. Side effects: none. Role: CommitResult and SubmitResult text."""
    summary, say = commit_summary(9600, "invoice ending 0412")
    assert summary.startswith("Refunded $96.00 for invoice ending 0412.") and "$96.00" in say
    summary, say = submit_summary("8213")
    assert "ticket ending 8213" in summary and "ticket ending 8213" in say
