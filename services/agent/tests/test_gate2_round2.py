"""What: tests for review PR #71 fix round 2: N1 a static sid vocabulary sent as a real enum (it survives the
provider schema transform), N2 the startup warm-up of the verifier schema, N3 code-written lines and the
session_state ticket list bound to the verification they were written under, N4 public results carried across a
verification change.
Use when: pytest (fakes; no provider, no network). Inputs: none. Output: pass/fail. Side effects: none.
Role: build-log/reports/rereview-pr71-r1.md N1 to N4.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from contracts.tools import TOOL_ARGS
from gate2_fakes import CitingVerifierModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_gate1 import search_body

from agent import line_ids
from agent.gates import (
    DOC_SIDS_MAX,
    TOOL_RESULTS_MAX,
    GroundingVerifier,
    session_evidence,
    sid_vocabulary,
    verdict_schema,
    warm_up_verifier,
)
from agent.models.structured import structured_output_kwargs
from agent.records import session_tickets, ticket_record
from agent.turn_control import TurnControl
from agent.untrusted import wrap_result, wrap_search

CHUNK = search_body("pass")["chunks"][0]
PRIYA = {"verified": True, "claims_snapshot": {"user_id": "U-1", "account_id": "QS-10423"}}
DANA = {"verified": True, "claims_snapshot": {"user_id": "U-9", "account_id": "QS-20318"}}
P_KEY, D_KEY = "U-1|QS-10423", "U-9|QS-20318"


def _search(cid: str, bound_to: str | None) -> list[Any]:
    """What: a search turn as ToolGuard writes it. Use when: N4. Inputs: call id, stamp. Output: messages.
    Side effects: none. Role: helper."""
    doc = {"id": CHUNK["chunk_id"], "article": "refund-policy", "section": "Pro", "text": CHUNK["text"],
           "injection_suspect": False}
    return [HumanMessage("q"), AIMessage("", tool_calls=[{"name": "search_kb", "args": {}, "id": cid,
                                                           "type": "tool_call"}]),
            ToolMessage(wrap_search("pass", None, [doc]), tool_call_id=cid, name="search_kb",
                        artifact={"bound_to": bound_to, "grounded_chunks": [CHUNK]}), AIMessage("a")]


# --- N1 ---------------------------------------------------------------------------------------------------------------


def test_n1_the_vocabulary_is_closed_and_bounded():
    vocab = sid_vocabulary()
    assert {"agent_capabilities", "account_context", "session_state", "fixed_line@1", "fixed_line@3"} <= set(vocab)
    for tool in set(TOOL_ARGS) - {"search_kb"}:
        assert f"{tool}@1" in vocab and f"{tool}@{TOOL_RESULTS_MAX}" in vocab and f"{tool}@{TOOL_RESULTS_MAX + 1}" not in vocab
    assert f"doc@{DOC_SIDS_MAX}" in vocab and f"doc@{DOC_SIDS_MAX + 1}" not in vocab
    assert len(vocab) == len(set(vocab)) and verdict_schema() == verdict_schema()


def test_n1_the_enum_is_in_the_anthropic_payload_actually_sent():
    from langchain_anthropic import ChatAnthropic

    model = ChatAnthropic(model="claude-haiku-5-5", api_key="test-key", max_tokens=64)  # type: ignore[call-arg]
    payload = model._get_request_payload([HumanMessage("x")], **structured_output_kwargs(model, verdict_schema()))
    branches = payload["output_config"]["format"]["schema"]["properties"]["claims"]["items"]["properties"][
        "source_id"]["anyOf"]
    string = next(b for b in branches if b.get("type") != "null")  # the transform keeps enum, drops "type"
    assert string["enum"] == sid_vocabulary()
    assert {"type": "null"} in branches
    assert "pattern" not in string and "pattern" not in str(string.get("description", ""))


def test_n1_every_sid_session_evidence_writes_is_in_the_vocabulary():
    thread = (_search("s-1", None) + [HumanMessage("q"), ToolMessage(wrap_result("list_tickets", {"tickets": []}),
                                                                    tool_call_id="t-1", name="list_tickets",
                                                                    artifact={"bound_to": P_KEY}), AIMessage("a")]
              + [line_ids.code_line(line_ids.ROUTE, "t0", "line", P_KEY), HumanMessage("now")])
    ev = session_evidence(PRIYA, thread, TurnControl(session_id="s", turn_id="t"))
    assert set(ev.citable) <= set(sid_vocabulary())
    assert "doc@1" in ev.citable and ev.chunks["doc@1"]["chunk_id"] == CHUNK["chunk_id"]
    assert ev.provenance["doc@1"]["chunk_id"] == CHUNK["chunk_id"]


def test_n1_more_chunks_than_the_bound_keep_the_newest():
    msgs: list[Any] = []
    for i in range(DOC_SIDS_MAX + 3):
        chunk = {**CHUNK, "chunk_id": f"refund-policy#c{i}", "text": f"Section {i}."}
        msgs += [ToolMessage(wrap_search("pass", None, []), tool_call_id=f"s-{i}", name="search_kb",
                             artifact={"bound_to": None, "grounded_chunks": [chunk]})]
    ev = session_evidence({}, [HumanMessage("now"), *msgs])
    ids = {c["chunk_id"] for c in ev.chunks.values()}
    assert len(ev.chunks) == DOC_SIDS_MAX and "refund-policy#c0" not in ids and f"refund-policy#c{DOC_SIDS_MAX + 2}" in ids


# --- N2 ---------------------------------------------------------------------------------------------------------------


async def test_n2_the_warm_up_sends_one_call_with_the_production_schema_and_never_raises(caplog: pytest.LogCaptureFixture):
    model = CitingVerifierModel(no_claims_reason="conversation")
    seen: list[dict[str, Any]] = []
    ver = GroundingVerifier(model, output_kwargs=lambda _m, schema: (seen.append(schema), {"schema": schema})[1])
    with caplog.at_level(logging.INFO, logger="agent.gates"):
        ok = await warm_up_verifier(ver, timeout_s=1.0)
    assert ok is True and len(model.seen) == 1 and seen == [verdict_schema()]
    assert any("verifier warm-up" in r.getMessage() for r in caplog.records)


async def test_n2_a_failing_or_slow_warm_up_is_logged_and_returns_false():
    class Boom:
        """What: a model that raises. Use when: this test. Inputs: any. Output: never. Side effects: raises.
        Role: fake."""

        async def ainvoke(self, *a: Any, **k: Any) -> Any:
            """What: raise. Use when: verify. Inputs: any. Output: never. Side effects: raises. Role: fake."""
            raise RuntimeError("down")

    class Slow:
        """What: a model that hangs. Use when: this test. Inputs: any. Output: never. Side effects: sleeps.
        Role: fake."""

        async def ainvoke(self, *a: Any, **k: Any) -> Any:
            """What: hang. Use when: verify. Inputs: any. Output: never. Side effects: sleeps. Role: fake."""
            await asyncio.sleep(10)

    assert await warm_up_verifier(GroundingVerifier(Boom()), timeout_s=0.5) is False
    assert await warm_up_verifier(GroundingVerifier(Slow()), timeout_s=0.2) is False


async def test_n2_the_warm_up_is_skipped_without_a_verifier_and_counts_in_no_turn():
    assert await warm_up_verifier(None, timeout_s=0.1) is False


# --- N3 ---------------------------------------------------------------------------------------------------------------


def _commit_line(bound_to: str | None) -> AIMessage:
    """What: the ingress commit summary line as the handler writes it. Use when: N3. Inputs: stamp.
    Output: AIMessage. Side effects: none. Role: helper."""
    return line_ids.code_line(line_ids.INGRESS, "t-commit",
                              "I refunded $96.00 to the card ending 4242 for invoice INV-1001.", bound_to)


def test_n3_code_lines_are_stamped_by_the_one_builder():
    msg = line_ids.code_line(line_ids.ROUTE, "T1", "hello", P_KEY)
    assert msg.id == "route-T1" and msg.response_metadata["bound_to"] == P_KEY


@pytest.mark.parametrize("values", [DANA, {"verified": False}])
def test_n3_a_code_line_written_under_another_verification_is_not_carried(values: dict[str, Any]):
    thread = [HumanMessage("refund"), _commit_line(P_KEY), HumanMessage("now")]
    ev = session_evidence(values, thread)
    assert not any(i.startswith("fixed_line") for i in ev.ids) and "INV-1001" not in ev.text


def test_n3_the_same_verification_and_public_lines_are_carried_and_unstamped_lines_are_not():
    same = session_evidence(PRIYA, [HumanMessage("refund"), _commit_line(P_KEY), HumanMessage("now")])
    assert "fixed_line@1" in same.ids and "INV-1001" in same.text
    public = session_evidence(PRIYA, [HumanMessage("hi"), line_ids.code_line(line_ids.ROUTE, "t0", "Verify, please.",
                                                                             None), HumanMessage("now")])
    assert "fixed_line@1" in public.ids
    bare = session_evidence(PRIYA, [HumanMessage("hi"), AIMessage("old", id="route-t0"), HumanMessage("now")])
    assert not any(i.startswith("fixed_line") for i in bare.ids)


def test_n3_the_session_ticket_list_is_bound_to_the_verification():
    control = TurnControl(session_id="s", turn_id="t1")
    control.identity = P_KEY
    rec = ticket_record(control, {"ticket_id": "TCK-1", "display": "ticket ending 1863"}, "grounding_failed")
    assert rec.response_metadata["bound_to"] == P_KEY
    assert session_tickets([rec], P_KEY) == ["ticket ending 1863"]
    assert session_tickets([rec], D_KEY) == [] and session_tickets([rec], None) == []
    ev = session_evidence(DANA, [HumanMessage("a"), rec, HumanMessage("now")])
    assert "ticket ending 1863" not in ev.text


# --- N4 ---------------------------------------------------------------------------------------------------------------


def test_n4_a_public_article_read_before_verifying_is_citable_after_verifying():
    ev = session_evidence(PRIYA, [*_search("s-1", None), HumanMessage("now")])
    assert "doc@1" in ev.citable and ev.chunks["doc@1"]["chunk_id"] == CHUNK["chunk_id"]


def test_n4_a_tier_scoped_search_made_under_an_identity_stays_bound():
    ev = session_evidence(DANA, [*_search("s-1", P_KEY), HumanMessage("now")])
    assert not ev.chunks
    unverified = session_evidence({"verified": False}, [*_search("s-1", P_KEY), HumanMessage("now")])
    assert not unverified.chunks


async def test_n2_startup_schedules_the_warm_up_in_the_background_only_with_a_key(monkeypatch: pytest.MonkeyPatch):
    from agent import composition

    calls: list[Any] = []

    async def fake(verifier: Any, **kw: Any) -> bool:
        """What: record the warm-up. Use when: this test. Inputs: verifier. Output: True. Side effects: appends.
        Role: spy."""
        calls.append(verifier)
        return True

    monkeypatch.setattr(composition, "warm_up_verifier", fake)
    ver = object()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert composition.start_verifier_warm_up(ver, "anthropic") is None and calls == []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
    task = composition.start_verifier_warm_up(ver, "anthropic")
    assert task is not None
    await task
    assert calls == [ver]


async def test_n3_the_turn_runs_under_its_verification_and_its_route_line_is_stamped_with_it():
    from test_structured_input import readback_world

    from agent.gates import identity_key

    w, _, _ = await readback_world()  # verified session; the read-back turn ends on a route line
    values = await w.values()
    route = [m for m in values["messages"] if str(m.id or "").startswith(line_ids.ROUTE)]
    assert route and identity_key(values) is not None
    assert route[-1].response_metadata["bound_to"] == identity_key(values)


def test_d070_capabilities_and_the_public_tool_set_follow_the_contract_v0_set():
    # D-070 (PR #78): no ticket without a verified requester, so the agent's own capability text must not claim a
    # ticket "in any conversation", and the public (carried) tool set is the contract's V0 set.
    from contracts.tools import VISIBILITY

    from agent.gates import AGENT_CAPABILITIES, V0_TOOLS

    assert V0_TOOLS == frozenset(VISIBILITY["V0"])
    assert "in any conversation" not in AGENT_CAPABILITIES
    assert "Once the customer is verified, it can open a ticket for the Quillstack support team" in AGENT_CAPABILITIES


async def test_m6_each_verifier_and_scope_classifier_call_is_counted_once_on_its_turn():
    # Doc 07 s2.3: usage.model_calls equals the LangSmith llm run count, so every gate call is recorded on the turn.
    from agent.gates import ScopeClassifier

    class Fail:
        """What: a model that raises. Use when: this test. Inputs: any. Output: never. Side effects: raises.
        Role: fake."""

        model = "fake-fail"

        async def ainvoke(self, *a: Any, **k: Any) -> Any:
            """What: raise. Use when: a call. Inputs: any. Output: never. Side effects: raises. Role: fake."""
            raise RuntimeError("down")

    class On:
        """What: a scope model answering ON. Use when: this test. Inputs: any. Output: AIMessage.
        Side effects: none. Role: fake."""

        model = "fake-scope"

        async def ainvoke(self, *a: Any, **k: Any) -> Any:
            """What: answer ON. Use when: a call. Inputs: any. Output: AIMessage. Side effects: none. Role: fake."""
            return AIMessage("ON", usage_metadata={"input_tokens": 9, "output_tokens": 1, "total_tokens": 10})

    for model, expect in ((On(), "fake-scope"), (Fail(), "fake-fail")):
        control = TurnControl(session_id="s", turn_id="t")
        await ScopeClassifier(model).on_topic(control, "hello")
        assert [name for name, _ in control.gate_calls] == [expect]
    for model in (CitingVerifierModel(no_claims_reason="conversation"), Fail()):
        control = TurnControl(session_id="s", turn_id="t")
        ev = session_evidence({}, [HumanMessage("hi")], control)
        await GroundingVerifier(model, timeout_s=0.5, min_s=0.0).verify(control, "Hello.", ev)
        assert len(control.gate_calls) == 1


async def test_m10_the_warm_up_is_not_counted_on_any_turn():
    from agent.turn_control import bind_turn_control

    turn = TurnControl(session_id="s", turn_id="live-turn")
    bind_turn_control(turn)  # a turn bound in this context must not receive the warm-up's call
    model = CitingVerifierModel(no_claims_reason="conversation")
    assert await warm_up_verifier(GroundingVerifier(model), timeout_s=1.0) is True
    assert len(model.seen) == 1 and turn.gate_calls == []


def test_m8_session_tickets_use_the_one_binding_rule():
    control = TurnControl(session_id="s", turn_id="t0")  # identity None: opened while unverified (public stamp)
    public = ticket_record(control, {"ticket_id": "TCK-2", "display": "ticket ending 2002"}, "deadline")
    control.identity = P_KEY
    priya = ticket_record(TurnControl(session_id="s", turn_id="t1", identity=P_KEY),
                          {"ticket_id": "TCK-1", "display": "ticket ending 1001"}, "deadline")
    unstamped = AIMessage("[System record] x", id="record-t2-ticket",
                          response_metadata={"qs_action": {"kind": "ticket", "display": "ticket ending 3003"}})
    msgs: list[Any] = [public, priya, unstamped]
    assert session_tickets(msgs, P_KEY) == ["ticket ending 2002", "ticket ending 1001"]
    assert session_tickets(msgs, D_KEY) == ["ticket ending 2002"]
