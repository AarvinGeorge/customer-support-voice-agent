"""What: tests for ingress steps 10 and 11 (doc 07 s2.4, s2.6, s5.3): dangling tool-call repair with not_run
tool messages described as unknown, and the memory refresh from core GET /v1/me/context (2 s, one retry, stale
marker on failure, a 401 clears verified so the next model call sees V0).
Use when: pytest (contracts core stub over ASGI, scripted MockTransport for failures; fake model; no network).
Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when of CS-E-03.S7.T5."""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
from contracts.stubs import examples as ex
from contracts.stubs.core_stub import create_app
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.assembly import build_agent, run_config
from agent.clients.core_agent import CoreAgentClient
from agent.memory import refresh_memory, repair_dangling
from agent.middleware.visibility import visibility_set
from agent.sse import TurnMeta, TurnStream
from agent.turn_control import TurnControl

SID, TID = ex.SESSION_ID, "019a7c2e-5b20-7000-8000-0000000000d1"
OWNER = {"verified": True, "claims_snapshot": {"role": "owner", "tier": "pro"},
         "context_snapshot": {"generated_at": "old"}}


class Fake(GenericFakeChatModel):
    """What: scripted fake model. Use when: building a graph. Inputs: messages. Output: replies.
    Side effects: none. Role: test double."""

    def bind_tools(self, tools: Any, **kw: Any) -> "Fake":
        """What: no-op. Use when: create_agent binds tools. Inputs: tools. Output: self. Side effects: none.
        Role: fake plumbing."""
        return self


def agent() -> Any:
    """What: a compiled agent on an in-memory checkpointer. Use when: repair tests. Inputs: none.
    Output: graph. Side effects: none. Role: helper."""
    return build_agent(Fake(messages=iter([])), fallback_model=Fake(messages=iter([])), checkpointer=InMemorySaver())


def control(remaining_s: float | None = None) -> TurnControl:
    """What: a TurnControl with a real stream as its sink. Use when: refresh tests. Inputs: seconds left.
    Output: control. Side effects: none. Role: helper."""
    stream = TurnStream(TurnMeta(SID, TID, "chat", "agent_a", "text"))
    return TurnControl(session_id=SID, turn_id=TID, events=stream,
                       deadline=None if remaining_s is None else time.monotonic() + remaining_s)


def stub_client(verified: bool = True) -> tuple[CoreAgentClient, Any]:
    """What: a CoreAgentClient on the contracts core stub. Use when: refresh tests. Inputs: verified flag.
    Output: (client, stub app). Side effects: none. Role: helper."""
    app = create_app()
    if verified:
        app.state.verified.add(SID)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    return CoreAgentClient("http://core:8000", "agent-tok", http=http), app


def scripted(*responses: httpx.Response | Exception) -> tuple[CoreAgentClient, list[httpx.Request]]:
    """What: a CoreAgentClient whose transport plays a script. Use when: failure tests. Inputs: responses or
    exceptions. Output: (client, request log). Side effects: none. Role: helper."""
    log: list[httpx.Request] = []
    script = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        """What: next scripted outcome. Use when: each request. Inputs: request. Output: response.
        Side effects: logs. Role: fake."""
        log.append(request)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CoreAgentClient("http://core:8000", "agent-tok", http=http), log


def envelope(status: int, code: str, retryable: bool = False) -> httpx.Response:
    """What: a doc 07 s1.3 error response. Use when: scripts. Inputs: status, code, retryable.
    Output: response. Side effects: none. Role: helper."""
    return httpx.Response(status, json={"error": {"class": "core_unavailable", "code": code, "message": "m",
                                                  "retryable": retryable, "changed": False,
                                                  "fault_injected": False, "details": {}}})


# --- step 10: repair -----------------------------------------------------------------------------------------

