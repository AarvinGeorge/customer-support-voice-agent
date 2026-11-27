"""What: table tests for the pure refund evaluate function, one table row group per refund reason code.
Use when: pytest runs services/core/tests (CI job `python (services/core)`).
Inputs: data/policy.yaml and a frozen clock (2026-11-27 09:30 America/New_York); no database.
Output: pass or fail per row; a reachability test proves every refund ReasonCode is produced by some row.
Side effects: reads data/policy.yaml.
Role: CS-E-03.S2.T2 Done-when (L1 s5 A1; doc 07 s13.1 refund codes); the rule-unit-test gate."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from contracts.policy import PolicyConfig, load_policy
from contracts.reason_codes import ReasonCode as RC

from core.clock import FixedClock
from core.rules.refund import (
    REFUND_REASON_CODES,
    ChargeFacts,
    Outcome,
    PriorRefund,
    RefundEvaluation,
    evaluate,
)

POLICY_PATH = Path(__file__).resolve().parents[4] / "data" / "policy.yaml"
NY = ZoneInfo("America/New_York")
CLOCK = FixedClock(datetime(2026, 11, 27, 9, 30, tzinfo=NY))
TODAY = CLOCK.today()
POLICY = load_policy(POLICY_PATH)

AUTO, APPROVAL, HUMAN, DECLINED = (Outcome.ALLOW_AUTO_WITH_CONFIRM, Outcome.NEEDS_APPROVAL,
                                   Outcome.HUMAN_ONLY, Outcome.DECLINED)


def charge_on(days_ago: int, *, amount: int = 9600, status: str = "paid", charge_id: str = "CH-20261122",
              hour: int = 12) -> ChargeFacts:
    """What: a charge made `days_ago` business days before the frozen today. Use when: building a row.
    Inputs: age in days, amount, status, id, hour. Output: ChargeFacts. Side effects: none.
    Role: keeps row ages readable (age 14 means exactly 14 days)."""
    when = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time(), NY).replace(hour=hour)
    return ChargeFacts(charge_id=charge_id, amount_cents=amount, charged_at=when,
                       status=status, label="invoice ending 0412")  # pyright: ignore[reportArgumentType]


@dataclass(frozen=True)
class Row:
    """What: one table row. Use when: parametrizing. Inputs: scenario and expectation. Output: row.
    Side effects: none. Role: the data of the table test."""

    id: str
    outcome: Outcome
    codes: tuple[RC, ...]
    tier: str = "pro"
    status: str = "active"
    role: str = "owner"
    charge: ChargeFacts = field(default_factory=lambda: charge_on(5))
    prior: tuple[PriorRefund, ...] = ()
    followup: str | None = None


def prior_refund(days_ago: int, charge_id: str = "CH-20260901") -> PriorRefund:
    """What: a completed refund `days_ago` days before today. Use when: building a row. Inputs: age,
    charge id. Output: PriorRefund. Side effects: none. Role: 90-day rule input."""
    return PriorRefund(charge_id=charge_id, effective_date=TODAY - timedelta(days=days_ago))


ROWS: list[Row] = [
    # ROLE_NOT_PERMITTED (role is checked before status: a member never learns the account is past due)
    Row("role-member-pro", DECLINED, (RC.ROLE_NOT_PERMITTED,), role="member"),
    Row("role-member-business", DECLINED, (RC.ROLE_NOT_PERMITTED,), tier="business", role="member"),
    Row("role-member-past-due-no-ticket", DECLINED, (RC.ROLE_NOT_PERMITTED,), role="member", status="past_due"),
    Row("role-billing-admin-ok", AUTO, (RC.PRO_WITHIN_LIMITS,), role="billing_admin"),
    # FREE_NOTHING_CHARGED
    Row("free", DECLINED, (RC.FREE_NOTHING_CHARGED,), tier="free"),
    # TRIAL_NOTHING_TO_REFUND
    Row("trialing-pro", DECLINED, (RC.TRIAL_NOTHING_TO_REFUND,), status="trialing"),
    Row("trialing-business", DECLINED, (RC.TRIAL_NOTHING_TO_REFUND,), tier="business", status="trialing"),
    # PAST_DUE first for every tier, with the billing follow-up flag
    Row("past-due-pro", DECLINED, (RC.PAST_DUE,), status="past_due", followup="billing"),
    Row("past-due-business", DECLINED, (RC.PAST_DUE,), tier="business", status="past_due", followup="billing"),
    Row("past-due-enterprise-not-human", DECLINED, (RC.PAST_DUE,), tier="enterprise", status="past_due",
        followup="billing"),
    Row("past-due-beats-already-refunded", DECLINED, (RC.PAST_DUE,), status="past_due",
        charge=charge_on(5, status="refunded"), followup="billing"),
    Row("past-due-beats-outside-window", DECLINED, (RC.PAST_DUE,), tier="business", status="past_due",
        charge=charge_on(60), followup="billing"),
    # ALREADY_REFUNDED
    Row("already-refunded-status", DECLINED, (RC.ALREADY_REFUNDED,), charge=charge_on(5, status="refunded")),
    Row("already-refunded-history", DECLINED, (RC.ALREADY_REFUNDED,), prior=(prior_refund(3, "CH-20261122"),)),
    Row("already-refunded-enterprise", DECLINED, (RC.ALREADY_REFUNDED,), tier="enterprise",
        charge=charge_on(5, status="refunded")),
    # CHARGE_NOT_REFUNDABLE
    Row("failed-charge", DECLINED, (RC.CHARGE_NOT_REFUNDABLE,), charge=charge_on(5, status="failed")),
    Row("future-dated-charge", DECLINED, (RC.CHARGE_NOT_REFUNDABLE,), charge=charge_on(-1)),
    # PRO_WITHIN_LIMITS (act with confirmation), boundaries inclusive
    Row("pro-day-0", AUTO, (RC.PRO_WITHIN_LIMITS,), charge=charge_on(0)),
    Row("pro-exactly-14-days", AUTO, (RC.PRO_WITHIN_LIMITS,), charge=charge_on(14)),
    Row("pro-exactly-150-dollars", AUTO, (RC.PRO_WITHIN_LIMITS,), charge=charge_on(5, amount=15000)),
    Row("pro-cancelled-in-window", AUTO, (RC.PRO_WITHIN_LIMITS,), status="cancelled"),
    Row("pro-in-notice", AUTO, (RC.PRO_WITHIN_LIMITS,), status="in_notice"),
    Row("pro-prior-refund-91-days-ago", AUTO, (RC.PRO_WITHIN_LIMITS,), prior=(prior_refund(91),)),
    # PRO_LATE_IN_BAND
    Row("pro-15-days", APPROVAL, (RC.PRO_LATE_IN_BAND,), charge=charge_on(15)),
    Row("pro-exactly-21-days", APPROVAL, (RC.PRO_LATE_IN_BAND,), charge=charge_on(21)),
    # PRO_OVER_CAP
    Row("pro-15001-cents", APPROVAL, (RC.PRO_OVER_CAP,), charge=charge_on(5, amount=15001)),
    Row("pro-over-cap-day-14", APPROVAL, (RC.PRO_OVER_CAP,), charge=charge_on(14, amount=50000)),
    # PRO_SECOND_IN_REPEAT_WINDOW
    Row("pro-prior-refund-exactly-90-days", APPROVAL, (RC.PRO_SECOND_IN_REPEAT_WINDOW,), prior=(prior_refund(90),)),
    Row("pro-prior-refund-yesterday", APPROVAL, (RC.PRO_SECOND_IN_REPEAT_WINDOW,), prior=(prior_refund(1),)),
    # all three Pro reasons together, primary first, in a stable order
    Row("pro-all-three-reasons", APPROVAL,
        (RC.PRO_LATE_IN_BAND, RC.PRO_OVER_CAP, RC.PRO_SECOND_IN_REPEAT_WINDOW),
        charge=charge_on(18, amount=40000), prior=(prior_refund(30),)),
    # PRO_PAST_LATE_BAND
    Row("pro-22-days", DECLINED, (RC.PRO_PAST_LATE_BAND,), charge=charge_on(22)),
    Row("pro-400-days", DECLINED, (RC.PRO_PAST_LATE_BAND,), charge=charge_on(400)),
    # BUSINESS_APPROVAL_REQUIRED (never auto), window inclusive at 30
    Row("business-day-0", APPROVAL, (RC.BUSINESS_APPROVAL_REQUIRED,), tier="business", charge=charge_on(0)),
    Row("business-small-amount", APPROVAL, (RC.BUSINESS_APPROVAL_REQUIRED,), tier="business",
        charge=charge_on(2, amount=100)),
    Row("business-exactly-30-days", APPROVAL, (RC.BUSINESS_APPROVAL_REQUIRED,), tier="business",
        charge=charge_on(30)),
    Row("business-prior-refund-ignored", APPROVAL, (RC.BUSINESS_APPROVAL_REQUIRED,), tier="business",
        prior=(prior_refund(10),)),
    # OUTSIDE_WINDOW
    Row("business-31-days", DECLINED, (RC.OUTSIDE_WINDOW,), tier="business", charge=charge_on(31)),
    # ENTERPRISE_HUMAN_DECIDES: human only, no window applies
    Row("enterprise-recent", HUMAN, (RC.ENTERPRISE_HUMAN_DECIDES,), tier="enterprise"),
    Row("enterprise-old-charge", HUMAN, (RC.ENTERPRISE_HUMAN_DECIDES,), tier="enterprise", charge=charge_on(400)),
    Row("enterprise-big-amount", HUMAN, (RC.ENTERPRISE_HUMAN_DECIDES,), tier="enterprise",
        charge=charge_on(1, amount=9_000_000)),
]


def run(row: Row, policy: PolicyConfig = POLICY) -> RefundEvaluation:
    """What: call evaluate for a row with the frozen clock. Use when: a test needs the result.
    Inputs: row, optional policy. Output: RefundEvaluation. Side effects: none. Role: one call site."""
    return evaluate(tier=row.tier, contract_status=row.status, role=row.role, charge=row.charge,  # pyright: ignore[reportArgumentType]
                    prior_refunds=row.prior, policy=policy, now=CLOCK.now())


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.id)
def test_row(row: Row) -> None:
    """What: outcome, ordered reason codes and follow-up flag match the row. Use when: pytest runs.
    Inputs: a row. Output: none. Side effects: none. Role: the table test per reason code."""
    ev = run(row)
    assert ev.outcome == row.outcome
    assert ev.reason_codes == row.codes
    assert ev.followup_ticket_queue == row.followup


def test_every_refund_reason_code_is_reachable() -> None:
    """What: the union of codes the table produces equals the refund code set, and that set equals
    the contract enum minus the email and grace codes. Use when: pytest runs. Inputs: none.
    Output: none. Side effects: none. Role: Done-when 'every refund ReasonCode reachable by a row'."""
    produced = {c for row in ROWS for c in run(row).reason_codes}
    assert produced == REFUND_REASON_CODES
    non_refund = {c for c in RC if c.name.startswith(("EMAIL_", "GRACE_")) or c is RC.NO_BILLING_ON_PLAN}
    assert REFUND_REASON_CODES == set(RC) - non_refund
    assert len(REFUND_REASON_CODES) == 14


def test_table_has_a_row_for_each_outcome() -> None:
    """What: all four outcomes occur. Use when: pytest runs. Inputs: none. Output: none.
    Side effects: none. Role: guards against an unreachable Outcome member."""
    assert {r.outcome for r in ROWS} == set(Outcome)


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.id)
def test_decision_text_and_refs(row: Row) -> None:
    """What: policy decision, message, policy ref and read-back follow the outcome and policy.yaml.
    Use when: pytest runs. Inputs: a row. Output: none. Side effects: none.
    Role: customer-safe text and citation per code (07 s13.1, s3.3)."""
    ev = run(row)
    primary = POLICY.reason_codes[ev.reason_codes[0]]
    assert ev.policy_refs[0] == primary.policy_ref
    assert [r for r in ev.policy_refs] == list(dict.fromkeys(POLICY.reason_codes[c].policy_ref for c in ev.reason_codes))
    assert ev.policy_decision == primary.decision
    if ev.outcome is DECLINED:
        assert ev.message and "{" not in ev.message
        assert ev.readback is None and ev.params == {}
    else:
        assert ev.message is None and ev.readback is not None
        assert ev.params == {"charge_id": row.charge.charge_id, "amount_cents": row.charge.amount_cents}
    if ev.outcome is AUTO:
        assert ev.readback is not None
        assert not ev.readback.chat.endswith("approval?")
    if ev.outcome in (APPROVAL, HUMAN):
        assert ev.readback is not None
        assert ev.readback.chat.endswith("Shall I send this to our team for approval?")
        assert ev.readback.voice.endswith("Shall I send this to our team for approval?")


def test_auto_readback_exact_text() -> None:
    """What: the confirm read-back fills amount, date and label from the charge. Use when: pytest runs.
    Inputs: none. Output: none. Side effects: none. Role: policy readback templates, 07 s13.1."""
    ev = run(Row("x", AUTO, (RC.PRO_WITHIN_LIMITS,), charge=charge_on(5)))
    assert ev.readback is not None
    assert ev.readback.chat == ("I can refund $96.00 for the charge on 22 November (invoice ending 0412). "
                                "Shall I go ahead?")
    assert ev.readback.voice == ("I can refund $96.00 for the charge on 22 November, invoice ending 0412. "
                                 "Shall I go ahead?")


def test_approval_readback_exact_text() -> None:
    """What: the approval read-back names the amount and label and ends with the fixed question.
    Use when: pytest runs. Inputs: none. Output: none. Side effects: none. Role: 07 s13.1."""
    ev = run(Row("x", APPROVAL, (RC.BUSINESS_APPROVAL_REQUIRED,), tier="business"))
    assert ev.readback is not None
    assert ev.readback.chat == ("I can't issue this refund myself, but I can send a request to refund $96.00 "
                                "for invoice ending 0412 to our team for approval. "
                                "Shall I send this to our team for approval?")


def test_decline_messages_fill_window_late_and_age() -> None:
    """What: PRO_PAST_LATE_BAND and OUTSIDE_WINDOW messages carry the policy numbers and the real age.
    Use when: pytest runs. Inputs: none. Output: none. Side effects: none. Role: draft C s2.3 wording."""
    pro = run(Row("x", DECLINED, (RC.PRO_PAST_LATE_BAND,), charge=charge_on(30)))
    assert pro.message == ("Pro refunds are possible up to 14 days after the charge, or up to 7 days later "
                           "with approval; this charge is 30 days old.")
    biz = run(Row("x", DECLINED, (RC.OUTSIDE_WINDOW,), tier="business", charge=charge_on(45)))
    assert biz.message == "Business refunds are possible within 30 days of the charge; this charge is 45 days old."


def test_billing_followup_only_for_past_due() -> None:
    """What: only PAST_DUE sets the follow-up queue (policy followup_queue). Use when: pytest runs.
    Inputs: none. Output: none. Side effects: none. Role: core opens the billing ticket from this flag."""
    assert {r.id for r in ROWS if run(r).followup_ticket_queue} == {r.id for r in ROWS if r.followup}
    assert all(run(r).followup_ticket_queue is None for r in ROWS if RC.PAST_DUE not in r.codes)


# ---- time: business dates, DST, UTC offset ----

def _age_case(charged: datetime, now: datetime, amount: int = 9600) -> RefundEvaluation:
    """What: evaluate a Pro charge at an explicit charged time and now. Use when: a time test needs
    exact instants. Inputs: two aware datetimes. Output: RefundEvaluation. Side effects: none.
    Role: DST and offset tests."""
    ch = ChargeFacts(charge_id="CH-20260301", amount_cents=amount, charged_at=charged, status="paid",
                     label="invoice ending 0412")
    return evaluate(tier="pro", contract_status="active", role="owner", charge=ch, prior_refunds=(),
                    policy=POLICY, now=now)


def test_dst_spring_forward_counts_calendar_days() -> None:
    """What: a charge on 1 March at 12:00 EST is exactly 14 days old on 15 March 00:30 EDT even though
    only 13 days 11.5 hours of real time passed (the clocks skipped an hour on 8 March). Use when:
    pytest runs. Inputs: none. Output: none. Side effects: none. Role: DST boundary row."""
    charged = datetime(2026, 3, 1, 12, 0, tzinfo=NY)
    assert _age_case(charged, datetime(2026, 3, 15, 0, 30, tzinfo=NY)).reason_codes == (RC.PRO_WITHIN_LIMITS,)
    assert _age_case(charged, datetime(2026, 3, 16, 0, 30, tzinfo=NY)).reason_codes == (RC.PRO_LATE_IN_BAND,)


def test_dst_fall_back_counts_calendar_days() -> None:
    """What: across the 1 November fall-back, 25 October 12:00 EDT to 8 November 23:30 EST is 14 days
    (14 days 10.5 hours real), and 9 November is day 15. Use when: pytest runs. Inputs: none.
    Output: none. Side effects: none. Role: DST boundary row."""
    charged = datetime(2026, 10, 25, 12, 0, tzinfo=NY)
    assert _age_case(charged, datetime(2026, 11, 8, 23, 30, tzinfo=NY)).reason_codes == (RC.PRO_WITHIN_LIMITS,)
    assert _age_case(charged, datetime(2026, 11, 9, 0, 5, tzinfo=NY)).reason_codes == (RC.PRO_LATE_IN_BAND,)


def test_now_in_utc_uses_the_policy_business_date() -> None:
    """What: 02:00 UTC on 9 November is still 8 November in New York, so a 25 October charge is day 14,
    not 15; a charge at 22:00 EDT on 25 October (already 26 October in UTC) is dated 25 October.
    Use when: pytest runs. Inputs: none. Output: none. Side effects: none. Role: timezone trap row."""
    charged = datetime(2026, 10, 25, 22, 0, tzinfo=NY)
    assert charged.astimezone(UTC).date() == date(2026, 10, 26)
    now = datetime(2026, 11, 9, 2, 0, tzinfo=UTC)
    assert _age_case(charged, now).reason_codes == (RC.PRO_WITHIN_LIMITS,)
    assert _age_case(charged.astimezone(UTC), now).reason_codes == (RC.PRO_WITHIN_LIMITS,)


def test_naive_datetimes_are_rejected() -> None:
    """What: a naive now or charged_at raises ValueError. Use when: pytest runs. Inputs: none.
    Output: none. Side effects: none. Role: no silent timezone guess in a money rule."""
    with pytest.raises(ValueError, match="aware"):
        _age_case(datetime(2026, 11, 1, 12, 0), CLOCK.now())
    with pytest.raises(ValueError, match="aware"):
        _age_case(datetime(2026, 11, 1, 12, 0, tzinfo=NY), datetime(2026, 11, 27, 9, 30))


def test_evaluate_is_pure() -> None:
    """What: same inputs give equal results, and the call does not mutate the policy or the inputs.
    Use when: pytest runs. Inputs: none. Output: none. Side effects: none. Role: pure-function check."""
    row = ROWS[0]
    before = POLICY.model_dump_json()
    assert run(row) == run(row)
    assert POLICY.model_dump_json() == before


def test_limits_come_from_policy_not_code() -> None:
    """What: changing policy.yaml numbers moves the boundaries (window 10, cap 100 dollars, repeat 30
    days). Use when: pytest runs. Inputs: none. Output: none. Side effects: none.
    Role: L1 rule 'every value appears once, in the policy file'."""
    raw = yaml.safe_load(POLICY_PATH.read_text())
    raw["actions"]["refund"]["tiers"]["pro"].update(window_days=10, auto_cap_cents=10000)
    raw["actions"]["refund"]["repeat_window_days"] = 30
    custom = PolicyConfig.model_validate(raw)
    assert run(Row("x", AUTO, (), charge=charge_on(10, amount=10000)), custom).reason_codes == (RC.PRO_WITHIN_LIMITS,)
    assert run(Row("x", AUTO, (), charge=charge_on(11)), custom).reason_codes == (RC.PRO_LATE_IN_BAND,)
    assert run(Row("x", AUTO, (), charge=charge_on(5, amount=10001)), custom).reason_codes == (RC.PRO_OVER_CAP,)
    assert run(Row("x", AUTO, (), prior=(prior_refund(31),)), custom).reason_codes == (RC.PRO_WITHIN_LIMITS,)
    assert run(Row("x", AUTO, (), prior=(prior_refund(30),)), custom).reason_codes == (
        RC.PRO_SECOND_IN_REPEAT_WINDOW,)


def test_charge_facts_are_frozen() -> None:
    """What: ChargeFacts cannot be changed after creation. Use when: pytest runs. Inputs: none.
    Output: none. Side effects: none. Role: pure inputs stay pure."""
    ch = charge_on(1)
    with pytest.raises(AttributeError):
        ch.amount_cents = 1  # type: ignore[misc]
    assert replace(ch, amount_cents=1).amount_cents == 1
