"""What: checks that every policy number in the Help Center articles equals data/policy.yaml (L1 s6, D-041).
Use when: CI (uv run --project packages/contracts --no-editable pytest tests). Inputs: data/articles, data/policy.yaml.
Output: pass or fail naming article, section and token. Side effects: none.
Role: the articles hold real numbers (public Notion pages, no placeholders, doc 07 s16), so this test is what stops
them drifting from the policy that core enforces. Two directions: every FACT phrase appears in its section (forward),
and every money, day, hour, minute, month, ticket or digit token in any article is a FACT, derived from policy, or on
an allowlist with a reason (reverse)."""
import re
from pathlib import Path

import pytest
import yaml

from contracts.policy import PolicyConfig, load_policy, render_placeholder
from tests.test_articles_snapshot import load_all

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_policy(ROOT / "data" / "policy.yaml")

# (article, section slug, policy key with formatter, sentence fragment containing the rendered value)
FACTS = [
    ("refund-policy", "pro-plan-refunds", "actions.refund.tiers.pro.window_days|days", "within {v} of the charge"),
    ("refund-policy", "pro-plan-refunds", "actions.refund.tiers.pro.auto_cap_cents|dollars", "up to {v}"),
    ("refund-policy", "pro-plan-refunds", "actions.refund.tiers.pro.late_band_days|days", "up to {v} after the refund window"),
    ("refund-policy", "pro-plan-refunds", "actions.refund.repeat_window_days|days", "within {v} of a previous refund"),
    ("refund-policy", "business-plan-refunds", "actions.refund.tiers.business.window_days|days", "within {v} of the charge"),
    ("billing-invoices-payments", "grace-extension", "actions.grace_extension.tiers.pro.days|days", "extension of {v}"),
    ("billing-invoices-payments", "grace-extension", "actions.grace_extension.tiers.business.days|days", "extension of {v}"),
    ("billing-invoices-payments", "grace-extension", "actions.grace_extension.tiers.pro.once_per_months", "once per {v} months"),
    ("billing-invoices-payments", "grace-extension", "actions.grace_extension.tiers.business.once_per_months", "once per {v} months"),
    ("plans-pricing-contracts", "pricing", "tiers.pro.price_cents_per_seat_month|dollars", "{v} per seat per month"),
    ("plans-pricing-contracts", "pricing", "tiers.business.price_cents_per_seat_month|dollars", "{v} per seat per month"),
    ("support-and-escalation", "tickets", "tickets.limits.verified_per_account_per_day", "up to {v} tickets per day"),
    ("support-and-escalation", "how-to-reach-support", "identity.account_lock.failures", "after {v} failed attempts"),
    ("support-and-escalation", "how-to-reach-support", "identity.account_lock.lock_minutes", "paused for {v} minutes"),
    ("support-and-escalation", "priorities-and-response-targets", "tickets.target_label.P1", "| P1 | {v} |"),
    ("support-and-escalation", "priorities-and-response-targets", "tickets.target_label.P2_enterprise", "| Enterprise | P2 | {v} |"),
    ("support-and-escalation", "priorities-and-response-targets", "tickets.target_label.P2", "| Business | P2 | {v} |"),
    ("support-and-escalation", "priorities-and-response-targets", "tickets.target_label.P3", "| Pro | P3 | {v} |"),
    ("support-and-escalation", "priorities-and-response-targets", "tickets.target_label.P4", "| P4 | {v} |"),
    ("support-and-escalation", "approvals", "actions.refund.tiers.pro.auto_cap_cents|dollars", "over {v}"),
    ("sso-login-troubleshooting", "support-pin-and-verification-problems", "identity.max_failures_per_session", "fails {v} times"),
    ("sso-login-troubleshooting", "support-pin-and-verification-problems", "identity.account_lock.failures", "{v} failed attempts"),
    ("sso-login-troubleshooting", "support-pin-and-verification-problems", "identity.account_lock.window_minutes", "within {v} minutes"),
    ("sso-login-troubleshooting", "support-pin-and-verification-problems", "identity.account_lock.lock_minutes", "paused for {v} minutes"),
    ("roles-and-permissions", "how-support-knows-your-role", "identity.pin_digits", "{v}-digit"),
    ("account-notification-settings", "your-support-pin", "identity.pin_digits", "{v}-digit"),
    ("refund-policy", "refunds-at-a-glance", "actions.refund.tiers.pro.auto_cap_cents|dollars", "up to {v}"),
    ("refund-policy", "refunds-at-a-glance", "actions.refund.tiers.pro.window_days|days", "within {v} of the charge"),
    ("refund-policy", "refunds-at-a-glance", "actions.refund.tiers.business.window_days|days", "within {v} of the charge"),
    ("refund-policy", "refunds-we-can-issue-right-away", "actions.refund.repeat_window_days|days", "in {v}"),
    ("refund-policy", "refunds-we-cannot-make", "actions.refund.tiers.pro.late_band_days|days", "more than {v} after"),
    ("plans-pricing-contracts", "plans-at-a-glance", "calc.pro.seats_range", "{v}"),
    ("plans-pricing-contracts", "plans-at-a-glance", "calc.business.seats_range", "{v}"),
    ("plans-pricing-contracts", "plans-at-a-glance", "calc.enterprise_seats", "{v}"),
    ("plans-pricing-contracts", "typical-totals", "calc.pro.seats_range", "Pro with {v}"),
    ("plans-pricing-contracts", "typical-totals", "calc.pro.min_total", "{v} to"),
    ("plans-pricing-contracts", "typical-totals", "calc.pro.max_total", "to {v} a month"),
    ("plans-pricing-contracts", "typical-totals", "calc.business.seats_range", "Business with {v}"),
    ("plans-pricing-contracts", "typical-totals", "calc.business.min_total", "{v} to"),
    ("plans-pricing-contracts", "typical-totals", "calc.business.max_total", "to {v} a year"),
    ("support-and-escalation", "approvals", "actions.refund.tiers.pro.late_band_days|days", "up to {v} after"),
    ("support-and-escalation", "approvals", "actions.refund.repeat_window_days|days", "within {v}"),
    ("support-and-escalation", "how-to-reach-support", "identity.pin_digits", "{v}-digit"),
    ("account-notification-settings", "your-support-pin", "identity.max_failures_per_session", "fails {v} times"),
]

