"""What: CS-E-03.F12, the escalation line states only the ticket result the code recorded. The route line is written
into the thread inside the agent loop, before the escalation module makes the one ticket attempt in the reserve; it
used to choose its ticket clause from `route.ticket is None`, which reads the same before the attempt and after a
failed one, so every escalation line in the loop said "I couldn't open a ticket just now" (seen in LangSmith for
G-07 while the ticket existed). These tests pin the three states: not attempted (no ticket claim), opened (the
ticket clause), failed (the s8.1 fallback clause).
Use when: pytest (fake models, contracts core stub over ASGI with failure overrides, in-memory checkpointer).
Inputs: none. Output: pass/fail. Side effects: none.
Role: red-first tests of the fix (build-log/reports/rca-escalation-reply.md)."""
from __future__ import annotations

from typing import Any

import pytest
from contracts.stubs import examples as ex
from langchain_core.messages import AIMessage, HumanMessage

from agent.escalation import Escalator
from agent.middleware.router import ESCALATION_LEADS, record_hook_ticket, route_reply
from agent.turn_control import Route, TurnControl
from escalation_world import OWNER, SID, World, error_response, json_response, of
from test_escalation import call, routed_search

NO_TICKET = "couldn't open a ticket"
LEAD = ESCALATION_LEADS["not_in_docs"]


def _control() -> TurnControl:
    """What: a turn with an escalate(not_in_docs) route and no ticket attempt yet. Use when: unit tests.
    Inputs: none. Output: TurnControl. Side effects: none. Role: helper."""
    control = TurnControl(session_id=SID, turn_id="01a21ff1-c81e-77a1-a236-d2d7ab071354")
    control.set_route(Route("escalate", reason="not_in_docs"))
    return control


def test_a_line_written_before_the_ticket_attempt_makes_no_claim_about_the_ticket() -> None:
    reply = route_reply(_control())
    assert reply.text == LEAD
    assert NO_TICKET not in reply.text and "opened a ticket" not in reply.text


def test_the_fallback_clause_needs_a_recorded_failed_attempt() -> None:
    control = _control()
    control.ticket_attempt = "failed"
    assert route_reply(control).text == f"{LEAD} I couldn't open a ticket just now. If you contact us, please quote reference 1354."


@pytest.mark.parametrize("override,recorded", [
    (None, "opened"),
    (lambda r: error_response(), "failed"),
    (lambda r: error_response("REQUESTER_UNKNOWN", 403, changed=False), "refused"),
    (lambda r: json_response({}), "failed"),  # a 200 answer with no ticket in it (review PR #87 I-1, m3)
])
async def test_the_escalator_records_every_ticket_attempt_result(override: Any, recorded: str) -> None:
    w = World([], overrides={("POST", "/v1/tickets"): override} if override else None)
    control = _control()
    core = w.ingress.run_agent.escalator.core  # type: ignore[attr-defined]
    await Escalator(core).open_ticket(control, "not_in_docs", summary="s")
    assert control.ticket_attempt == recorded


async def _skip_thread(*_: Any, **__: Any) -> None:
    """What: stands in for Escalator._thread when the best-effort rewrite is skipped (no time left, or the write
    failed: both are logged and swallowed in production). Use when: the thread-truth tests. Inputs: ignored.
    Output: none. Side effects: none. Role: fault double."""
    return None


async def test_the_thread_never_says_no_ticket_while_a_ticket_exists_when_the_rewrite_is_skipped(
        monkeypatch: pytest.MonkeyPatch) -> None:
    w = World([call("search_kb", {"query": "android app"})], tools={"search_kb": routed_search("not_in_docs")})
    monkeypatch.setattr(Escalator, "_thread", _skip_thread)
    outcome, _ = await w.send(text="Do you have an Android app for Quillstack?")
    assert outcome.final.ticket is not None and len(w.transport.tickets) == 1
    lines = [str(m.content) for m in (await w.values())["messages"] if str(m.id or "").startswith("route-")]
    assert lines and all(NO_TICKET not in line for line in lines), lines


