"""What: tests for data/policy.yaml and the frozen PolicyConfig loader (doc 07 s13.1, CS-E-09.S2.T1).
Use when: CI (contracts suite). Inputs: the committed data/policy.yaml and temp copies of it.
Output: pass or fail. Side effects: writes temp files only.
Role: proves each Done-when item and that policy numbers equal L1 s5 and s6."""
import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from contracts.policy import (PolicyConfig, PolicyError, load_policy, policy_version,
                              render_placeholder)
from contracts.reason_codes import ReasonCode

REPO = Path(__file__).resolve().parents[3]
POLICY = REPO / "data" / "policy.yaml"
APPROVAL_TAIL = "Shall I send this to our team for approval?"


def _articles_dir() -> Path:
    """What: data/articles, or an explicit named skip (fail when REQUIRE_ARTICLES=1).
    Use when: the two article agreement tests. Inputs: env REQUIRE_ARTICLES. Output: Path.
    Side effects: skips or fails the calling test. Role: the Help Center articles land in PR #10
    (branch cs-e-09-s3-articles, CS-E-09.S3.T5); until that merges these tests are skipped by name,
    after it they run on every CI pass."""
    art = REPO / "data" / "articles"
    if art.is_dir() and any(art.glob("*.md")):
        return art
    msg = "SKIPPED until PR #10 (cs-e-09-s3-articles, CS-E-09.S3.T5) adds data/articles"
    if os.environ.get("REQUIRE_ARTICLES") == "1":
        pytest.fail(msg.replace("SKIPPED", "FAILED"))
    pytest.skip(msg)


def _raw() -> dict:
    """What: parsed committed YAML. Use when: building mutated variants. Inputs: none.
    Output: dict. Side effects: reads a file. Role: test helper."""
    return yaml.safe_load(POLICY.read_text())


def _write(tmp_path: Path, data: dict) -> Path:
    """What: dump a dict to a temp YAML. Use when: mutation tests. Inputs: tmp dir, dict.
    Output: path. Side effects: writes a file. Role: test helper."""
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return p


def test_loads_and_is_frozen():
    """What: the committed file validates and the model is immutable. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: frozen loader proof."""
    cfg = load_policy(POLICY)
    assert isinstance(cfg, PolicyConfig)
    with pytest.raises(Exception):
        cfg.timezone = "UTC"  # type: ignore[misc]


def test_unknown_key_raises(tmp_path):
    """What: an unknown nested key is rejected. Use when: CI. Inputs: mutated YAML.
    Output: assertion. Side effects: temp file. Role: extra=forbid at every level."""
    d = _raw()
    d["actions"]["refund"]["tiers"]["pro"]["auto_cap_centz"] = 1
    with pytest.raises(PolicyError, match="auto_cap_centz"):
        load_policy(_write(tmp_path, d))
    d = _raw()
    d["surprise"] = True
    with pytest.raises(PolicyError, match="surprise"):
        load_policy(_write(tmp_path, d))


def test_unknown_key_exits_non_zero(tmp_path):
    """What: the startup entry point exits non-zero on a bad file. Use when: CI.
    Inputs: mutated YAML on disk. Output: assertion on return code and stderr.
    Side effects: runs a subprocess. Role: Done-when 'unknown key makes core exit non-zero'."""
    d = _raw()
    d["identity"]["pin_digitz"] = 4
    bad = _write(tmp_path, d)
    run = subprocess.run([sys.executable, "-m", "contracts.policy", str(bad)],
                         capture_output=True, text=True,
                         env={"PYTHONPATH": str(REPO / "packages/contracts/src")})
    assert run.returncode != 0
    assert "pin_digitz" in run.stderr
    ok = subprocess.run([sys.executable, "-m", "contracts.policy", str(POLICY)],
                        capture_output=True, text=True,
                        env={"PYTHONPATH": str(REPO / "packages/contracts/src")})
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip().startswith("policy_version=")


def test_reason_codes_equal_enum():
    """What: YAML code list equals the ReasonCode enum exactly. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: Done-when enum test."""
    cfg = load_policy(POLICY)
    assert set(cfg.reason_codes) == {c.value for c in ReasonCode}
    assert len(ReasonCode) == 23


