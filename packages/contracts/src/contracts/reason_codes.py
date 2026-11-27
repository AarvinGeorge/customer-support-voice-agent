"""What: the frozen list of decision reason codes (doc 07 s13.1 table).
Use when: any service or test needs the closed set of codes (core engines, agent, evals).
Inputs: none.
Output: ReasonCode, a string enum whose values equal the keys of policy.yaml reason_codes.
Side effects: none.
Role: single Python source of the code list; a CI test compares it with data/policy.yaml."""
from enum import StrEnum


class ReasonCode(StrEnum):
    """What: every reason code a prepare, commit or ticket decision can cite.
    Use when: returning or comparing a decision code.
    Inputs: none. Output: the enum. Side effects: none.
    Role: doc 07 s13.1 frozen list; adding a member needs a decision entry and a YAML key."""

    ROLE_NOT_PERMITTED = "ROLE_NOT_PERMITTED"
    FREE_NOTHING_CHARGED = "FREE_NOTHING_CHARGED"
    TRIAL_NOTHING_TO_REFUND = "TRIAL_NOTHING_TO_REFUND"
    PAST_DUE = "PAST_DUE"
    ALREADY_REFUNDED = "ALREADY_REFUNDED"
    CHARGE_NOT_REFUNDABLE = "CHARGE_NOT_REFUNDABLE"
    PRO_PAST_LATE_BAND = "PRO_PAST_LATE_BAND"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    PRO_LATE_IN_BAND = "PRO_LATE_IN_BAND"
    PRO_OVER_CAP = "PRO_OVER_CAP"
    PRO_SECOND_IN_REPEAT_WINDOW = "PRO_SECOND_IN_REPEAT_WINDOW"
    BUSINESS_APPROVAL_REQUIRED = "BUSINESS_APPROVAL_REQUIRED"
    ENTERPRISE_HUMAN_DECIDES = "ENTERPRISE_HUMAN_DECIDES"
    PRO_WITHIN_LIMITS = "PRO_WITHIN_LIMITS"
    EMAIL_DAILY_LIMIT = "EMAIL_DAILY_LIMIT"
    EMAIL_NO_CHANGE = "EMAIL_NO_CHANGE"
    EMAIL_STEP_UP = "EMAIL_STEP_UP"
    EMAIL_ENTERPRISE_APPROVAL = "EMAIL_ENTERPRISE_APPROVAL"
    NO_BILLING_ON_PLAN = "NO_BILLING_ON_PLAN"
    GRACE_NOT_PAST_DUE = "GRACE_NOT_PAST_DUE"
    GRACE_ALREADY_USED = "GRACE_ALREADY_USED"
    GRACE_WITHIN_LIMITS = "GRACE_WITHIN_LIMITS"
    GRACE_ENTERPRISE_APPROVAL = "GRACE_ENTERPRISE_APPROVAL"
