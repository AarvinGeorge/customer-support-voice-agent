"""What: every doc 07 s7.3 outcome row and the failure sequence (status check by idempotency key, budget-checked
retry with the same key, `unconfirmed`) for the Confirmer, against a scripted core (httpx MockTransport).
Use when: pytest. Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when of CS-E-03.S7.T8 ("every outcome row has a passing test"). Step-up rows are out of the thin slice."""
from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from contracts.stubs import examples as ex

from agent.structured.confirm import (
    CANCELLED_LINE,
    NOT_ACTIVE_LINE,
    TIMED_OUT_LINE,
    Confirmer,
    CoreCommitClient,
)
from agent.turn_control import TurnControl

SID, TID = ex.SESSION_ID, "019a7c2e-5b20-7000-8000-0000000000f1"
PID = ex.PENDING_ID
COMMIT_CARD = ex.PENDING_PUBLIC.model_dump(mode="json")
SUBMIT_CARD = {**COMMIT_CARD, "kind": "submit_for_approval", "state": "awaiting_customer"}
TICKET = ex.TICKET_REF.model_dump(mode="json")


def err(status: int, code: str, changed: bool | None = False, retryable: bool = False, **details: Any) -> httpx.Response:
    """What: an s1.3 error response. Use when: scripts. Inputs: status, code, changed, retryable, details.
    Output: response. Side effects: none. Role: helper."""
    return httpx.Response(status, json={"error": {"class": "core_conflict", "code": code, "message": "m",
                                                  "retryable": retryable, "changed": changed, "fault_injected": False,
                                                  "details": details}})


def status(state: str, **extra: Any) -> httpx.Response:
    """What: a PendingStatus 200. Use when: status check scripts. Inputs: state, extras. Output: response.
    Side effects: none. Role: helper."""
    return httpx.Response(200, json={**ex.PENDING_STATUS.model_dump(mode="json"), "state": state, **extra})


COMMIT_OK = httpx.Response(200, json=ex.COMMIT_RESULT.model_dump(mode="json"))
SUBMIT_OK = httpx.Response(200, json=ex.SUBMIT_RESULT.model_dump(mode="json"))
TOOL_ERROR = err(503, "TOOL_ERROR", changed=False, retryable=True)
INTERNAL = err(500, "INTERNAL", changed=None, retryable=True)
TIMEOUT = httpx.ReadTimeout("slow")


class Core:
    """What: a scripted core keyed by path suffix; records every request. Use when: each test.
    Inputs: {suffix: [responses or exceptions]}. Output: transport, log. Side effects: none. Role: fake."""

    def __init__(self, **script: list[Any]) -> None:
        """What: keep the script. Use when: tests. Inputs: script. Output: none. Side effects: none. Role: constructor."""
        self.script = {k: list(v) for k, v in script.items()}
        self.log: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """What: next outcome for the request's path. Use when: transport. Inputs: request. Output: response.
        Side effects: logs. Role: fake."""
        self.log.append(request)
        path = request.url.path
        key = "status" if request.method == "GET" else path.rsplit("/", 1)[-1]
        item = self.script[key].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def paths(self) -> list[str]:
        """What: method and last path segment per call. Use when: asserting order. Inputs: none. Output: list.
        Side effects: none. Role: helper."""
        return [f"{r.method} {r.url.path.rsplit('/', 1)[-1]}" for r in self.log]


class Tickets:
    """What: a recording failure-ticket opener. Use when: tests. Inputs: the ticket to return (None fails).
    Output: awaitable opener. Side effects: records. Role: fake."""

    def __init__(self, ticket: dict[str, Any] | None = TICKET) -> None:
        """What: keep the result. Use when: tests. Inputs: ticket. Output: none. Side effects: none. Role: constructor."""
        self.ticket, self.reasons = ticket, []

    async def __call__(self, control: TurnControl, reason: str, pending: dict[str, Any]) -> dict[str, Any] | None:
        """What: record and answer. Use when: Confirmer failure rows. Inputs: control, reason, pending.
        Output: ticket or None. Side effects: records. Role: fake."""
        self.reasons.append(reason)
        return self.ticket


