"""What: unit tests (no database) for the seed data files and the seed planner: shape, personas, PIN hashing,
relative dates under a frozen clock, and refund reason-code triggers.
Use when: changing data/seeds/*.yaml or core/seeds. Inputs: the YAML files under data/seeds. Output: pytest results.
Side effects: none. Role: CS-E-09.S4.T1 Done-when (frozen-clock reseed keeps Natasha's duplicate 5 days old; every
refund reason code has a seeded trigger) plus the invariants the later engines and the landing page rely on."""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from contracts.agent_api import Persona
from contracts.policy import load_policy
from contracts.reason_codes import ReasonCode

from core.seeds.seed import SeedError, load_seed_data, pin_hmac, plan_rows
from tests.conftest import POLICY

NY = ZoneInfo("America/New_York")
PEPPER = "unit-test-pepper"
# doc 07 s13.1 table: the codes the refund rules can return (the rest belong to billing email and grace extension).
REFUND_CODES = ["ROLE_NOT_PERMITTED", "FREE_NOTHING_CHARGED", "TRIAL_NOTHING_TO_REFUND", "PAST_DUE", "ALREADY_REFUNDED",
                "CHARGE_NOT_REFUNDABLE", "PRO_PAST_LATE_BAND", "OUTSIDE_WINDOW", "PRO_LATE_IN_BAND", "PRO_OVER_CAP",
                "PRO_SECOND_IN_REPEAT_WINDOW", "BUSINESS_APPROVAL_REQUIRED", "ENTERPRISE_HUMAN_DECIDES",
                "PRO_WITHIN_LIMITS"]
OTHER_CODES = ["EMAIL_DAILY_LIMIT", "EMAIL_NO_CHANGE", "EMAIL_STEP_UP", "EMAIL_ENTERPRISE_APPROVAL",
               "NO_BILLING_ON_PLAN", "GRACE_NOT_PAST_DUE", "GRACE_ALREADY_USED", "GRACE_WITHIN_LIMITS",
               "GRACE_ENTERPRISE_APPROVAL"]
DAYS = [date(2026, 11, 27), date(2026, 11, 3), date(2027, 3, 15), date(2026, 12, 31)]  # incl. just after the Nov DST end


@pytest.fixture(scope="module")
def data():
    """What: the parsed seed files. Use when: any seed test. Inputs: none. Output: SeedData.
    Side effects: reads data/seeds. Role: shared fixture."""
    return load_seed_data()


def rows_on(data, day: date):
    """What: plan the rows as if today were `day` (noon New York). Use when: date tests. Inputs: data, a date.
    Output: planned rows. Side effects: none. Role: frozen-clock helper."""
    return plan_rows(data, datetime(day.year, day.month, day.day, 12, 0, tzinfo=NY), PEPPER)


def test_requirements_reason_codes_are_split_completely():
    """What: REFUND_CODES plus OTHER_CODES are exactly the ReasonCode enum. Use when: always. Inputs: none.
    Output: assertions. Side effects: none. Role: guards the transcription above against a new code."""
    assert sorted(REFUND_CODES + OTHER_CODES) == sorted(c.value for c in ReasonCode)


def test_ids_are_unique_and_match_the_database_formats(data):
    """What: account, user and charge ids are unique, well formed and every foreign key resolves.
    Use when: always. Inputs: data. Output: assertions. Side effects: none. Role: the migration's CHECK constraints."""
    assert all(re.fullmatch(r"QS-\d{5}", a.id) for a in data.accounts)
    assert all(re.fullmatch(r"U-\d{4}", u.id) for u in data.users)
    assert all(re.fullmatch(r"CH-\d{8}", c.id) for c in data.charges)
    for name, ids in (("accounts", [a.id for a in data.accounts]), ("users", [u.id for u in data.users]),
                      ("charges", [c.id for c in data.charges])):
        assert len(ids) == len(set(ids)), name
    accounts = {a.id for a in data.accounts}
    assert {u.account_id for u in data.users} <= accounts and {c.account_id for c in data.charges} <= accounts
    assert len({c.id[-4:] for c in data.charges}) == len(data.charges)  # "invoice ending 0412" must be unambiguous