def test_enum_drift_is_detected(tmp_path):
    """What: a missing or extra code fails the load. Use when: CI. Inputs: mutated YAML.
    Output: assertion. Side effects: temp file. Role: zero-proof (the check can fail)."""
    d = _raw()
    del d["reason_codes"]["PAST_DUE"]
    with pytest.raises(PolicyError, match="PAST_DUE"):
        load_policy(_write(tmp_path, d))
    d = _raw()
    d["reason_codes"]["MADE_UP"] = copy.deepcopy(d["reason_codes"]["PRO_WITHIN_LIMITS"])
    with pytest.raises(PolicyError, match="MADE_UP"):
        load_policy(_write(tmp_path, d))


def test_every_approval_readback_ends_with_the_question(tmp_path):
    """What: all readback_approval texts end with the fixed question. Use when: CI.
    Inputs: loaded config and a mutated copy. Output: assertion. Side effects: temp file.
    Role: Done-when readback rule, with a failing variant."""
    cfg = load_policy(POLICY)
    seen = 0
    for name in ("refund", "grace_extension"):
        act = getattr(cfg.actions, name)
        for text in (act.readback_approval.chat, act.readback_approval.voice):
            assert text.endswith(APPROVAL_TAIL), (name, text)
            seen += 1
    assert seen == 4
    d = _raw()
    d["actions"]["refund"]["readback_approval"]["voice"] = "Okay to send? Shall I send it?"
    with pytest.raises(PolicyError, match="readback_approval"):
        load_policy(_write(tmp_path, d))


def test_policy_version_stable_and_sensitive(tmp_path):
    """What: same bytes give the same version; one changed byte changes it. Use when: CI.
    Inputs: file and a mutated copy. Output: assertion. Side effects: temp file.
    Role: Done-when policy_version hash."""
    v1, v2 = policy_version(POLICY), policy_version(POLICY)
    assert v1 == v2 == load_policy(POLICY).policy_version
    assert v1.startswith(load_policy(POLICY).version_label + "+")
    d = _raw()
    d["actions"]["refund"]["tiers"]["pro"]["auto_cap_cents"] = 15100
    assert policy_version(_write(tmp_path, d)) != v1


def test_numbers_equal_l1_s5_s6():
    """What: every number equals L1 s5 (A1, A3, A4), s6 and s10. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: L1 is the value authority."""
    c = load_policy(POLICY)
    r = c.actions.refund
    assert (r.repeat_window_days, r.pending_ttl_seconds) == (90, 600)
    assert (r.tiers.pro.window_days, r.tiers.pro.late_band_days, r.tiers.pro.auto_cap_cents) == (14, 7, 15000)
    assert r.tiers.business.window_days == 30 and r.tiers.business.approval_only
    assert r.tiers.enterprise.approval_only and not r.tiers.free.eligible
    assert tuple(r.blocked_statuses) == ("trialing", "past_due")
    g = c.actions.grace_extension
    assert g.enabled is False and g.requires_status == "past_due"
    assert (g.tiers.pro.days, g.tiers.pro.once_per_months) == (7, 12)
    assert (g.tiers.business.days, g.tiers.business.once_per_months) == (14, 6)
    assert g.tiers.enterprise.approval_only and not g.tiers.free.eligible
    assert (c.tiers.pro.price_cents_per_seat_month, c.tiers.business.price_cents_per_seat_month) == (800, 1600)
    assert c.identity.pin_digits == 4 and c.identity.max_failures_per_session == 3
    t = c.tickets
    assert (t.limits.verified_per_account_per_day, t.limits.unverified_per_session) == (5, 2)
    assert dict(t.priority_by_tier) == {"enterprise": "P2", "business": "P2", "pro": "P3", "free": "P4",
                                  "unverified": "P4"}
    assert t.enterprise_blocking_priority == "P1"
    assert dict(t.target_label) == {"P1": "1 hour", "P2_enterprise": "4 hours", "P2": "8 hours",
                              "P3": "24 hours", "P4": "72 hours"}
    assert tuple(t.queues) == ("billing", "technical", "account", "approvals")
    assert c.timezone == "America/New_York" and c.currency == "USD"


def test_category_queue_covers_ticket_categories():
    """What: category_queue keys equal the TicketCategory literal. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: doc 07 s13.1 'keys exactly the s3.4 list'."""
    from typing import get_args

    from contracts.core_api import TicketCategory
    c = load_policy(POLICY)
    assert set(c.tickets.category_queue) == set(get_args(TicketCategory))
    assert set(c.tickets.category_queue.values()) <= set(c.tickets.queues)