def make(core: Core, tickets: Tickets | None = None, remaining_s: float = 40.0) -> tuple[Confirmer, TurnControl, Tickets]:
    """What: a Confirmer on the scripted core. Use when: tests. Inputs: core, opener, seconds left in the turn.
    Output: (confirmer, control, opener). Side effects: none. Role: helper."""
    t = tickets or Tickets()
    client = CoreCommitClient("http://core:8000", "commit-tok", http=httpx.AsyncClient(transport=httpx.MockTransport(core)))
    return Confirmer(client, t), TurnControl(session_id=SID, turn_id=TID, deadline=time.monotonic() + remaining_s), t


async def test_commit_200_is_committed_with_summary_and_summary_say() -> None:
    core = Core(commit=[COMMIT_OK])
    c, control, _ = make(core)
    r = await c.confirm(control, COMMIT_CARD)
    assert (r.outcome, r.changed, r.text, r.say) == ("committed", True, ex.COMMIT_RESULT.summary, ex.COMMIT_RESULT.summary_say)
    req = core.log[0]
    assert req.headers["authorization"] == "Bearer commit-tok" and req.headers["x-turn-id"] == TID
    assert req.headers["idempotency-key"] == PID and req.headers["x-session-id"] == SID


async def test_commit_replayed_200_is_still_committed() -> None:
    body = {**ex.COMMIT_RESULT.model_dump(mode="json"), "replayed": True}
    c, control, _ = make(Core(commit=[httpx.Response(200, json=body)]))
    assert (await c.confirm(control, COMMIT_CARD)).outcome == "committed"


async def test_submit_200_is_submitted_with_the_ticket_and_the_submit_key() -> None:
    core = Core(submit=[SUBMIT_OK])
    c, control, _ = make(core)
    r = await c.confirm(control, SUBMIT_CARD)
    assert r.outcome == "submitted" and r.changed is True and r.text == ex.SUBMIT_RESULT.summary
    assert r.text.startswith("I've sent this to our team for approval: ticket ending 8213.")
    assert r.ticket and r.ticket["short_id"] == "8213" and core.log[0].headers["idempotency-key"] == f"{PID}:submit"


@pytest.mark.parametrize("op_card,key", [(COMMIT_CARD, "commit"), (SUBMIT_CARD, "submit")])
async def test_rules_changed_gives_core_message_policy_and_nothing_was_changed(op_card: dict[str, Any], key: str) -> None:
    ref = {"article": "refund-policy", "section": "2", "title": "Refunds on the Pro plan"}
    resp = err(409, "RULES_CHANGED", reason_codes=["PRO_OVER_LIMIT"], policy_refs=[ref],
               message="This refund is now over the Pro limit.")
    c, control, _ = make(Core(**{key: [resp]}))
    r = await c.confirm(control, op_card)
    assert r.outcome == "rules_changed" and r.changed is False and r.policy_refs == [ref]
    assert r.text == "This refund is now over the Pro limit. See Refunds on the Pro plan. Nothing was changed."
    assert r.reason_codes == ["PRO_OVER_LIMIT"]


async def test_pending_expired_410() -> None:
    c, control, _ = make(Core(commit=[err(410, "PENDING_EXPIRED")]))
    r = await c.confirm(control, COMMIT_CARD)
    assert (r.outcome, r.changed, r.text) == ("expired", False, TIMED_OUT_LINE)


@pytest.mark.parametrize("resp", [err(409, "PENDING_NOT_ACTIVE"), err(404, "NOT_FOUND")])
async def test_not_active_409_or_404(resp: httpx.Response) -> None:
    c, control, _ = make(Core(submit=[resp]))
    r = await c.confirm(control, SUBMIT_CARD)
    assert (r.outcome, r.changed, r.text) == ("expired", False, NOT_ACTIVE_LINE)