def test_personas_validate_against_the_contract_and_match_the_users(data):
    """What: every personas.yaml entry parses as contracts Persona; every user has exactly one persona by
    (account_id, name) and role, tier and status agree with the account and user rows. Use when: always.
    Inputs: data. Output: assertions. Side effects: none. Role: doc 07 s2.2 GET /api/demo/personas source."""
    personas = [Persona.model_validate(p) for p in data.personas_raw]
    accounts = {a.id: a for a in data.accounts}
    assert len(personas) == len(data.users)
    for u in data.users:
        match = [p for p in personas if (p.account_id, p.name) == (u.account_id, u.display_name)]
        assert len(match) == 1, u
        p, a = match[0], accounts[u.account_id]
        assert (p.role, p.tier, p.contract_status) == (u.role, a.tier, a.contract_status)


def test_scenario_coverage(data):
    """What: Natasha covers S1, S2, S5; Peter and Tony cover S3 on one account; some persona covers S4; a past_due
    and a trialing persona exist and are tagged "rules". Use when: always. Inputs: data. Output: assertions.
    Side effects: none. Role: L1 s4 (one persona per scenario plus rule personas)."""
    by_name = {p["name"]: p for p in data.personas_raw}
    assert by_name["Natasha Romanoff"]["scenarios"] == ["S1", "S2", "S5"]
    assert "S3" in by_name["Peter Parker"]["scenarios"] and "S3" in by_name["Tony Stark"]["scenarios"]
    assert by_name["Peter Parker"]["account_id"] == by_name["Tony Stark"]["account_id"]
    assert (by_name["Peter Parker"]["role"], by_name["Tony Stark"]["role"]) == ("member", "owner")
    assert any("S4" in p["scenarios"] for p in data.personas_raw)
    for status in ("past_due", "trialing"):
        assert any(p["contract_status"] == status and "rules" in p["scenarios"] for p in data.personas_raw)
    assert {s for p in data.personas_raw for s in p["scenarios"]} >= {"S1", "S2", "S3", "S4", "S5", "rules"}


def test_pins_are_four_digits_unique_per_account_and_only_in_personas(data):
    """What: each PIN is 4 digits and unique within its account, and no other seed file has a pin key.
    Use when: always. Inputs: data. Output: assertions. Side effects: reads the seed files.
    Role: L1 s12 (demo PINs are public on the landing page) and the single-source rule for the web bundle check."""
    seen = set()
    for p in data.personas_raw:
        assert re.fullmatch(r"\d{4}", p["pin"])
        assert (p["account_id"], p["pin"]) not in seen
        seen.add((p["account_id"], p["pin"]))
    for path in data.seeds_dir.glob("*.yaml"):
        if path.name != "personas.yaml":
            assert not re.search(r"(?i)\bpin\b\s*:", path.read_text()), path.name


def test_pin_hmac_is_hmac_sha256_over_account_and_pin_with_the_pepper():
    """What: pin_hmac equals HMAC-SHA256(pepper, "account:pin"), changes with the pepper, and refuses an empty
    pepper. Use when: always. Inputs: none. Output: assertions. Side effects: none. Role: draft C decision C-6."""
    expected = hmac.new(b"p1", b"QS-10423:7342", hashlib.sha256).digest()
    assert pin_hmac("p1", "QS-10423", "7342") == expected
    assert pin_hmac("p2", "QS-10423", "7342") != expected
    assert pin_hmac("p1", "QS-10424", "7342") != expected
    with pytest.raises(SeedError):
        pin_hmac("", "QS-10423", "7342")