async def test_interrupted_thread_gets_not_run_tool_messages_described_as_unknown() -> None:
    a = agent()
    cfg = run_config(SID)
    calls = [{"name": "list_charges", "args": {}, "id": "c1", "type": "tool_call"},
             {"name": "list_tickets", "args": {}, "id": "c2", "type": "tool_call"}]
    await a.aupdate_state(cfg, {"messages": [HumanMessage("hi"), AIMessage(content="", tool_calls=calls, id="ai1"),
                                             ToolMessage(content="ok", tool_call_id="c1", name="list_charges")]})
    stream = TurnStream(TurnMeta(SID, TID, "chat", "agent_a", "text"))
    repaired = await repair_dangling(a, cfg, events=stream)
    assert repaired == 1
    msgs = (await a.aget_state(cfg)).values["messages"]
    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_msgs] == ["c1", "c2"]
    fixed = tool_msgs[1]
    content = str(fixed.content)
    assert fixed.name == "list_tickets" and content.startswith('<tool_result name="list_tickets" untrusted="true">')
    payload = json.loads(content.split(">", 1)[1].rsplit("</tool_result>", 1)[0])
    assert payload["status"] == "not_run" and payload["reason"] == "turn_interrupted"
    assert payload["result"] == "unknown" and "unknown" in payload["note"] and "empty" in payload["note"]
    kinds = [(getattr(e, "type", None), getattr(e, "kind", None)) for e in stream.events]
    assert ("step.started", "repair") in kinds


async def test_repair_is_a_no_op_when_every_call_has_a_result_or_the_thread_is_empty() -> None:
    a = agent()
    cfg = run_config(SID)
    assert await repair_dangling(a, cfg) == 0
    calls = [{"name": "list_charges", "args": {}, "id": "c1", "type": "tool_call"}]
    await a.aupdate_state(cfg, {"messages": [AIMessage(content="", tool_calls=calls, id="ai1"),
                                             ToolMessage(content="ok", tool_call_id="c1", name="list_charges"),
                                             AIMessage("done", id="ai2")]})
    before = len((await a.aget_state(cfg)).values["messages"])
    assert await repair_dangling(a, cfg) == 0
    assert len((await a.aget_state(cfg)).values["messages"]) == before


# --- step 11: memory refresh ---------------------------------------------------------------------------------

async def test_refresh_stores_the_context_snapshot_and_claims_for_a_verified_session() -> None:
    core, app = stub_client()
    c = control()
    res = await refresh_memory(core, c, OWNER)
    assert not res.stale and not res.verified_cleared
    assert res.update["context_snapshot"]["claims"]["session_id"] == SID
    assert res.update["claims_snapshot"]["role"] == "owner"
    call = app.state.calls[-1]
    assert call.path == "/v1/me/context" and call.token == "agent-tok" and call.headers["x-session-id"] == SID
    assert any(e.type == "step.completed" and e.status == "ok" for e in c.events.events)  # type: ignore[union-attr]


async def test_unverified_session_makes_no_core_call() -> None:
    core, app = stub_client(verified=False)
    res = await refresh_memory(core, control(), {"verified": False})
    assert res.update == {} and not res.stale and app.state.calls == []


async def test_failure_keeps_the_previous_snapshot_marks_it_stale_after_one_retry() -> None:
    core, log = scripted(envelope(503, "TOOL_ERROR", retryable=True), httpx.ConnectError("down"))
    res = await refresh_memory(core, control(), OWNER)
    assert res.stale and res.update == {} and res.snapshot == {"generated_at": "old"}
    assert len(log) == 2  # one retry on core_unavailable (doc 07 s8.1 "Core reads, me/context")


async def test_a_4xx_other_than_401_is_not_retried_and_is_stale() -> None:
    core, log = scripted(envelope(422, "VALIDATION_ERROR"))
    res = await refresh_memory(core, control(), OWNER)
    assert res.stale and len(log) == 1


async def test_no_retry_when_the_budget_does_not_fit() -> None:
    core, log = scripted(envelope(503, "TOOL_ERROR", retryable=True), envelope(503, "TOOL_ERROR", retryable=True))
    res = await refresh_memory(core, control(remaining_s=5.0), OWNER)  # 5 - 0.2 - 3 = 1.8 < 2: no retry
    assert res.stale and len(log) == 1


async def test_no_call_when_the_clamp_is_below_the_minimum() -> None:
    core, log = scripted()
    res = await refresh_memory(core, control(remaining_s=3.5), OWNER)  # 3.5 - 3 < 1 s
    assert res.stale and log == []


async def test_core_401_clears_verified_and_the_snapshots_so_the_next_call_uses_v0() -> None:
    core, app = stub_client(verified=False)  # claims expired at core: me/context answers 401 SESSION_NOT_VERIFIED
    res = await refresh_memory(core, control(), OWNER)
    assert res.verified_cleared and res.update == {"verified": False, "claims_snapshot": None,
                                                   "context_snapshot": None}
    assert len(app.state.calls) == 1  # a 401 is never retried
    assert visibility_set({**OWNER, **res.update}) == "V0"