# One policy-number token: money, a seat range, "N or more seats", or a number with a unit. Matched whole.
TOKEN = re.compile(
    r"\$\d+(?:,\d{3})*"
    r"|\d+ to \d+ seats|\d+ or more seats"
    r"|\d+(?:-| )(?:days?|hours?|minutes?|months?|digit|tickets?|seats?|times?|failed attempts?)\b"
)
# Tokens that are not policy values. Empty on purpose: every number in an article must come from policy.
ALLOW: set[str] = set()


def sections(body: str) -> dict[str, str]:
    """What: split an article body into {heading slug: text} for H2 and H3.
    Use when: locating a fact's section. Inputs: article body. Output: dict (an H2 includes its H3 text). Side effects: none.
    Role: same slug rule as the policy_ref sections (lowercase, non-alphanumerics to hyphen)."""
    out: dict[str, str] = {}
    h2 = None
    cur = "_intro"
    for line in body.splitlines():
        m = re.match(r"(#{2,3}) (.+)", line)
        if m:
            cur = re.sub(r"[^a-z0-9]+", "-", m.group(2).lower()).strip("-")
            out.setdefault(cur, "")
            if len(m.group(1)) == 2:
                h2 = cur
        else:
            out[cur] = out.get(cur, "") + line + "\n"
            if h2 and cur != h2:  # an H3 body also belongs to its H2 section
                out[h2] += line + "\n"
    return out


def _total(cfg: PolicyConfig, tier: str, seats: int) -> str:
    """What: a seat total in dollars (monthly for pro, annual for business). Use when: building calc values.
    Inputs: config, tier, seat count. Output: "$240" style text. Side effects: none. Role: seat-range arithmetic."""
    t = getattr(cfg.tiers, tier)
    return f"${t.price_cents_per_seat_month * seats * (12 if tier == 'business' else 1) // 100:,}"