async def test_g07_replay_the_verified_continuation_line_reports_the_ticket_it_opened(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """G-07 as it ran on 2026-11-28 02:17 UTC (LangSmith cs-f11-proof, run 01a21ff1-c81e): a weak first turn
    clarifies, the card verifies Steve, the continued request escalates not_in_docs and code opens the ticket."""
    snapshots: list[list[str]] = []
    real_finish = Escalator.finish

    async def finish(self: Escalator, stream: Any, control: TurnControl, outcome: Any, agent: Any, config: Any) -> Any:
        """What: record the thread as the loop left it (what the LangSmith root run outputs), then finish.
        Use when: this test. Inputs: as Escalator.finish. Output: its outcome. Side effects: one read. Role: probe."""
        state = await agent.aget_state(config)
        snapshots.append([str(m.content) for m in state.values.get("messages", []) if str(m.id or "").startswith("route-")])
        return await real_finish(self, stream, control, outcome, agent, config)

    monkeypatch.setattr(Escalator, "finish", finish)
    w = World([AIMessage(content="Do you mean a native Android app?"), call("search_kb", {"query": "android app"})],
              tools={"search_kb": routed_search("not_in_docs")}, verified=False, user_turns=1)
    await w.send(text="Do you have an Android app for Quillstack?")
    outcome, events = await w.send(verification={"account_id": ex.ACCOUNT_ID, "pin": ex.STUB_PIN})
    assert outcome.end_reason == "escalated" and outcome.final.ticket is not None
    assert outcome.final.text == f"{LEAD} I've opened a ticket for our team, ticket ending 8213."
    assert [e.ticket.short_id for e in of(events, "escalated")] == ["8213"]
    loop_end = snapshots[-1]
    assert loop_end and all(NO_TICKET not in line for line in loop_end), loop_end


async def test_a_failed_ticket_still_says_so_with_the_turn_reference() -> None:
    w = World([call("search_kb", {"query": "android app"})], tools={"search_kb": routed_search("not_in_docs")},
              overrides={("POST", "/v1/tickets"): lambda r: error_response()})
    await w.seed({"messages": [HumanMessage("hi"), AIMessage("hello", id="a0")], **OWNER})
    outcome, events = await w.send(text="Do you have an Android app for Quillstack?")
    ref = events[0].turn_id.replace("-", "")[-4:]
    want = f"{LEAD} I couldn't open a ticket just now. If you contact us, please quote reference {ref}."
    assert outcome.final.text == want and outcome.final.ticket is None
    lines = [str(m.content) for m in (await w.values())["messages"] if str(m.id or "").startswith("route-")]
    assert lines == [want]


FALLBACK = f"{LEAD} I couldn't open a ticket just now. If you contact us, please quote reference 1354."


async def _attempt(control: TurnControl, override: Any = None) -> World:
    """What: one Escalator.open_ticket attempt for control against the core stub. Use when: the exit tests.
    Inputs: control, optional ticket POST override. Output: the world (its transport records the POSTs).
    Side effects: at most one fake POST. Role: helper."""
    w = World([], overrides={("POST", "/v1/tickets"): override} if override else None)
    await Escalator(w.ingress.run_agent.escalator.core).open_ticket(control, "not_in_docs", summary="s")  # type: ignore[attr-defined]
    return w


async def test_too_little_time_left_is_a_failed_attempt_and_the_line_says_no_ticket() -> None:
    control = _control()
    control.clock, control.deadline = (lambda: 0.0), 0.5  # under the 1 s floor: no POST is sent (review PR #87 I-1, m2)
    w = await _attempt(control)
    assert w.transport.tickets == [] and control.ticket_attempt == "failed"
    assert route_reply(control).text == FALLBACK


async def test_an_answer_with_no_ticket_is_a_failed_attempt_and_the_line_says_no_ticket() -> None:
    control = _control()
    w = await _attempt(control, lambda r: json_response({}))
    assert len(w.transport.tickets) == 1 and control.ticket_attempt == "failed"
    reply = route_reply(control)
    assert reply.text == FALLBACK and reply.ticket is None


async def test_an_answer_with_no_ticket_reaches_the_customer_as_the_fallback_end_to_end() -> None:
    w = World([call("search_kb", {"query": "android app"})], tools={"search_kb": routed_search("not_in_docs")},
              overrides={("POST", "/v1/tickets"): lambda r: json_response({})})
    await w.seed({"messages": [HumanMessage("hi"), AIMessage("hello", id="a0")], **OWNER})
    outcome, events = await w.send(text="Do you have an Android app for Quillstack?")
    ref = events[0].turn_id.replace("-", "")[-4:]
    assert outcome.final.text == f"{LEAD} I couldn't open a ticket just now. If you contact us, please quote reference {ref}."
    assert outcome.final.ticket is None and [e.ticket for e in of(events, "escalated")] == [None]


@pytest.mark.parametrize("before,ticket,after", [
    (None, None, "failed"),        # a hook that answered None tried and failed (m7)
    ("refused", None, "refused"),  # the hook's own more exact record is kept (m8)
    (None, {"short_id": "8213"}, "opened"),  # m11
])
def test_record_hook_ticket_records_the_hook_result(before: str | None, ticket: Any, after: str) -> None:
    control = _control()
    control.ticket_attempt = before  # type: ignore[assignment]
    assert record_hook_ticket(control, ticket) == ticket
    assert control.ticket_attempt == after


async def test_a_failed_attempt_is_the_turns_one_attempt_so_the_reserve_does_not_try_again() -> None:
    control = _control()
    control.ticket_attempt = "failed"  # e.g. the optional in-loop hook already tried (review PR #87 M-1)
    w = await _attempt(control)
    assert w.transport.tickets == [] and control.ticket_attempt == "failed"
    assert route_reply(control).text == FALLBACK