def test_planned_users_carry_the_hmac_not_the_pin(data):
    """What: the planned user rows hold the HMAC of the persona's PIN and no plain PIN. Use when: always.
    Inputs: data. Output: assertions. Side effects: none. Role: core never stores a plain PIN."""
    rows = rows_on(data, DAYS[0])
    by_key = {(p["account_id"], p["name"]): p["pin"] for p in data.personas_raw}
    for u in rows.users:
        pin = by_key[(u["account_id"], u["display_name"])]
        assert u["pin_hmac"] == pin_hmac(PEPPER, u["account_id"], pin)
        assert pin not in repr({k: v for k, v in u.items() if k != "pin_hmac"})


@pytest.mark.parametrize("day", DAYS)
def test_natashas_duplicate_charge_is_five_days_old_whatever_the_day(data, day):
    """What: planning on four different days (one just after the autumn DST change) always yields the duplicate
    $96 charge on the local date exactly 5 days earlier, same amount as its twin. Use when: always.
    Inputs: data, a day. Output: assertions. Side effects: none.
    Role: Done-when 'reseeding on two different days keeps Natasha's duplicate charge 5 days old'."""
    rows = rows_on(data, day)
    dup = next(c for c in rows.charges if c["id"] == "CH-00000412")
    twin = next(c for c in rows.charges if c["id"] == "CH-00000411")
    assert (day - dup["charged_at"].astimezone(NY).date()).days == 5
    assert dup["charged_at"].astimezone(NY).date() == twin["charged_at"].astimezone(NY).date()
    assert dup["amount_cents"] == twin["amount_cents"] == 9600 and dup["account_id"] == "QS-10423"


def test_two_plans_on_different_days_differ_only_by_the_shift(data):
    """What: the same charge moves by exactly the number of days between the two plans. Use when: always.
    Inputs: data. Output: assertions. Side effects: none. Role: proves the dates are relative, not frozen text."""
    a, b = rows_on(data, date(2026, 11, 27)), rows_on(data, date(2026, 12, 4))
    ca = {c["id"]: c["charged_at"].astimezone(NY).date() for c in a.charges}
    cb = {c["id"]: c["charged_at"].astimezone(NY).date() for c in b.charges}
    assert ca != cb and all((cb[k] - ca[k]).days == 7 for k in ca)


def test_refunded_charges_have_a_matching_history_row(data):
    """What: each charge with status refunded has refunded_at equal to a refund history row on the same charge,
    and no history row points at a non-refunded charge. Use when: always. Inputs: data. Output: assertions.
    Side effects: none. Role: ah_refund_once and charges.refunded_at must tell one story."""
    rows = rows_on(data, DAYS[0])
    hist = {h["target_key"]: h for h in rows.history}
    refunded = {c["id"]: c for c in rows.charges if c["status"] == "refunded"}
    assert set(hist) == {f"refund:{cid}" for cid in refunded} and refunded
    for cid, c in refunded.items():
        assert c["refunded_at"].astimezone(NY).date() == hist[f"refund:{cid}"]["effective_date"]
        assert c["refunded_at"] >= c["charged_at"]


def test_seed_date_is_todays_local_date(data):
    """What: the plan's seed_date is the clock's local date as YYYY-MM-DD. Use when: always. Inputs: data.
    Output: assertion. Side effects: none. Role: the value /readyz reports."""
    assert rows_on(data, date(2026, 12, 4)).seed_date == "2026-12-04"
    late = plan_rows(data, datetime(2026, 11, 28, 3, 30, tzinfo=ZoneInfo("UTC")), PEPPER)  # 22:30 on the 27th in New York
    assert late.seed_date == "2026-11-27"


# ---- refund reason-code triggers -------------------------------------------------------------------------------

