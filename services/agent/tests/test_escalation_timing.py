"""What: timing proofs of the 3 s escalation reserve (doc 07 s8.1, s8.2, s8.3) on a virtual clock: the failure ticket
after a failed commit runs in the reserve and is not cut by the agent loop budget, and the deadline path stops the
loop 3 s before the deadline and opens the deadline ticket inside the reserve.
Use when: pytest. The clock is injected through the event loop (tests/virtual_clock.py): TurnControl reads the
running loop's time, so asyncio timeouts, clamps and the fake core's delays all run in virtual seconds.
Inputs: none. Output: pass/fail. Side effects: none.
Role: fix for the B1 concern on PR #47 (ingress used to stop the whole body at deadline minus 3 s)."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
from contracts.stubs import examples as ex
from langchain_core.messages import AIMessage, HumanMessage

from escalation_world import OWNER, Fake, World, error_response, json_response, of
from virtual_clock import run_virtual

DEADLINE_S = 12.0  # a short chat deadline keeps the arithmetic readable; the reserve is the real 3 s


def _ticket_recorder(times: list[tuple[float, float]], took_s: float) -> Any:
    """What: a ticket POST handler that takes `took_s` virtual seconds and records (start, end).
    Use when: timing tests. Inputs: list to append to, duration. Output: async handler. Side effects: records.
    Role: fake core ticket endpoint."""

    async def handler(_: httpx.Request) -> httpx.Response:
        """What: delay, then the created ticket. Use when: POST /v1/tickets. Inputs: request. Output: response.
        Side effects: records times. Role: fake."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.sleep(took_s)
        times.append((start, loop.time()))
        return json_response(ex.TICKET_CREATED)

    return handler


def _sleep_then(seconds: float, response: httpx.Response | None) -> Any:
    """What: a handler that sleeps (virtual) and then answers, or hangs when response is None.
    Use when: slow or hung core paths. Inputs: seconds, response. Output: async handler. Side effects: none.
    Role: fake."""

    async def handler(_: httpx.Request) -> httpx.Response:
        """What: delay then answer. Use when: overrides. Inputs: request. Output: response. Side effects: none.
        Role: fake."""
        await asyncio.sleep(seconds)
        return response if response is not None else error_response()

    return handler


def test_commit_failure_ticket_runs_in_the_reserve_and_is_not_cut_by_the_loop_budget() -> None:
    """me/context 1.9 s; commit hangs until its clamp (min(5, 12 - 1.9 - 3) = 5 s, ends 6.9 s); status check hangs
    until its clamp (2 s, ends 8.9 s); no retry fits (12 - 8.9 - 0.2 - 3 < 5); the unconfirmed ticket then takes
    2.5 s and must finish after the loop budget (9 s) and before the deadline (12 s)."""
    times: list[tuple[float, float]] = []
    pending = ex.PENDING_PUBLIC.model_dump(mode="json")
    ctx = {**ex.ME_CONTEXT.model_dump(mode="json"), "pending_action": pending}

    async def scenario() -> tuple[Any, list[Any], float]:
        """What: run the confirm turn. Use when: this test. Inputs: none. Output: outcome, events, end time.
        Side effects: none. Role: body."""
        w = World(use_stub=False, deadlines={"chat": DEADLINE_S, "voice": DEADLINE_S}, overrides={
            ("GET", "/v1/me/context"): _sleep_then(1.9, json_response(ctx)),
            ("POST", "/commit"): _sleep_then(600, None),
            ("GET", f"/v1/actions/pending/{ex.PENDING_ID}"): _sleep_then(600, None),
            ("POST", "/v1/tickets"): _ticket_recorder(times, 2.5)})
        await w.seed({"messages": [HumanMessage("refund"), AIMessage("read-back", id="route-t0")], **OWNER})
        start = asyncio.get_running_loop().time()
        outcome, events = await w.send(confirm={"pending_action_id": ex.PENDING_ID, "decision": "confirm"})
        return outcome, events, asyncio.get_running_loop().time() - start

    (outcome, events, elapsed), _ = run_virtual(scenario())
    assert len(times) == 1
    start, end = times[0]
    assert DEADLINE_S - 3 < end <= DEADLINE_S, times  # the ticket used the reserve
    assert outcome.end_reason == "escalated" and outcome.final.ticket.short_id == "8213"
    assert of(events, "action.result")[0].outcome == "unconfirmed"
    assert "Ticket ending 8213" in outcome.final.text and elapsed <= DEADLINE_S


def test_deadline_path_stops_the_loop_three_seconds_early_and_opens_the_deadline_ticket() -> None:
    """The model hangs; the loop stops at 12 - 3 = 9 s; the deadline ticket (0.5 s) runs in the reserve."""
    times: list[tuple[float, float]] = []

    async def scenario() -> tuple[Any, list[Any]]:
        """What: one hung chat turn. Use when: this test. Inputs: none. Output: outcome, events. Side effects: none.
        Role: body."""
        w = World(model=Fake(messages=iter([AIMessage("late")]), hang_s=600), verified=False,
                  deadlines={"chat": DEADLINE_S, "voice": DEADLINE_S},
                  overrides={("POST", "/v1/tickets"): _ticket_recorder(times, 0.5)})
        return await w.send(text="why was I charged twice?")

    (outcome, events), loop = run_virtual(scenario())
    assert len(times) == 1
    start, end = times[0]
    assert abs(start - (DEADLINE_S - 3)) < 0.1 and end <= DEADLINE_S, times
    assert outcome.end_reason == "deadline" and outcome.status == "failed"
    assert outcome.final.text == "I couldn't finish that in time, so I've opened a ticket for our team: ticket ending 8213."
    assert outcome.final.escalated is True and outcome.final.ticket.short_id == "8213"
    types = [e.type for e in events]
    assert types.index("error") < types.index("escalated") < types.index("turn.completed")
    assert [(e.reason, e.origin) for e in of(events, "escalated")] == [("deadline", "code")]
    assert of(events, "error")[0].class_ == "turn_deadline_exceeded"
