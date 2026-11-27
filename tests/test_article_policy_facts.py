"""What: checks that every policy number in the Help Center articles equals data/policy.yaml (L1 s6, D-041).
Use when: CI (uv run --project packages/contracts --no-editable pytest tests). Inputs: data/articles, data/policy.yaml.
Output: pass or fail naming article, section and token. Side effects: none.
Role: the articles hold real numbers (public Notion pages, no placeholders, doc 07 s16), so this test is what stops
them drifting from the policy that core enforces. Two directions: every FACT phrase appears in its section (forward),
and every money, day, hour, minute, month, ticket or digit token in any article is a FACT, derived from policy, or on
an allowlist with a reason (reverse)."""
import re
from pathlib import Path

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
    ("billing-invoices-payments", "billing-contact", "actions.billing_email.per_account_per_day", "at most once per day"),
    ("plans-pricing-contracts", "pricing", "tiers.pro.price_cents_per_seat_month|dollars", "{v} per seat per month"),
    ("plans-pricing-contracts", "pricing", "tiers.business.price_cents_per_seat_month|dollars", "{v} per seat per month"),
    ("support-and-escalation", "tickets", "tickets.limits.verified_per_account_per_day", "up to {v} tickets per day"),
    ("support-and-escalation", "tickets", "tickets.limits.unverified_per_session", "up to {v} tickets"),
    ("support-and-escalation", "priorities-and-response-targets", "tickets.target_label.P1", "{v}"),
    ("support-and-escalation", "priorities-and-response-targets", "tickets.target_label.P2_enterprise", "{v}"),
    ("support-and-escalation", "priorities-and-response-targets", "tickets.target_label.P2", "{v}"),
    ("support-and-escalation", "priorities-and-response-targets", "tickets.target_label.P3", "{v}"),
    ("support-and-escalation", "priorities-and-response-targets", "tickets.target_label.P4", "{v}"),
    ("support-and-escalation", "approvals", "actions.refund.tiers.pro.auto_cap_cents|dollars", "over {v}"),
    ("sso-login-troubleshooting", "support-pin-and-verification-problems", "identity.max_failures_per_session", "fails {v} times"),
    ("sso-login-troubleshooting", "support-pin-and-verification-problems", "identity.account_lock.failures", "{v} failed attempts"),
    ("sso-login-troubleshooting", "support-pin-and-verification-problems", "identity.account_lock.window_minutes", "within {v} minutes"),
    ("sso-login-troubleshooting", "support-pin-and-verification-problems", "identity.account_lock.lock_minutes", "paused for {v} minutes"),
    ("roles-and-permissions", "how-support-knows-your-role", "identity.pin_digits", "{v}-digit"),
    ("account-notification-settings", "your-support-pin", "identity.pin_digits", "{v}-digit"),
]

TOKEN = re.compile(r"\$\d[\d,]*|\d+(?:-| )(?:days?|hours?|minutes?|months?|digit|tickets?|seats?)\b")
# Tokens that are not policy values: tier seat ranges (checked against policy seats) are derived below.
ALLOW = {"1 hour", "72 hours"}  # also facts via target_label; kept so a label wording change is caught by FACTS, not here


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


def value(cfg: PolicyConfig, key: str) -> str:
    """What: render a policy key; target_label keys (dict lookups with a P1 style name) resolve too.
    Use when: building FACTS. Inputs: config, key[|fmt]. Output: text. Side effects: none. Role: key resolver."""
    return render_placeholder(cfg, key)


def derived(cfg: PolicyConfig) -> set[str]:
    """What: numbers computed from policy (seat ranges and monthly or annual totals).
    Use when: the reverse check meets a plans-article range. Inputs: config. Output: set of token texts.
    Side effects: none. Role: lets '$24 to $240' and '$5,760 to $57,600' be checked, not allowlisted."""
    out = set()
    for tier, per_year in (("pro", 1), ("business", 12)):
        t = getattr(cfg.tiers, tier)
        for seats in t.seats:
            total = t.price_cents_per_seat_month * seats * per_year // 100
            out.add(f"${total:,}")
            out.add(f"{seats}")
    return out


def check(articles: dict, cfg: PolicyConfig = POLICY, facts=FACTS) -> list[str]:
    """What: run both directions and return a list of problems (empty means pass).
    Use when: the tests below. Inputs: {id: (front matter, body)}, config, fact table.
    Output: list of strings naming article, section and token. Side effects: none. Role: the checker itself."""
    problems: list[str] = []
    known: set[str] = set(derived(cfg)) | ALLOW
    for article, section, key, frag in facts:
        v = value(cfg, key)
        known.add(v)
        text = sections(articles[article][1]).get(section)
        if text is None:
            problems.append(f"{article}#{section}: section missing")
        elif frag.format(v=v) not in text:
            problems.append(f"{article}#{section}: expected {frag.format(v=v)!r} for {key}")
    for article, (_, body) in articles.items():
        for tok in TOKEN.findall(body):
            if tok.strip() in known or any(tok.strip().startswith(k.split()[0]) and k in tok for k in known):
                continue
            n = re.match(r"\d+", tok)
            if n and n.group() in known:
                continue
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


def test_checker_catches_policy_change():
    """What: changing policy.yaml without the article fails. Use when: CI. Inputs: a config copy with
    the cap at 15100 cents. Output: assertion. Side effects: none. Role: the drift case D-041 exists for."""
    cfg = POLICY.model_copy(deep=True)
    pro = cfg.actions.refund.tiers.pro.model_copy(update={"auto_cap_cents": 15100})
    tiers = cfg.actions.refund.tiers.model_copy(update={"pro": pro})
    refund = cfg.actions.refund.model_copy(update={"tiers": tiers})
    cfg = cfg.model_copy(update={"actions": cfg.actions.model_copy(update={"refund": refund})})
    assert any("$151" in p for p in check(load_all(), cfg))