def applicable_codes(data, persona_name: str, charge_id: str | None, today: date) -> list[str]:
    """What: an independent reading of L1 s5 A1 over the seed rows: every refund reason code that applies to
    (persona, charge) on `today`, in precedence order. Use when: checking triggers. Inputs: data, persona name,
    charge id or None, today. Output: codes. Side effects: none.
    Role: a reference evaluator that lives in the test, so a seed that drifts from the rules fails here; the real
    engine (CS-E-03.S2) is tested separately and may reuse these cases."""
    policy = load_policy(POLICY).actions.refund
    rows = rows_on(data, today)
    user = next(u for u in rows.users if u["display_name"] == persona_name)
    acct = next(a for a in rows.accounts if a["id"] == user["account_id"])
    if user["role"] not in ("owner", "billing_admin"):
        return ["ROLE_NOT_PERMITTED"]
    if acct["tier"] == "free":
        return ["FREE_NOTHING_CHARGED"]
    if acct["contract_status"] == "trialing":
        return ["TRIAL_NOTHING_TO_REFUND"]
    if acct["contract_status"] == "past_due":
        return ["PAST_DUE"]
    charge = next(c for c in rows.charges if c["id"] == charge_id)
    assert charge["account_id"] == acct["id"]
    if charge["status"] == "refunded":
        return ["ALREADY_REFUNDED"]
    if charge["status"] == "failed":
        return ["CHARGE_NOT_REFUNDABLE"]
    age = (today - charge["charged_at"].astimezone(NY).date()).days
    if acct["tier"] == "enterprise":
        return ["ENTERPRISE_HUMAN_DECIDES"]
    if acct["tier"] == "business":
        window = policy.tiers.business.window_days
        assert window is not None
        return ["OUTSIDE_WINDOW"] if age > window else ["BUSINESS_APPROVAL_REQUIRED"]
    pro = policy.tiers.pro
    assert pro.window_days is not None and pro.late_band_days is not None and pro.auto_cap_cents is not None
    window, late = pro.window_days, pro.late_band_days
    if age > window + late:
        return ["PRO_PAST_LATE_BAND"]
    codes = []
    if age > window:
        codes.append("PRO_LATE_IN_BAND")
    if charge["amount_cents"] > pro.auto_cap_cents:
        codes.append("PRO_OVER_CAP")
    recent = [h for h in rows.history if h["account_id"] == acct["id"] and h["action_type"] == "refund"
              and (today - h["effective_date"]).days <= policy.repeat_window_days]
    if recent:
        codes.append("PRO_SECOND_IN_REPEAT_WINDOW")
    return codes or ["PRO_WITHIN_LIMITS"]


def test_every_refund_reason_code_has_exactly_one_seeded_trigger(data):
    """What: triggers.yaml names a (persona, charge) for each of the 14 refund codes and nothing else; reading the
    seed rows with the L1 rules gives exactly that one code (no second code competes), on four different days.
    Use when: always. Inputs: data. Output: assertions. Side effects: none.
    Role: Done-when 'every refund reason code has a seeded trigger'."""
    assert sorted(data.triggers) == sorted(REFUND_CODES)
    assert set(data.triggers) == {c.value for c in ReasonCode} - set(OTHER_CODES)
    for day in DAYS:
        for code, t in data.triggers.items():
            assert applicable_codes(data, t["persona"], t["charge"], day) == [code], (code, day)


def test_the_reference_evaluator_can_disagree(data):
    """What: pointing a trigger at the wrong charge changes the answer. Use when: always. Inputs: data.
    Output: assertion. Side effects: none. Role: the check above is not vacuous."""
    t = data.triggers["PRO_WITHIN_LIMITS"]
    assert applicable_codes(data, t["persona"], "CH-00000412", DAYS[0]) == ["PRO_WITHIN_LIMITS"]
    other = data.triggers["PRO_OVER_CAP"]
    assert applicable_codes(data, t["persona"], t["charge"], DAYS[0]) != applicable_codes(
        data, other["persona"], other["charge"], DAYS[0])