def value(cfg: PolicyConfig, key: str) -> str:
    """What: render a policy key; "calc.<tier>.<what>" keys give seat ranges and totals computed from policy.
    Use when: building FACTS. Inputs: config, key[|fmt]. Output: text. Side effects: none. Role: key resolver."""
    if key == "calc.enterprise_seats":
        return f"{cfg.tiers.business.seats[1]} or more seats"
    if key.startswith("calc."):
        _, tier, what = key.split(".")
        lo, hi = getattr(cfg.tiers, tier).seats
        return {
            "seats_range": f"{lo} to {hi} seats",
            "min_total": _total(cfg, tier, lo),
            "max_total": _total(cfg, tier, hi),
        }[what]
    return render_placeholder(cfg, key)


def allowed_tokens(cfg: PolicyConfig, facts=FACTS) -> set[str]:
    """What: every full token an article may contain, rendered from policy (no prefixes, no lookalikes).
    Use when: the reverse scan. Inputs: config, fact table. Output: set of exact token texts. Side effects: none.
    Role: the whole-token allow list; a wrong figure like "60 days" is not in it unless policy says so."""
    out = set(ALLOW)
    for _, _, key, frag in facts:
        v = value(cfg, key)
        out.add(v)
        out.update(TOKEN.findall(frag.format(v=v)))
    for tier in ("pro", "business"):
        out.update(value(cfg, f"calc.{tier}.{w}") for w in ("min_total", "max_total", "seats_range"))
    out.add(value(cfg, "calc.enterprise_seats"))
    out.update(cfg.tickets.target_label.values())
    return out


def check(articles: dict, cfg: PolicyConfig = POLICY, facts=FACTS) -> list[str]:
    """What: run both directions and return a list of problems (empty means pass).
    Use when: the tests below. Inputs: {id: (front matter, body)}, config, fact table.
    Output: list of strings naming article, section and token. Side effects: none. Role: the checker itself.
    Forward: each fact phrase appears in its section as a whole phrase (no digit or word glued before or after).
    Reverse: every number token in any article equals a rendered policy token exactly."""
    problems: list[str] = []
    allowed = allowed_tokens(cfg, facts)
    for article, section, key, frag in facts:
        phrase = frag.format(v=value(cfg, key))
        text = sections(articles[article][1]).get(section)
        if text is None:
            problems.append(f"{article}#{section}: section missing")
        elif not re.search(r"(?<![\w$,.])" + re.escape(phrase) + r"(?!\w)", text):
            problems.append(f"{article}#{section}: expected {phrase!r} for {key}")
    for article, (_, body) in articles.items():
        for tok in TOKEN.findall(body):
            if tok not in allowed:
                problems.append(f"{article}: unmapped number {tok!r}")
    return problems


def test_articles_match_policy():
    """What: no problems in the committed snapshot. Use when: CI. Inputs: data/articles, policy.yaml.
    Output: assertion listing problems. Side effects: none. Role: D-041 consistency gate."""
    assert check(load_all()) == []


def test_checker_catches_wrong_number():
    """What: a wrong figure in an article is reported (forward direction). Use when: CI.
    Inputs: a mutated copy of refund-policy. Output: assertion. Side effects: none.
    Role: zero-proof (a green result must be able to be red)."""
    arts = dict(load_all())
    fm, body = arts["refund-policy"]
    arts["refund-policy"] = (fm, body.replace("up to $150", "up to $175"))
    problems = check(arts)
    assert any("refund-policy#pro-plan-refunds" in p and "$150" in p for p in problems), problems


def test_checker_catches_unmapped_number():
    """What: a new number with no policy source is reported (reverse direction). Use when: CI.
    Inputs: article with an added sentence. Output: assertion. Side effects: none. Role: zero-proof."""
    arts = dict(load_all())
    fm, body = arts["support-and-escalation"]
    arts["support-and-escalation"] = (fm, body + "\nWe answer within 99 hours.\n")
    assert any("99 hours" in p for p in check(arts))


def test_checker_catches_policy_change(tmp_path):
    """What: changing policy.yaml without the article fails. Use when: CI. Inputs: a config copy with
    the cap at 15100 cents. Output: assertion. Side effects: temp file. Role: the drift case D-041 exists for."""
    cfg = _policy_with(tmp_path, lambda r: r["actions"]["refund"]["tiers"]["pro"].update(auto_cap_cents=15100))
    assert any("$151" in p for p in check(load_all(), cfg))