async def test_confirmation_required_repeats_the_readback() -> None:
    c, control, _ = make(Core(commit=[err(409, "CONFIRMATION_REQUIRED")]))
    r = await c.confirm(control, COMMIT_CARD)
    assert r.outcome == "failed" and r.changed is False and r.repeat_readback and r.text == ex.READBACK.chat


async def test_commit_503_on_the_last_attempt_is_failed_nothing_changed_with_a_ticket() -> None:
    core = Core(commit=[TOOL_ERROR, TOOL_ERROR], status=[status("prepared")])
    c, control, t = make(core)
    r = await c.confirm(control, COMMIT_CARD)
    assert core.paths() == ["POST commit", "GET " + PID, "POST commit"]
    assert {q.headers["idempotency-key"] for q in core.log if q.method == "POST"} == {PID}  # same key on the retry
    assert (r.outcome, r.changed) == ("failed", False) and r.end_reason == "escalated"
    assert r.text == "Our billing system didn't respond, so nothing was changed. I've opened a ticket, ending 8213."
    assert t.reasons == ["commit_failed"] and r.ticket == TICKET


async def test_submit_503_on_the_last_attempt_is_failed_nothing_sent() -> None:
    c, control, _ = make(Core(submit=[TOOL_ERROR, TOOL_ERROR], status=[status("awaiting_customer")]))
    r = await c.confirm(control, SUBMIT_CARD)
    assert r.outcome == "failed" and r.changed is False
    assert r.text == "I couldn't send this to our team just now, so nothing was sent. I've opened a ticket, ending 8213."


async def test_commit_timeout_with_a_failed_status_check_is_unconfirmed_never_nothing_changed() -> None:
    core = Core(commit=[TIMEOUT], status=[httpx.ConnectError("down")])
    c, control, t = make(core)
    r = await c.confirm(control, COMMIT_CARD)
    assert core.paths() == ["POST commit", "GET " + PID]  # no blind retry
    assert r.outcome == "unconfirmed" and r.changed is None and "nothing was changed" not in r.text.lower()
    assert r.text == ("I couldn't confirm whether that went through, so I haven't tried again. Ticket ending 8213 is "
                      "with our team, and you won't be charged or refunded twice.")
    assert t.reasons == ["commit_unconfirmed"]


async def test_submit_500_with_a_failed_status_check_is_unconfirmed() -> None:
    c, control, _ = make(Core(submit=[INTERNAL], status=[err(503, "TOOL_ERROR", retryable=True)]))
    r = await c.confirm(control, SUBMIT_CARD)
    assert r.outcome == "unconfirmed" and r.changed is None
    assert r.text == ("I couldn't confirm whether your request reached our team. Ticket ending 8213 is with them, and "
                      "it won't be approved twice.")


async def test_500_then_status_committed_reports_success_without_a_retry() -> None:
    result = ex.COMMIT_RESULT.model_dump(mode="json")
    core = Core(commit=[INTERNAL], status=[status("committed", result=result)])
    c, control, t = make(core)
    r = await c.confirm(control, COMMIT_CARD)
    assert r.outcome == "committed" and r.text == ex.COMMIT_RESULT.summary and core.paths()[-1].startswith("GET")
    assert t.reasons == []


async def test_submit_timeout_then_status_awaiting_approval_cites_the_status_ticket() -> None:
    ticket = {**TICKET, "stage": "pending_approval", "queue": "approvals"}
    c, control, _ = make(Core(submit=[TIMEOUT], status=[status("awaiting_approval", ticket=ticket)]))
    r = await c.confirm(control, SUBMIT_CARD)
    assert r.outcome == "submitted" and "ticket ending 8213" in r.text and r.ticket == ticket


async def test_503_then_status_open_then_retry_succeeds() -> None:
    core = Core(commit=[TOOL_ERROR, COMMIT_OK], status=[status("prepared")])
    c, control, _ = make(core)
    assert (await c.confirm(control, COMMIT_CARD)).outcome == "committed" and len(core.log) == 3