def test_declined_codes_have_safe_messages():
    """What: every declined code has a non-empty message; allowed codes may not. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: doc 07 s13.1 message rule."""
    c = load_policy(POLICY)
    for code, rc in c.reason_codes.items():
        if rc.decision == "declined":
            assert rc.message and "{" not in rc.message.replace("{window}", "").replace(
                "{late}", "").replace("{age}", ""), code
        assert chr(0x2014) not in (rc.message or "")
    assert c.reason_codes[ReasonCode("PAST_DUE")].followup_queue == "billing"
    assert c.reason_codes[ReasonCode("EMAIL_STEP_UP")].requires_step_up is True


def test_article_refs_match_staged_articles():
    """What: every policy_ref article and section slug exists in data/articles.
    Use when: CI once data/articles exists (PR #10, CS-E-09.S3.T5); set REQUIRE_ARTICLES=1 to fail
    instead of skip. Inputs: data/articles. Output: assertion or named skip. Side effects: none. Role: keeps refs citable."""
    staging = _articles_dir()
    import re
    c = load_policy(POLICY)
    for code, rc in c.reason_codes.items():
        text = (staging / f"{rc.policy_ref.article}.md").read_text()
        slugs = {re.sub(r"[^a-z0-9]+", "-", h.lower()).strip("-")
                 for h in re.findall(r"^#{2,3} (.+)$", text, re.M)}
        assert rc.policy_ref.section in slugs, (code, rc.policy_ref)


def test_render_placeholder_formatters():
    """What: {policy.key} lookup with |dollars and |days. Use when: CI. Inputs: loaded config.
    Output: assertion. Side effects: none. Role: knowledge ingest helper (07 s13.1 last bullets)."""
    c = load_policy(POLICY)
    assert render_placeholder(c, "actions.refund.tiers.pro.auto_cap_cents|dollars") == "$150"
    assert render_placeholder(c, "actions.refund.tiers.pro.window_days|days") == "14 days"
    assert render_placeholder(c, "tickets.limits.verified_per_account_per_day") == "5"
    with pytest.raises(PolicyError, match="nope"):
        render_placeholder(c, "actions.refund.nope")


def test_numbers_appear_in_staged_articles():
    """What: each policy number is stated in its staged article. Use when: CI in the full repo.
    Inputs: data/articles. Output: assertion or named skip. Side effects: none.
    Role: L1 s6 consistency check for the staged corpus (full checker lands with CS-E-02.S1.T3)."""
    staging = _articles_dir()
    c = load_policy(POLICY)
    facts = {
        "refund-policy": ["within 14 days of the charge", "up to $150", "90 days", "7 days after",
                          "within 30 days of the charge"],
        "billing-invoices-payments": ["at most once per day", "7 days, once per 12 months",
                                      "14 days, once per 6 months"],
        "plans-pricing-contracts": ["$8 per seat per month", "$16 per seat per month"],
        "support-and-escalation": ["5 tickets per day", "up to 2 tickets", "72 hours", "24 hours",
                                   "8 hours", "4 hours", "1 hour"],
    }
    for article, phrases in facts.items():
        text = (staging / f"{article}.md").read_text()
        for ph in phrases:
            assert ph in text, (article, ph)
    assert render_placeholder(c, "tiers.pro.price_cents_per_seat_month|dollars") == "$8"
    assert render_placeholder(c, "tiers.business.price_cents_per_seat_month|dollars") == "$16"
    bogus = "within 15 days of the charge"
    assert bogus not in (staging / "refund-policy.md").read_text()


# ---- fix round 1 (review of PR #7) ----

