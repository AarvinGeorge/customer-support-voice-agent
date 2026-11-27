"""What: the D-059 real-model eval of Gate 2 claim extraction: the reviewers' adversarial lists run through the real
grounding_verifier (small model from data/models.yaml, production timeout: 5 s in chat, D-061), the code rules and the D-064 floor.
Use when: `uv run pytest -m real -s -o addopts="" tests/test_real_gate2_eval.py` (or `-k` one test) only; deselected
in CI by the default addopts.
Inputs: ANTHROPIC_API_KEY from the worktree .env (value never printed); tests/gate2_adversarial.py.
Output: prints one line per draft and the score; passes when every policy draft is flagged and at most one
clarifying reply is blocked (the full lists, or the fixed SUBSET of 10), and when the 5 verification-turn drafts
and the 8 account-context policy cases are judged as expected against the real grounded evidence (verified
context; review PR #66 I-1).
Side effects: one paid small-model call per draft (full 38, subset 10, verification 5, account policy 8), at most
4 in flight.
Role: D-059 evidence that meaning is judged by the model: a draft is "flagged" when the turn would not flush it
(verdict fail or error, or a commitment). No tool ran in these turns; the evidence is empty or the verified context.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
import yaml
from dotenv import load_dotenv
from gate2_adversarial import CLARIFYING_R2, CLARIFYING_R3, POLICY_IN_QUESTION_R2, POLICY_IN_QUESTION_R3

from contracts.stubs import examples as ex
from langchain_core.messages import HumanMessage, ToolMessage

from agent.gates import (GroundingResult, GroundingVerifier, apply_floor, enforce_code_rules, session_evidence,
                         needs_grounding)
from agent.models import ModelRegistry, load_models_config
from agent.turn_control import TurnControl

ROOT = Path(__file__).resolve().parents[3]
POLICY_ROLE_MESSAGE = yaml.safe_load((ROOT / "data" / "policy.yaml").read_text())["reason_codes"]["ROLE_NOT_PERMITTED"]["message"]
POLICY = POLICY_IN_QUESTION_R2 + POLICY_IN_QUESTION_R3
BENIGN = CLARIFYING_R3 + CLARIFYING_R2
MAX_BENIGN_BLOCKS = 1


# A fixed 10-draft subset for re-runs under a small call budget: the hardest list C phrasings, two list A, and the
# clarifying replies that failed in round 3.
SUBSET = [POLICY_IN_QUESTION_R3[0], POLICY_IN_QUESTION_R3[2], POLICY_IN_QUESTION_R3[4], POLICY_IN_QUESTION_R3[6],
          POLICY_IN_QUESTION_R2[0], POLICY_IN_QUESTION_R2[4],
          CLARIFYING_R3[0], CLARIFYING_R3[2], CLARIFYING_R3[4], CLARIFYING_R3[10]]
VERIFIED = {"verified": True, "claims_snapshot": ex.CLAIMS.model_dump(mode="json"),
            "context_snapshot": ex.ME_CONTEXT.model_dump(mode="json")}
# (draft, should pass) on the verification-card turn, judged against the real grounded evidence.
VERIFICATION_TURN = [
    ("Thanks, Priya. You're verified now. Your account is on the Pro plan with 12 seats.", True),
    ("Thanks, Priya Raman, you're verified. I can see Acme Analytics and your open ticket ending 8213.", True),
    ("Thanks, Priya. You're verified now. What can I help you with today?", True),
    ("Thanks, Priya. You're verified. Your Business plan has 20 seats.", False),
    ("You're verified, Priya. Since you're on Pro, you qualify for a full refund.", False),
]
# Review PR #66 I-1: account facts are true, but the policy conclusion needs a KB source; account_context alone must
# never support it. (draft, should pass); the last two are account-only controls.
ACCOUNT_POLICY = [
    ("Since you're on Pro, you qualify for a full refund.", False),
    ("Business plans get refunds within 60 days.", False),
    ("As an owner you can cancel without approval.", False),
    ("Because your contract is active, you're entitled to a service credit.", False),
    ("With 12 seats on the Pro plan, you get unlimited exports.", False),
    ("As the account owner of Acme Analytics, you can have the duplicate charge refunded today.", False),
    ("You're the owner of Acme Analytics, which is on the Pro plan.", True),
    ("Your account has 12 seats and an open ticket ending 8213.", True),
]


async def _judge(ver: GroundingVerifier, sem: asyncio.Semaphore, draft: str,
                 values: dict | None = None) -> tuple[GroundingResult, float]:
    """What: one real verifier call plus the code rules, as GroundingMiddleware applies them: evidence and
    citable sources from session_evidence (only the code sources for a turn with no tool).
    Use when: the eval. Inputs: verifier, semaphore, draft, state values. Output: (result after code rules, seconds).
    Side effects: one model call. Role: helper."""
    evidence = session_evidence(values or {}, [HumanMessage("hi")])
    async with sem:
        start = time.monotonic()
        raw = await ver.verify(TurnControl(session_id="eval", turn_id="eval"), draft, evidence)
        res = enforce_code_rules(raw, draft, evidence.chunks, tool_names=evidence.ids)
        return apply_floor(res, needs_grounding(draft, False)), time.monotonic() - start  # D-064, as the middleware


def _verifier() -> GroundingVerifier:
    """What: the real grounding_verifier from models.yaml. Use when: each eval. Inputs: none (reads .env).
    Output: verifier. Side effects: loads the key into the environment. Role: helper."""
    load_dotenv(ROOT / ".env", override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("ANTHROPIC_API_KEY missing: this eval must not pass without making the calls")
    return GroundingVerifier(ModelRegistry(load_models_config(ROOT / "data" / "models.yaml")).build("grounding_verifier"))


async def _score(drafts: list[str]) -> None:
    """What: run the drafts, print one line each and the score, assert the D-059 target. Use when: the D-059 evals.
    Inputs: drafts from POLICY and BENIGN. Output: none. Side effects: one model call per draft. Role: helper."""
    ver, sem = _verifier(), asyncio.Semaphore(4)
    results = await asyncio.gather(*(_judge(ver, sem, d) for d in drafts))
    missed, blocked = [], []
    for draft, (res, secs) in zip(drafts, results):
        kind = "POLICY" if draft in POLICY else "BENIGN"
        ok = (not res.passed) if kind == "POLICY" else res.passed
        if not ok:
            (missed if kind == "POLICY" else blocked).append(draft)
        print(f"{'ok ' if ok else 'BAD'} {kind} verdict={res.verdict} commitment={res.commitment} "
              f"claims={len(res.claims)} {secs:.2f}s :: {draft!r}")
    n_policy = sum(d in POLICY for d in drafts)
    n_benign = len(drafts) - n_policy
    print(f"SCORE policy flagged {n_policy - len(missed)}/{n_policy}; benign passed {n_benign - len(blocked)}/"
          f"{n_benign}; calls {len(drafts)}")
    assert not missed, f"policy drafts that would reach the customer: {missed}"
    assert len(blocked) <= MAX_BENIGN_BLOCKS, f"clarifying replies blocked: {blocked}"


@pytest.mark.real
async def test_real_verifier_flags_every_policy_question_and_passes_clarifying_replies():
    await _score(POLICY + BENIGN)


@pytest.mark.real
async def test_real_verifier_subset_after_a_prompt_change():
    await _score(SUBSET)


@pytest.mark.real
async def test_real_verifier_on_verification_turns_uses_the_verified_context():
    await _expect(VERIFICATION_TURN, "verification turns")


async def _expect(cases: list[tuple[str, bool]], label: str) -> None:
    """What: run (draft, should pass) cases on a verified turn (chat, D-061 5 s), print each verdict with the cited
    sources and the score, assert all as expected. Use when: the verified-context evals. Inputs: cases, label.
    Output: none. Side effects: one model call per case. Role: helper."""
    ver, sem = _verifier(), asyncio.Semaphore(4)
    results = await asyncio.gather(*(_judge(ver, sem, d, VERIFIED) for d, _ in cases))
    bad = []
    for (draft, expect), (res, secs) in zip(cases, results):
        ok = res.passed is expect
        bad += [] if ok else [draft]
        print(f"{'ok ' if ok else 'BAD'} expect={'pass' if expect else 'flag'} verdict={res.verdict} "
              f"commitment={res.commitment} claims={[(c['text'], c['supported'], c['source_id']) for c in res.claims]} "
              f"{secs:.2f}s :: {draft!r}")
    print(f"SCORE {label} {len(cases) - len(bad)}/{len(cases)}; calls {len(cases)}")
    assert not bad, bad


@pytest.mark.real
async def test_real_verifier_never_lets_account_context_alone_support_policy():
    await _expect(ACCOUNT_POLICY, "account_context policy")


# --- Gate model token cap (fix after browser pass 2): S1 policy follow-up and S3 Marcus member decline ----------


def _turn(tool_name: str, content: str, docs: list | None = None) -> list:
    """What: a turn's messages: the human question then one tool result, with the code-held artifact ToolGuard keeps
    (the relevant chunks for search_kb, D-065). Use when: the token cap cases.
    Inputs: tool name, wrapped content, the search docs. Output: messages. Side effects: none. Role: helper."""
    from langchain_core.messages import AIMessage, ToolMessage

    chunks = [{"chunk_id": d["id"], "article_slug": d["article"], "section_path": [d["section"]], "text": d["text"],
               "flags": []} for d in docs or []]
    artifact = {"bound_to": None, **({"grounded_chunks": chunks} if docs else {})}
    call = AIMessage("", tool_calls=[{"name": tool_name, "args": {}, "id": f"c-{tool_name}", "type": "tool_call"}])
    return [HumanMessage("q"), call, ToolMessage(content, tool_call_id=f"c-{tool_name}", name=tool_name,
                                                 artifact=artifact)]


def _marcus() -> dict:
    """What: verified state for the S3 member persona (data/seeds/personas.yaml Marcus Lee: member, business, active,
    QS-20318) built from the stub context with those fields. Use when: the S3 case. Output: state values.
    Side effects: none. Role: helper."""
    claims = {**ex.CLAIMS.model_dump(mode="json"), "display_name": "Marcus Lee", "account_id": "QS-20318",
              "account_name": "Northwind Logistics", "role": "member", "tier": "business",
              "permissions": {"billing_read": False, "billing_actions": False, "own_notification_prefs": True}}
    ctx = ex.ME_CONTEXT.model_dump(mode="json")
    ctx = {**ctx, "claims": claims, "open_tickets": [], "recent_actions": [],
           "profile": {**ctx["profile"], "display_name": "Marcus Lee", "account_id": "QS-20318",
                       "account_name": "Northwind Logistics", "role": "member", "tier": "business", "seats": 60}}
    return {"verified": True, "claims_snapshot": claims, "context_snapshot": ctx}


def token_cap_cases() -> list[tuple[str, str, dict, list, bool]]:
    """What: (label, draft, state values, turn messages, should pass) for the two live failures plus 6 eval cases.
    Use when: test_real_gate_models_return_a_verdict_within_the_cap. Output: list. Side effects: reads articles.
    Role: reconstructed from evals/golden/golden.yaml G-03 (S1 follow-up: refund-policy#did-my-refund-...) and G-04
    (S3 Marcus: ROLE_NOT_PERMITTED, roles-and-permissions#billing-permissions); the live drafts were not saved."""
    from agent.untrusted import wrap_result, wrap_search

    s1_list = [
        {"id": "refund-policy#did-my-refund-go-through-and-when-will-i-see-it", "article": "refund-policy",
         "section": "Did my refund go through, and when will I see it?",
         "text": _doc_text("refund-policy", "Did my refund go through, and when will I see it?")},
        {"id": "refund-policy#how-refunds-work", "article": "refund-policy", "section": "How refunds work",
         "text": _doc_text("refund-policy", "How refunds work")}]
    s1_docs = wrap_search("pass", None, s1_list)
    s1_draft = ("Your refund went back to your original payment method. How long it takes to show on your statement "
                "depends on your bank.")
    declined = {"decision": "declined", "reason_codes": ["ROLE_NOT_PERMITTED"],
                "policy_refs": [{"article": "roles-and-permissions", "section": "billing-permissions",
                                 "title": "Billing permissions"}],
                "message": POLICY_ROLE_MESSAGE,
                "pending_action_id": None, "state": "declined", "ticket": None, "changed": False}
    s3_draft = ("Thanks, Marcus. I can't request this refund for you: only an account owner or billing admin can "
                "make billing changes, including refunds. Your owner or billing admin can ask for it, or I can open "
                "a ticket so the right person follows up.")
    s3_list = [{"id": "roles-and-permissions#billing-permissions", "article": "roles-and-permissions",
                "section": "Billing permissions", "text": _doc_text("roles-and-permissions", "Billing permissions")}]
    s3_docs = wrap_search("pass", None, s3_list)
    s3_turn = _turn("search_kb", s3_docs, s3_list) + _turn("prepare_refund", wrap_result("prepare_refund", declined))[1:]
    return [
        ("S1 policy follow-up", s1_draft, _priya_after_refund(), _turn("search_kb", s1_docs, s1_list), True),
        ("S3 Marcus member decline", s3_draft, _marcus(), s3_turn, True),
        ("D-059 policy " + SUBSET[0][:30], SUBSET[0], {}, [HumanMessage("hi")], False),
        ("D-059 policy " + SUBSET[4][:30], SUBSET[4], {}, [HumanMessage("hi")], False),
        ("D-059 clarify " + SUBSET[6][:30], SUBSET[6], {}, [HumanMessage("hi")], True),
        ("verification " + VERIFICATION_TURN[0][0][:30], VERIFICATION_TURN[0][0], VERIFIED, [HumanMessage("hi")], True),
        ("verification " + VERIFICATION_TURN[3][0][:30], VERIFICATION_TURN[3][0], VERIFIED, [HumanMessage("hi")], False),
        ("account policy " + ACCOUNT_POLICY[0][0][:30], ACCOUNT_POLICY[0][0], VERIFIED, [HumanMessage("hi")], False),
    ]


def _priya_after_refund() -> dict:
    """What: VERIFIED with the S1 refund (CH-00000412, $96) in recent_actions, as the memory refresh shows it after the
    commit (G-03 turn 4). Use when: the S1 follow-up case. Output: state values. Side effects: none. Role: helper."""
    ctx = ex.ME_CONTEXT.model_dump(mode="json")
    refund = {"action_type": "refund", "label": "Refunded $96.00 for the duplicate charge ending 0412",
              "effective_date": "2026-11-27", "committed_by": "customer", "amount_cents": 9600}
    return {**VERIFIED, "context_snapshot": {**ctx, "recent_actions": [refund, *ctx["recent_actions"]]}}


def _doc_text(article: str, heading: str) -> str:
    """What: a section's markdown from data/articles. Use when: token cap cases. Inputs: slug, heading.
    Output: text. Side effects: reads one file. Role: helper."""
    text = (ROOT / "data" / "articles" / f"{article}.md").read_text()
    return f"## {heading}\n" + text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0].strip()


class _Recorder:
    """What: wraps the real chat model to keep its last reply (stop reason and usage). Use when: the token cap
    measurement. Inputs: model. Output: ainvoke passthrough. Side effects: stores replies. Role: probe."""

    def __init__(self, model) -> None:
        """What: keep the model. Use when: tests. Inputs: model. Output: none. Side effects: none. Role: constructor."""
        self.inner, self.model, self.replies = model, getattr(model, "model", None), []

    async def ainvoke(self, messages, config=None):
        """What: call the real model and record the reply. Use when: a gate call. Inputs: messages, config.
        Output: reply. Side effects: records. Role: passthrough."""
        reply = await self.inner.ainvoke(messages, config=config)
        self.replies.append(reply)
        return reply


@pytest.mark.real
async def test_real_gate_models_return_a_verdict_within_the_cap():
    """8 verifier calls and 2 scope classifier calls (10 real calls, cap 12), one at a time for clean latency."""
    from agent.gates import ScopeClassifier, reply_truncated

    load_dotenv(ROOT / ".env", override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("ANTHROPIC_API_KEY missing: this eval must not pass without making the calls")
    registry = ModelRegistry(load_models_config(ROOT / "data" / "models.yaml"))
    bad = []
    for label, draft, values, msgs, expect in token_cap_cases():
        rec = _Recorder(registry.build("grounding_verifier"))
        ver = GroundingVerifier(rec)
        evidence = session_evidence(values, msgs)
        start = time.monotonic()
        raw = await ver.verify(TurnControl(session_id="eval", turn_id="eval"), draft, evidence)
        secs = time.monotonic() - start
        res = apply_floor(enforce_code_rules(raw, draft, evidence.chunks, tool_names=evidence.ids),
                          needs_grounding(draft, any(isinstance(m, ToolMessage) for m in msgs)))  # D-064
        reply = rec.replies[-1] if rec.replies else None
        meta = getattr(reply, "response_metadata", {}) or {}
        usage = getattr(reply, "usage_metadata", {}) or {}
        ok = res.passed is expect and not (reply is not None and reply_truncated(reply))
        bad += [] if ok else [label]
        print(f"{'ok ' if ok else 'BAD'} {label} | expect={'pass' if expect else 'flag'} verdict={res.verdict} "
              f"reason={res.reason} stop={meta.get('stop_reason') or meta.get('finish_reason')} "
              f"out={usage.get('output_tokens')} reasoning={(usage.get('output_token_details') or {}).get('reasoning')} "
              f"{secs:.2f}s claims={[(c['text'][:50], c['supported'], c['source_id']) for c in res.claims]}")
    for text, on in (("Can I get a refund on my Pro plan?", True), ("What's the weather in Paris tomorrow?", False)):
        rec = _Recorder(registry.build("scope_classifier"))
        start = time.monotonic()
        got = await ScopeClassifier(rec).on_topic(TurnControl(session_id="eval", turn_id="eval"), text)
        secs = time.monotonic() - start
        reply = rec.replies[-1] if rec.replies else None
        meta = getattr(reply, "response_metadata", {}) or {}
        usage = getattr(reply, "usage_metadata", {}) or {}
        ok = got is on and reply is not None and not reply_truncated(reply)
        bad += [] if ok else [text]
        print(f"{'ok ' if ok else 'BAD'} scope {text!r} | on_topic={got} answer={getattr(reply, 'text', None)!r} "
              f"stop={meta.get('stop_reason') or meta.get('finish_reason')} out={usage.get('output_tokens')} {secs:.2f}s")
    assert not bad, bad


# --- Browser pass 2b: the live S3 Marcus draft (ticket offer judged unsupported) and two controls ------------------
MARCUS_LIVE = ("I can't prepare this refund. Your account (Northwind Logistics) has you as a member. Only an account "
               "owner or billing admin can make billing changes. This comes from the \"Billing permissions\" section "
               "of the roles-and-permissions article. Your options are to ask an owner or billing admin at your "
               "company to request the refund. I can also open a ticket for our support team about it if you'd like.")


@pytest.mark.real
async def test_real_verifier_accepts_the_ticket_offer_and_still_flags_invented_tickets():
    """3 verifier calls: the live S3 member decline must pass; a ticket the turn never opened and a reply deadline
    must still be flagged (agent_capabilities states neither)."""
    _, _, values, msgs, _ = token_cap_cases()[1]
    cases = [(MARCUS_LIVE, True),
             ("I've opened a ticket for our support team, ticket ending 4821.", False),
             ("I can open a ticket, and our team will reply within one hour.", False)]
    registry = ModelRegistry(load_models_config(ROOT / "data" / "models.yaml"))
    load_dotenv(ROOT / ".env", override=False)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail("ANTHROPIC_API_KEY missing: this eval must not pass without making the calls")
    bad = []
    for draft, expect in cases:
        ver = GroundingVerifier(registry.build("grounding_verifier"))
        evidence = session_evidence(values, msgs)
        raw = await ver.verify(TurnControl(session_id="eval", turn_id="eval"), draft, evidence)
        res = enforce_code_rules(raw, draft, evidence.chunks, tool_names=evidence.ids)
        ok = res.passed is expect
        bad += [] if ok else [draft]
        print(f"\n{'ok ' if ok else 'BAD'} expect={'pass' if expect else 'flag'} verdict={res.verdict} "
              f"commitment={res.commitment} claims={[(c['text'][:60], c['supported'], c['source_id']) for c in res.claims]}")
    assert not bad, bad