def _policy_with(tmp_path, mutate):
    """What: load policy.yaml with one edit applied. Use when: a mutation test needs a changed policy.
    Inputs: tmp dir, function that edits the parsed dict. Output: PolicyConfig. Side effects: writes a temp file.
    Role: drift simulator for the policy side."""
    raw = yaml.safe_load((ROOT / "data" / "policy.yaml").read_text())
    mutate(raw)
    f = tmp_path / "policy.yaml"
    f.write_text(yaml.safe_dump(raw))
    return load_policy(f)


ARTICLE_MUTATIONS = [
    ("refund-policy", "up to $150 within 14 days", "up to $175 within 14 days"),  # A
    ("refund-policy", "within 14 days of the charge can", "within 15 days of the charge can"),  # C (at a glance)
    ("refund-policy", "every refund within 30 days", "every refund within 60 days"),  # D
    ("support-and-escalation", "| Enterprise | P2 | 4 hours |", "| Enterprise | P2 | 5 hours |"),  # F
    ("support-and-escalation", "| Business | P2 | 8 hours |", "| Business | P2 | 4 hours |"),  # swap to another real label
    ("plans-pricing-contracts", "3 to 30 seats: $24 to $240", "3 to 50 seats: $24 to $2400"),  # I
    ("sso-login-troubleshooting", "fails 3 times", "fails 9 times"),  # J
    ("account-notification-settings", "fails 3 times", "fails 2 times"),  # repeat of J elsewhere
    ("support-and-escalation", "approvals", "approvals"),  # placeholder replaced below
]
ARTICLE_MUTATIONS[-1] = ("support-and-escalation", "a second refund within 90 days", "a second refund within 60 days")


@pytest.mark.parametrize("article,old,new", ARTICLE_MUTATIONS)
def test_article_mutation_is_caught(article, old, new):
    """What: each wrong figure in an article text makes check() report a problem. Use when: CI.
    Inputs: article id, text to replace, wrong text. Output: assertion that the old text existed and a problem is raised.
    Side effects: none. Role: zero-proof for the reviewer's mutation list (probes A, C, D, F, I, J and repeats)."""
    arts = dict(load_all())
    fm, body = arts[article]
    assert old in body, f"mutation anchor missing in {article}: {old!r}"
    arts[article] = (fm, body.replace(old, new))
    assert check(arts) != []


POLICY_MUTATIONS = [
    ("target P1", lambda r: r["tickets"]["target_label"].update(P1="2 hours")),  # G
    ("pro seats max", lambda r: r["tiers"]["pro"].update(seats=[3, 50])),
    ("business seats max", lambda r: r["tiers"]["business"].update(seats=[30, 400])),
    ("auto cap", lambda r: r["actions"]["refund"]["tiers"]["pro"].update(auto_cap_cents=20000)),  # B
    ("repeat window", lambda r: r["actions"]["refund"].update(repeat_window_days=60)),
    ("pin digits", lambda r: r["identity"].update(pin_digits=6)),
    ("lock minutes", lambda r: r["identity"]["account_lock"].update(lock_minutes=30)),
]


@pytest.mark.parametrize("name,mutate", POLICY_MUTATIONS, ids=[m[0] for m in POLICY_MUTATIONS])
def test_policy_mutation_is_caught(tmp_path, name, mutate):
    """What: each changed policy number makes check() fail against the unchanged articles. Use when: CI.
    Inputs: a mutation of policy.yaml. Output: assertion. Side effects: temp file. Role: drift case for every policy
    number that an article states (probes B, G and the seat range)."""
    cfg = _policy_with(tmp_path, mutate)
    assert check(load_all(), cfg) != []


@pytest.mark.parametrize("bad", ["15 days", "60 days", "5 hours", "$2400", "50 seats", "1 tickets", "6 times"])
def test_reverse_scan_rejects_prefix_lookalikes(bad):
    """What: a token that merely starts like a policy value is still unmapped. Use when: CI. Inputs: a sentence
    with a wrong number whose first digit equals a real policy value. Output: assertion. Side effects: none.
    Role: zero-proof for the prefix-match bug (review finding 1)."""
    arts = dict(load_all())
    fm, body = arts["support-and-escalation"]
    arts["support-and-escalation"] = (fm, body + f"\nExtra: {bad}.\n")
    assert any(bad in p for p in check(arts)), check(arts)
