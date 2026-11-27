"""What: the pure refund `evaluate` function and its input and output types (L1 s5 A1, doc 07 s13.1).
Use when: refund prepare decides what to offer, and commit or submit re-checks on live rows.
Inputs: account tier and contract status, caller role, one charge, the account's completed refunds,
the PolicyConfig and an aware `now`. Output: RefundEvaluation (outcome, ordered reason codes, policy
refs, customer-safe message, read-back text, frozen params, billing follow-up flag).
Side effects: none (no database, no clock, no logging).
Role: the single place refund rules live. Every limit is read from policy.yaml; the order of checks is
role, tier Free, trial, past_due, charge state, then tier rules (draft C s2.4)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from contracts.core_api import ContractStatus, Role, Tier
from contracts.policy import PolicyConfig, PolicyRefCfg, Readback
from contracts.reason_codes import ReasonCode

ChargeStatus = Literal["paid", "failed", "refunded"]

REFUND_REASON_CODES: frozenset[ReasonCode] = frozenset({
    ReasonCode.ROLE_NOT_PERMITTED, ReasonCode.FREE_NOTHING_CHARGED, ReasonCode.TRIAL_NOTHING_TO_REFUND,
    ReasonCode.PAST_DUE, ReasonCode.ALREADY_REFUNDED, ReasonCode.CHARGE_NOT_REFUNDABLE,
    ReasonCode.PRO_PAST_LATE_BAND, ReasonCode.OUTSIDE_WINDOW, ReasonCode.PRO_LATE_IN_BAND,
    ReasonCode.PRO_OVER_CAP, ReasonCode.PRO_SECOND_IN_REPEAT_WINDOW,
    ReasonCode.BUSINESS_APPROVAL_REQUIRED, ReasonCode.ENTERPRISE_HUMAN_DECIDES,
    ReasonCode.PRO_WITHIN_LIMITS,
})


class Outcome(StrEnum):
    """What: the four refund outcomes. Use when: branching on the result. Inputs: none. Output: enum.
    Side effects: none. Role: allow_auto_with_confirm maps to wire decision allowed, needs_approval and
    human_only both map to needs_approval (human_only marks Enterprise, where no window applies),
    declined stays declined."""

    ALLOW_AUTO_WITH_CONFIRM = "allow_auto_with_confirm"
    NEEDS_APPROVAL = "needs_approval"
    HUMAN_ONLY = "human_only"
    DECLINED = "declined"


@dataclass(frozen=True)
class ChargeFacts:
    """What: the billing-record facts about one charge. Use when: building evaluate input.
    Inputs: id, amount in cents, aware charge time, status, speakable label (e.g. "invoice ending 0412"),
    optional spoken label. Output: frozen record. Side effects: none.
    Role: the amount always comes from the billing record, never from the model or the caller."""

    charge_id: str
    amount_cents: int
    charged_at: datetime
    status: ChargeStatus
    label: str
    label_spoken: str | None = None


@dataclass(frozen=True)
class PriorRefund:
    """What: one completed refund on the account. Use when: the 90-day rule and ALREADY_REFUNDED run.
    Inputs: the refunded charge id and the business date it completed. Output: frozen record.
    Side effects: none. Role: only completed refunds are passed (doc 07 s3.3 X5); in-flight ones are not."""

    charge_id: str
    effective_date: date


@dataclass(frozen=True)
class RefundEvaluation:
    """What: the result of evaluate. Use when: prepare builds a PrepareResult or a pending row.
    Inputs: set by evaluate. Output: frozen record. Side effects: none.
    Role: outcome and reason_codes (primary first) drive the wire decision; params are frozen for the
    commit re-check; followup_ticket_queue tells core to open the past-due billing ticket."""

    outcome: Outcome
    reason_codes: tuple[ReasonCode, ...]
    policy_decision: Literal["allowed", "needs_approval", "declined"]
    policy_refs: tuple[PolicyRefCfg, ...]
    message: str | None = None
    readback: Readback | None = None
    params: dict[str, object] = field(default_factory=dict)
    followup_ticket_queue: str | None = None


def _business_date(moment: datetime, tz: ZoneInfo) -> date:
    """What: the calendar date of an instant in the policy timezone. Use when: computing charge age.
    Inputs: aware datetime, zone. Output: date. Side effects: none.
    Role: age is in business days (calendar dates), so DST and UTC offsets cannot shift a boundary."""
    if moment.tzinfo is None:
        raise ValueError("evaluate needs aware datetimes (naive given)")
    return moment.astimezone(tz).date()


def _dollars(cents: int) -> str:
    """What: format cents as dollars. Use when: filling {amount}. Inputs: int cents. Output: "$96.00".
    Side effects: none. Role: read-back text."""
    return f"${cents // 100:,}.{cents % 100:02d}"


def _verdict(policy: PolicyConfig, codes: tuple[ReasonCode, ...], outcome: Outcome, *,
             charge: ChargeFacts | None = None, charge_date: date | None = None,
             followup: bool = False, variables: dict[str, object] | None = None) -> RefundEvaluation:
    """What: build the result for a set of codes from policy.yaml. Use when: a rule has fired.
    Inputs: policy, ordered codes, outcome, charge and its business date (not declined), follow-up
    flag, message variables. Output: RefundEvaluation. Side effects: none.
    Role: single place that turns codes into refs, message and read-back text."""
    primary = policy.reason_codes[codes[0]]
    refs = tuple(dict.fromkeys(policy.reason_codes[c].policy_ref for c in codes))
    if outcome is Outcome.DECLINED:
        message = (primary.message or "").format_map(variables or {})
        queue = primary.followup_queue if followup else None
        return RefundEvaluation(outcome, codes, primary.decision, refs, message=message,
                                followup_ticket_queue=queue)
    assert charge is not None and charge_date is not None
    action = policy.actions.refund
    template = action.readback if outcome is Outcome.ALLOW_AUTO_WITH_CONFIRM else action.readback_approval
    spoken = charge.label_spoken or charge.label
    day = f"{charge_date.day} {charge_date:%B}"
    fill = {"amount": _dollars(charge.amount_cents), "charge_date": day, "charge_date_spoken": day,
            "charge_label": charge.label, "charge_label_spoken": spoken}
    readback = Readback(chat=template.chat.format_map(fill), voice=template.voice.format_map(fill))
    params: dict[str, object] = {"charge_id": charge.charge_id, "amount_cents": charge.amount_cents}
    return RefundEvaluation(outcome, codes, primary.decision, refs, readback=readback, params=params)


def evaluate(*, tier: Tier, contract_status: ContractStatus, role: Role, charge: ChargeFacts,
             prior_refunds: tuple[PriorRefund, ...], policy: PolicyConfig,
             now: datetime) -> RefundEvaluation:
    """What: decide a refund for one charge by the L1 A1 rules, all limits from policy.yaml.
    Use when: prepare and the commit or submit re-check (same function, so they cannot disagree).
    Inputs: tier, contract_status, role, the charge, the account's completed refunds, PolicyConfig, aware now.
    Output: RefundEvaluation; Pro within limits is allow_auto_with_confirm, Pro late or over cap or
    second in 90 days and every Business refund inside 30 days is needs_approval, Enterprise is
    human_only, everything else is declined. Order: role, Free, trialing, past_due (first for every
    tier, with the billing follow-up flag), already refunded, not collected, then tier rules.
    Side effects: none; raises ValueError for a naive datetime.
    Role: the rule engine for A1; ages are business-date days, boundaries inclusive."""
    tz = ZoneInfo(policy.timezone)
    today = _business_date(now, tz)
    charge_date = _business_date(charge.charged_at, tz)
    cfg = policy.actions.refund
    codes = ReasonCode

    def declined(code: ReasonCode, *, followup: bool = False, **variables: object) -> RefundEvaluation:
        """What: a declined verdict. Use when: a rule rejects. Inputs: code, flag, message vars.
        Output: RefundEvaluation. Side effects: none. Role: local shorthand."""
        return _verdict(policy, (code,), Outcome.DECLINED, followup=followup, variables=variables)

    if role not in policy.roles.billing_roles:
        return declined(codes.ROLE_NOT_PERMITTED)
    if tier == "free":
        return declined(codes.FREE_NOTHING_CHARGED)
    if contract_status == "trialing":
        return declined(codes.TRIAL_NOTHING_TO_REFUND)
    if contract_status in cfg.blocked_statuses:  # past_due; trialing is caught above with its own code
        return declined(codes.PAST_DUE, followup=True)

    already = charge.status == "refunded" or any(p.charge_id == charge.charge_id for p in prior_refunds)
    if already:
        return declined(codes.ALREADY_REFUNDED)
    age = (today - charge_date).days
    if charge.status != "paid" or age < 0:
        return declined(codes.CHARGE_NOT_REFUNDABLE)

    def verdict(outcome: Outcome, *reasons: ReasonCode) -> RefundEvaluation:
        """What: an allow or approval verdict for this charge. Use when: a rule accepts. Inputs:
        outcome, ordered codes. Output: RefundEvaluation. Side effects: none. Role: local shorthand."""
        return _verdict(policy, reasons, outcome, charge=charge, charge_date=charge_date)

    if tier == "enterprise":
        return verdict(Outcome.HUMAN_ONLY, codes.ENTERPRISE_HUMAN_DECIDES)

    if tier == "business":
        window = cfg.tiers.business.window_days or 0
        if age <= window:
            return verdict(Outcome.NEEDS_APPROVAL, codes.BUSINESS_APPROVAL_REQUIRED)
        return declined(codes.OUTSIDE_WINDOW, window=window, age=age)

    pro = cfg.tiers.pro
    window, late, cap = pro.window_days or 0, pro.late_band_days or 0, pro.auto_cap_cents or 0
    if age > window + late:
        return declined(codes.PRO_PAST_LATE_BAND, window=window, late=late, age=age)
    reasons: list[ReasonCode] = []
    if age > window:
        reasons.append(codes.PRO_LATE_IN_BAND)
    if charge.amount_cents > cap:
        reasons.append(codes.PRO_OVER_CAP)
    since = today - timedelta(days=cfg.repeat_window_days)
    if any(p.effective_date >= since for p in prior_refunds):
        reasons.append(codes.PRO_SECOND_IN_REPEAT_WINDOW)
    if reasons:
        return verdict(Outcome.NEEDS_APPROVAL, *reasons)
    return verdict(Outcome.ALLOW_AUTO_WITH_CONFIRM, codes.PRO_WITHIN_LIMITS)