DECISION_TABLE = [
    ("ROLE_NOT_PERMITTED", "declined", "roles-and-permissions", "billing-permissions"),
    ("FREE_NOTHING_CHARGED", "declined", "refund-policy", "free-plan"),
    ("TRIAL_NOTHING_TO_REFUND", "declined", "refund-policy", "trials"),
    ("PAST_DUE", "declined", "refund-policy", "past-due-accounts"),
    ("ALREADY_REFUNDED", "declined", "refund-policy", "how-refunds-work"),
    ("CHARGE_NOT_REFUNDABLE", "declined", "refund-policy", "how-refunds-work"),
    ("PRO_PAST_LATE_BAND", "declined", "refund-policy", "pro-plan-refunds"),
    ("OUTSIDE_WINDOW", "declined", "refund-policy", "business-plan-refunds"),
    ("PRO_LATE_IN_BAND", "needs_approval", "refund-policy", "pro-plan-refunds"),
    ("PRO_OVER_CAP", "needs_approval", "refund-policy", "pro-plan-refunds"),
    ("PRO_SECOND_IN_REPEAT_WINDOW", "needs_approval", "refund-policy", "pro-plan-refunds"),
    ("BUSINESS_APPROVAL_REQUIRED", "needs_approval", "refund-policy", "business-plan-refunds"),
    ("ENTERPRISE_HUMAN_DECIDES", "needs_approval", "support-and-escalation", "approvals"),
    ("PRO_WITHIN_LIMITS", "allowed", "refund-policy", "pro-plan-refunds"),
    ("EMAIL_DAILY_LIMIT", "declined", "billing-invoices-payments", "billing-contact"),
    ("EMAIL_NO_CHANGE", "declined", "billing-invoices-payments", "billing-contact"),
    ("EMAIL_STEP_UP", "allowed", "billing-invoices-payments", "billing-contact"),
    ("EMAIL_ENTERPRISE_APPROVAL", "needs_approval", "billing-invoices-payments", "billing-contact"),
    ("NO_BILLING_ON_PLAN", "declined", "plans-pricing-contracts", "free-plan"),
    ("GRACE_NOT_PAST_DUE", "declined", "billing-invoices-payments", "grace-extension"),
    ("GRACE_ALREADY_USED", "declined", "billing-invoices-payments", "grace-extension"),
    ("GRACE_WITHIN_LIMITS", "allowed", "billing-invoices-payments", "grace-extension"),
    ("GRACE_ENTERPRISE_APPROVAL", "needs_approval", "billing-invoices-payments", "grace-extension"),
]


def test_a2_blocks_are_designed_only():
    """What: the A2 billing email and notification preference blocks are absent (doc 09 s1.2).
    Use when: CI. Inputs: YAML and model. Output: assertion. Side effects: none.
    Role: spec gap S1: designed-only items must not ship; extra=forbid rejects them if added back."""
    assert set(type(load_policy(POLICY).actions).model_fields) == {"refund", "grace_extension"}
    assert set(_raw()["actions"]) == {"refund", "grace_extension"}


def test_a2_block_in_yaml_is_rejected(tmp_path):
    """What: a billing_email block fails the load until A2 is built. Use when: CI.
    Inputs: mutated YAML. Output: assertion. Side effects: temp file. Role: spec gap S1 guard."""
    d = _raw()
    d["actions"]["billing_email"] = {"enabled": True}
    with pytest.raises(PolicyError, match="billing_email"):
        load_policy(_write(tmp_path, d))


def test_decision_table_is_frozen():
    """What: all 23 reason codes keep decision, article and section (doc 07 s13.1 table).
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: pins review I2."""
    c = load_policy(POLICY)
    got = [(k, v.decision, v.policy_ref.article, v.policy_ref.section) for k, v in c.reason_codes.items()]
    assert sorted(got) == sorted(DECISION_TABLE)
    assert len(DECISION_TABLE) == 23


def test_pinned_values():
    """What: roles, lock rule, TTLs, seats and read-back texts are pinned. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: kills review mutations Y10 to Y13, Y18."""
    c = load_policy(POLICY)
    assert tuple(c.roles.billing_roles) == ("owner", "billing_admin")
    assert tuple(c.roles.all_roles) == ("owner", "billing_admin", "member")
    a = c.identity.account_lock
    assert (a.failures, a.window_minutes, a.lock_minutes, c.identity.session_ttl_minutes) == (5, 15, 15, 120)
    assert c.tiers.pro.seats == (3, 30) and c.tiers.business.seats == (30, 300)
    assert c.actions.grace_extension.pending_ttl_seconds == 600
    r = c.actions.refund
    for rb, ph in ((r.readback, ("{amount}", "{charge_label")), (r.readback_approval, ("{amount}",)),
                   (c.actions.grace_extension.readback, ("{days}", "{grace_until"))):
        for text in (rb.chat, rb.voice):
            assert text and all(p in text for p in ph), text
    assert "{charge_date}" in r.readback.chat and "{charge_date_spoken}" in r.readback.voice