async def test_retry_timeout_follows_the_retry_response_unconfirmed() -> None:
    c, control, _ = make(Core(commit=[TOOL_ERROR, TIMEOUT], status=[status("prepared")]))
    assert (await c.confirm(control, COMMIT_CARD)).outcome == "unconfirmed"


async def test_no_budget_for_the_retry_follows_the_last_response() -> None:
    core = Core(commit=[TOOL_ERROR], status=[status("prepared")])
    c, control, _ = make(core, remaining_s=8.0)  # 8 - 0.2 - 3 < 5: no retry fits
    r = await c.confirm(control, COMMIT_CARD)
    assert len(core.log) == 2 and r.outcome == "failed" and r.changed is False


async def test_failed_ticket_gives_the_reference_line() -> None:
    c, control, _ = make(Core(commit=[TOOL_ERROR], status=[httpx.ConnectError("down")]), tickets=Tickets(None))
    r = await c.confirm(control, COMMIT_CARD)
    assert r.text.startswith("Our billing system didn't respond, so nothing was changed. I couldn't open a ticket")
    assert TID.replace("-", "")[-4:] in r.text and r.ticket is None


async def test_status_says_expired_after_a_503() -> None:
    c, control, _ = make(Core(commit=[TOOL_ERROR], status=[status("expired")]))
    r = await c.confirm(control, COMMIT_CARD)
    assert (r.outcome, r.changed, r.text) == ("expired", False, TIMED_OUT_LINE)


async def test_cancel_200_says_nothing_changed_and_carries_x_turn_id() -> None:
    core = Core(cancel=[httpx.Response(200, json=ex.CANCEL_RESULT.model_dump(mode="json"))])
    c, control, _ = make(core)
    r = await c.cancel(control, COMMIT_CARD)
    assert (r.outcome, r.changed, r.text, r.end_reason) == ("cancelled", False, CANCELLED_LINE, "cancelled")
    assert core.log[0].headers["x-turn-id"] == TID


async def test_cancel_not_active_and_cancel_failure() -> None:
    c, control, _ = make(Core(cancel=[err(409, "PENDING_NOT_ACTIVE")]))
    assert (await c.cancel(control, COMMIT_CARD)).text == NOT_ACTIVE_LINE
    c, control, _ = make(Core(cancel=[TIMEOUT]))
    r = await c.cancel(control, COMMIT_CARD)
    assert r.outcome == "failed" and r.keep_pending


async def test_no_time_left_skips_the_commit_call() -> None:
    core = Core(commit=[], status=[])
    c, control, _ = make(core, remaining_s=3.5)  # clamp 0.5 s < 1 s minimum
    r = await c.confirm(control, COMMIT_CARD)
    assert core.log == [] and r.outcome == "failed" and r.changed is False


async def test_commit_401_session_not_verified_keeps_the_pending_and_asks_for_verification() -> None:
    for card, key in ((COMMIT_CARD, "commit"), (SUBMIT_CARD, "submit")):
        core = Core(**{key: [err(401, "SESSION_NOT_VERIFIED", changed=False)]})
        c, control, tickets = make(core)
        r = await c.confirm(control, card)
        assert (r.outcome, r.changed, r.end_reason) == ("failed", False, "verification_requested")
        assert r.needs_verification and r.keep_pending and not r.repeat_readback and tickets.reasons == []
        assert core.paths() == [f"POST {key}"]  # no status check, no retry


async def test_commit_200_without_summary_still_reports_committed() -> None:
    c, control, _ = make(Core(commit=[httpx.Response(200, json={"pending_action_id": PID, "replayed": False})]))
    r = await c.confirm(control, COMMIT_CARD)
    assert (r.outcome, r.changed, r.text) == ("committed", True, "That went through.")


async def test_cancel_timeout_does_not_say_nothing_changed() -> None:
    c, control, _ = make(Core(cancel=[TIMEOUT]))
    r = await c.cancel(control, COMMIT_CARD)
    assert r.changed is None and "nothing has been changed" not in r.text.lower()
