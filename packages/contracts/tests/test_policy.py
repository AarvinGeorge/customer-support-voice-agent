"""What: tests for data/policy.yaml and the frozen PolicyConfig loader (doc 07 s13.1, CS-E-09.S2.T1).
Use when: CI (contracts suite). Inputs: the committed data/policy.yaml and temp copies of it.
Output: pass or fail. Side effects: writes temp files only.
Role: proves each Done-when item and that policy numbers equal L1 s5 and s6."""
import copy
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
    for name in ("refund", "billing_email", "grace_extension"):
        act = getattr(cfg.actions, name)
        for text in (act.readback_approval.chat, act.readback_approval.voice):
            assert text.endswith(APPROVAL_TAIL), (name, text)
            seen += 1
    assert seen == 6
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
    """What: every number equals L1 s5 (A1, A3, A4), s6, s10 and s5 A2. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: L1 is the value authority."""
    c = load_policy(POLICY)
    r = c.actions.refund
    assert (r.repeat_window_days, r.pending_ttl_seconds) == (90, 600)
    assert (r.tiers.pro.window_days, r.tiers.pro.late_band_days, r.tiers.pro.auto_cap_cents) == (14, 7, 15000)
    assert r.tiers.business.window_days == 30 and r.tiers.business.approval_only
    assert r.tiers.enterprise.approval_only and not r.tiers.free.eligible
    assert r.blocked_statuses == ["trialing", "past_due"]
    e = c.actions.billing_email
    assert (e.per_account_per_day, e.step_up_valid_seconds) == (1, 300)
    assert e.step_up_required_tiers == ["pro", "business"] and e.approval_tiers == ["enterprise"]
    g = c.actions.grace_extension
    assert g.enabled is False and g.requires_status == "past_due"
    assert (g.tiers.pro.days, g.tiers.pro.once_per_months) == (7, 12)
    assert (g.tiers.business.days, g.tiers.business.once_per_months) == (14, 6)
    assert g.tiers.enterprise.approval_only and not g.tiers.free.eligible
    assert (c.tiers.pro.price_cents_per_seat_month, c.tiers.business.price_cents_per_seat_month) == (800, 1600)
    assert c.identity.pin_digits == 4 and c.identity.max_failures_per_session == 3
    t = c.tickets
    assert (t.limits.verified_per_account_per_day, t.limits.unverified_per_session) == (5, 2)
    assert t.priority_by_tier == {"enterprise": "P2", "business": "P2", "pro": "P3", "free": "P4",
                                  "unverified": "P4"}
    assert t.enterprise_blocking_priority == "P1"
    assert t.target_label == {"P1": "1 hour", "P2_enterprise": "4 hours", "P2": "8 hours",
                              "P3": "24 hours", "P4": "72 hours"}
    assert t.queues == ["billing", "technical", "account", "approvals"]
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
        assert "—" not in (rc.message or "")
    assert c.reason_codes["PAST_DUE"].followup_queue == "billing"
    assert c.reason_codes["EMAIL_STEP_UP"].requires_step_up is True


def test_article_refs_match_staged_articles():
    """What: every policy_ref article and section slug exists in the staged articles, when the
    staging folder is present. Use when: CI in the full repo. Inputs: build-log/articles-staging.
    Output: assertion or skip. Side effects: none. Role: keeps refs citable."""
    staging = REPO.parent / "build-log" / "articles-staging"
    if not staging.exists():
        pytest.skip("articles staging not present in this checkout")
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
    Inputs: build-log/articles-staging. Output: assertion or skip. Side effects: none.
    Role: L1 s6 consistency check for the staged corpus (full checker lands with CS-E-02.S1.T3)."""
    staging = REPO.parent / "build-log" / "articles-staging"
    if not staging.exists():
        pytest.skip("articles staging not present in this checkout")
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