def test_loaded_config_is_deeply_frozen():
    """What: lists and maps cannot be changed after load. Use when: CI. Inputs: loaded config.
    Output: assertion. Side effects: none. Role: review I3; core holds one instance per process."""
    c = load_policy(POLICY)
    with pytest.raises(AttributeError):
        c.roles.billing_roles.append("member")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        c.reason_codes["PAST_DUE"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        c.tickets.priority_by_tier["pro"] = "P1"  # type: ignore[index]
    with pytest.raises(AttributeError):
        c.tickets.queues.append("x")  # type: ignore[attr-defined]


def test_grace_readback_ending_enforced(tmp_path):
    """What: the approval-question rule applies to grace_extension, to both channels, as a suffix.
    Use when: CI. Inputs: mutated YAML. Output: assertion. Side effects: temp files.
    Role: kills review mutations L4 and L12."""
    for ch in ("chat", "voice"):
        d = _raw()
        d["actions"]["grace_extension"]["readback_approval"][ch] = "Send it? Shall I."
        with pytest.raises(PolicyError, match="readback_approval grace_extension"):
            load_policy(_write(tmp_path, d))
    d = _raw()
    d["actions"]["grace_extension"]["readback_approval"]["chat"] += " Thanks."
    with pytest.raises(PolicyError, match="readback_approval"):
        load_policy(_write(tmp_path, d))


def test_declined_code_without_message_and_unknown_queue_rejected(tmp_path):
    """What: a declined code needs a message; an unknown queue fails. Use when: CI.
    Inputs: mutated YAML. Output: assertion. Side effects: temp files. Role: kills L9 and L13."""
    d = _raw()
    del d["reason_codes"]["PAST_DUE"]["message"]
    with pytest.raises(PolicyError, match="needs a message"):
        load_policy(_write(tmp_path, d))
    d = _raw()
    d["tickets"]["category_queue"]["billing"] = "biling"
    with pytest.raises(PolicyError, match="biling"):
        load_policy(_write(tmp_path, d))


@pytest.mark.parametrize("edit,needle", [
    (lambda d: d["actions"]["refund"]["tiers"]["pro"].update(window_days=-14), "window_days"),
    (lambda d: d["roles"]["billing_roles"].append("membre"), "membre"),
    (lambda d: d["tickets"]["priority_by_tier"].update(pro="P9"), "P9"),
    (lambda d: d["tickets"]["queues"].append("biling"), "biling"),
    (lambda d: d.update(timezone="America/New_Yrok"), "timezone"),
    (lambda d: d["actions"]["refund"]["blocked_statuses"].append("pastdue"), "pastdue"),
    (lambda d: d["tickets"]["category_queue"].pop("bug"), "bug"),
], ids=["negative-days", "bad-role", "bad-priority", "bad-queue", "bad-timezone", "bad-status", "missing-category"])
def test_loose_types_rejected(tmp_path, edit, needle):
    """What: bad enum-like and numeric values fail the load. Use when: CI. Inputs: mutated YAML.
    Output: assertion. Side effects: temp files. Role: review M2 typed fields."""
    d = _raw()
    edit(d)
    with pytest.raises(PolicyError, match=needle):
        load_policy(_write(tmp_path, d))


def test_duplicate_yaml_key_rejected(tmp_path):
    """What: a repeated key fails instead of the last one winning. Use when: CI.
    Inputs: raw YAML text. Output: assertion. Side effects: temp file. Role: review M2."""
    text = POLICY.read_text().replace("pin_digits: 4", "pin_digits: 4\n  pin_digits: 6", 1)
    p = tmp_path / "policy.yaml"
    p.write_text(text)
    with pytest.raises(PolicyError, match="pin_digits"):
        load_policy(p)


def test_dollars_and_days_reject_non_integers():
    """What: |dollars and |days refuse booleans and strings. Use when: CI. Inputs: config.
    Output: assertion. Side effects: none. Role: review M7."""
    c = load_policy(POLICY)
    with pytest.raises(PolicyError, match="integer"):
        render_placeholder(c, "actions.refund.enabled|dollars")
    with pytest.raises(PolicyError, match="integer"):
        render_placeholder(c, "timezone|days")


def test_frozen_config_dumps_and_deep_copies():
    """What: the deep-frozen config still serializes and deep-copies, and the copy stays frozen.
    Use when: CI. Inputs: loaded config. Output: assertion. Side effects: none.
    Role: re-review N1 (MappingProxyType broke model_dump_json and copy.deepcopy)."""
    import json
    c = load_policy(POLICY)
    dumped = json.loads(c.model_dump_json())
    assert dumped["reason_codes"]["PAST_DUE"]["decision"] == "declined"
    assert dumped["tickets"]["priority_by_tier"]["pro"] == "P3"
    assert PolicyConfig.model_validate_json(c.model_dump_json()).reason_codes.keys() == c.reason_codes.keys()
    d = copy.deepcopy(c)
    assert d == c and d is not c
    with pytest.raises(TypeError):
        d.reason_codes["PAST_DUE"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        c.tickets.priority_by_tier.update(pro="P1")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        del c.tickets.target_label["P1"]  # type: ignore[attr-defined]


def test_role_decline_states_the_next_step_it_allows():
    """Browser pass 2b: the member decline gave no next step, so the agent's "an owner or billing admin can ask for
    it" had no source and Gate 2 failed the S3 reply. The tool result must carry the step it allows (the
    roles-and-permissions article's billing permissions section says the same)."""
    msg = yaml.safe_load(POLICY.read_text())["reason_codes"]["ROLE_NOT_PERMITTED"]["message"]
    assert msg.startswith("Only an account owner or billing admin can make billing changes.")
    assert "an owner or billing admin on your account can ask for it" in msg.lower()


_STOP = frozenset("that this with have been from your their there which while because cannot nothing already these "
                  "those after than then only into they them what when where would could should about over also "
                  "just".split())
MESSAGE_SECTION_OVERLAP_MIN = 0.5  # measured 2026-11-27: lowest 0.50 (GRACE_ALREADY_USED), most 0.8 to 1.0


def _section_text(article: str, slug: str) -> str:
    """What: the body of one article section, its subsections included, up to the next heading of the same or a
    higher level. Use when: the message-to-section tie. Inputs: article slug, section slug. Output: text.
    Side effects: reads one file. Role: helper."""
    import re
    lines, out, level = (REPO / "data" / "articles" / f"{article}.md").read_text().splitlines(), [], None
    for line in lines:
        m = re.match(r"(#+)\s+(.*)", line)
        if m:
            heading = re.sub(r"[^a-z0-9]+", "-", m.group(2).lower()).strip("-")
            if level is not None and len(m.group(1)) <= level:
                break
            if level is None and heading == slug:
                level = len(m.group(1))
                continue
        if level is not None:
            out.append(line)
    return "\n".join(out)


def _content_words(text: str) -> set[str]:
    """What: lowercase words of 4 or more letters, placeholders and _STOP removed. Use when: the overlap test.
    Inputs: text. Output: set. Side effects: none. Role: helper."""
    import re
    return {w for w in re.findall(r"[a-z]+", re.sub(r"\{[a-z_]+\}", " ", text.lower())) if len(w) >= 4
            and w not in _STOP}


def test_every_decline_message_is_stated_by_the_article_section_it_cites():
    """Review PR #71 M3: a decline message is what the customer is told, and its policy_ref is the article section
    Gate 2 and the customer can check it against. Most of the message's content words must appear in that section
    (subsections included), so a message rewritten away from its article, or a policy_ref pointed at the wrong
    section, fails here. Meaning is checked live by Gate 2; this is the drift floor."""
    _articles_dir()
    c = load_policy(POLICY)
    low = {}
    for code, rc in c.reason_codes.items():
        if not rc.message:
            continue
        section = _section_text(rc.policy_ref.article, rc.policy_ref.section)
        assert section.strip(), (code, rc.policy_ref)
        words = _content_words(rc.message)
        share = len(words & _content_words(section)) / len(words)
        if share < MESSAGE_SECTION_OVERLAP_MIN:
            low[code] = (round(share, 2), sorted(words - _content_words(section)))
    assert not low, low


def test_role_decline_next_step_is_stated_by_its_section():
    """Browser pass 2b: the next step the decline gives ("an owner or billing admin ... can ask for it") must be
    what the cited section says (the request needs an owner or billing admin)."""
    c = load_policy(POLICY)
    rc = c.reason_codes["ROLE_NOT_PERMITTED"]
    section = _section_text(rc.policy_ref.article, rc.policy_ref.section).lower()
    assert "needs an owner or billing admin" in section and "owner or billing admin" in rc.message.lower()


def test_the_s3_fixture_decline_message_is_the_policy_message():
    """Review PR #71 M3: the contract fixtures quote the decline core sends; a stale copy would test the old text."""
    from contracts.fixtures_build import ROLE_DECLINE_MESSAGE

    assert ROLE_DECLINE_MESSAGE == load_policy(POLICY).reason_codes["ROLE_NOT_PERMITTED"].message
