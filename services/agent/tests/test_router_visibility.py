"""What: tests for RouterMiddleware and ToolVisibilityMiddleware (CS-E-03.S8.T4; doc 07 s2.5 item 1, s5.2,
s6.3 step 3, s6.5).
Use when: pytest (fake models and fake tools; no provider, no network).
Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when: each route ends the run with its fixed line and end_reason; the tool list per role matches
doc 07 s5.2 (minus the designed-only tools, doc 09 s1.2)."""
from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.assembly import build_agent, run_config
from agent.middleware import router as router_mod
from agent.middleware.router import RouterMiddleware, route_reply
from agent.middleware.visibility import visibility_set, visible_tool_names
from agent.sse import TurnMeta, TurnOutcome, TurnStream, start_turn, stream_agent_turn
from agent.turn_control import Route, TurnControl, bind_turn_control, require_control
from contracts.events import KEEPALIVE, parse_event

SID, TID = "019a7c2e-5b10-7000-8000-00000000a001", "019a7c2e-5b20-7000-8000-0000000000c1"

# doc 07 s5.2, copied from the contract, and the designed-only tools of doc 09 s1.2.
S52 = {
    "V0": ["create_ticket", "request_verification", "search_kb"],
    "V1": ["create_ticket", "get_account_summary", "list_tickets", "prepare_billing_email_change",
           "prepare_grace_extension", "prepare_refund", "search_kb", "update_notification_preferences"],
}
S52["V2"] = sorted(S52["V1"] + ["list_charges"])
DESIGNED_ONLY = {"prepare_billing_email_change", "prepare_grace_extension", "update_notification_preferences"}

STATES = {
    "V0": {"verified": False},
    "V1": {"verified": True, "claims_snapshot": {"role": "member", "tier": "pro"}},
    "V2": {"verified": True, "claims_snapshot": {"role": "owner", "tier": "pro"}},
}


class Recorder(GenericFakeChatModel):
    """What: scripted fake model that records the tool names bound for each call. Use when: tests.
    Inputs: messages. Output: scripted replies. Side effects: appends to `bound`. Role: test double."""

    bound: Any = None

    def bind_tools(self, tools: Any, **kw: Any) -> "Recorder":
        """What: record the names. Use when: each model call. Inputs: tools. Output: self.
        Side effects: appends. Role: fake plumbing."""
        self.bound.append([getattr(t, "name", None) or t.get("name") for t in tools])
        return self


def recorder(*replies: AIMessage) -> Recorder:
    """What: build a Recorder (non-streaming). Use when: tests. Inputs: replies. Output: Recorder.
    Side effects: none. Role: helper."""
    return Recorder(messages=iter(replies), bound=[], disable_streaming=True)


def _fake_tool(name: str, effect: Any = None) -> Any:
    """What: an async fake tool with a contract tool name that runs `effect(control)` and returns "ok".
    Use when: tests. Inputs: name, effect. Output: BaseTool. Side effects: none. Role: helper."""

    async def run(**kwargs: Any) -> str:
        """What: apply the effect to the bound TurnControl. Use when: the fake tool runs. Inputs: any args.
        Output: "ok". Side effects: mutates the control. Role: fake."""
        if effect is not None:
            effect(require_control())
        return "ok"

    run.__name__ = name
    return tool(name, description=f"fake {name}")(run)  # pyright: ignore[reportCallIssue, reportArgumentType]


ALL_NAMES = sorted(set(S52["V2"]) | set(S52["V0"]))


def all_fake_tools(**effects: Any) -> list[Any]:
    """What: one fake per s5.2 name (designed-only names included, to prove they stay hidden).
    Use when: tests. Inputs: per-name effects. Output: tools. Side effects: none. Role: helper."""
    return [_fake_tool(n, effects.get(n)) for n in ALL_NAMES]


def call(name: str, call_id: str = "c1", **args: Any) -> AIMessage:
    """What: an AI message with one tool call. Use when: scripting. Inputs: name, id, args. Output: AIMessage.
    Side effects: none. Role: helper."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


async def run_turn(agent: Any, state: dict[str, Any], mode: str = "chat") -> tuple[list[Any], TurnOutcome]:
    """What: one turn through start_turn and stream_agent_turn. Use when: tests. Inputs: agent, extra state,
    mode. Output: (events, outcome). Side effects: none. Role: helper."""
    stream = TurnStream(TurnMeta(session_id=SID, turn_id=TID, mode=mode, served_by="agent_a", input_kind="text"))

    async def body(s: TurnStream) -> TurnOutcome:
        """What: agent body. Use when: start_turn. Inputs: stream. Output: outcome. Side effects: none. Role: helper."""
        return await stream_agent_turn(s, agent, {"messages": [HumanMessage("hi")], **state}, run_config(SID))

    async def write_final(_: TurnOutcome) -> None:
        """What: no-op final row. Use when: start_turn. Inputs: outcome. Output: none. Side effects: none. Role: fake."""

    task = start_turn(stream, body, write_final=write_final)
    frames = [f async for f in stream.frames()]
    outcome = await task
    events = [parse_event(f.split("\n")[2][len("data: "):]) for f in frames if f != KEEPALIVE]
    return events, outcome


# --- visibility --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("vset", ["V0", "V1", "V2"])
async def test_tool_list_per_role_matches_doc07_s5_2(vset: str):
    model = recorder(AIMessage(content="hello"))
    agent = build_agent(model, fallback_model=recorder(), checkpointer=InMemorySaver(), tools=all_fake_tools())
    await run_turn(agent, STATES[vset])
    expected = sorted(n for n in S52[vset] if n not in DESIGNED_ONLY)
    assert model.bound == [expected]
    assert list(visible_tool_names(STATES[vset])) == expected


def test_grace_and_other_designed_only_tools_are_never_visible():
    for state in STATES.values():
        assert not DESIGNED_ONLY & set(visible_tool_names(state))


def test_visibility_set_rules():
    assert visibility_set({}) == "V0"
    assert visibility_set({"verified": False, "claims_snapshot": {"role": "owner"}}) == "V0"
    assert visibility_set({"verified": True, "claims_snapshot": {"role": "billing_admin"}}) == "V2"
    assert visibility_set({"verified": True, "claims_snapshot": None}) == "V1"  # unknown role: smaller set


async def test_visibility_is_recomputed_before_every_model_call():
    # The state changes between calls only through a new turn here; two turns, two different sets.
    model = recorder(AIMessage(content="a"), AIMessage(content="b"))
    agent = build_agent(model, fallback_model=recorder(), checkpointer=InMemorySaver(), tools=all_fake_tools())
    await run_turn(agent, STATES["V0"])
    await run_turn(agent, STATES["V2"])
    assert model.bound == [sorted(S52["V0"]), sorted(set(S52["V2"]) - DESIGNED_ONLY)]


# --- routes ------------------------------------------------------------------------------------------------

READBACK = {"chat": "I'll refund $96.00 for the charge on 21 November. Shall I go ahead?",
            "voice": "I'll refund ninety-six dollars for the charge on the twenty-first of November. Shall I go ahead?"}


def _readback(c: TurnControl) -> None:
    """What: effect of a fake allowed prepare. Use when: readback route test. Inputs: control. Output: none.
    Side effects: sets the route. Role: helper."""
    c.set_route(Route("readback", readback=READBACK, pending={"id": "PA-3F2A9C1B", "action_type": "refund"}))


ROUTES = [
    ("verify", "request_verification", {"reason": "action"}, "V0",
     lambda c: c.set_route(Route("verify", reason="action")), router_mod.VERIFY_LINE, "verification_requested"),
    ("readback", "prepare_refund", {"reason": "duplicate", "note": "x"}, "V1", _readback, READBACK["chat"], "readback"),
    ("escalate", "search_kb", {"query": "sso setup"}, "V0",
     lambda c: c.set_route(Route("escalate", reason="not_in_docs")),
     "Our help articles don't cover that. I couldn't open a ticket just now. If you contact us, please quote "
     "reference 00c1.", "escalated"),
    ("decline_off_topic", "search_kb", {"query": "weather today"}, "V0",
     lambda c: c.set_route(Route("decline_off_topic")), router_mod.DECLINE_OFF_TOPIC_LINE, "declined_off_topic"),
]


@pytest.mark.parametrize("kind,tool_name,args,vset,effect,line,end_reason", ROUTES, ids=[r[0] for r in ROUTES])
async def test_each_route_ends_the_run_with_its_fixed_line_and_end_reason(kind, tool_name, args, vset, effect,
                                                                         line, end_reason):
    model = recorder(call(tool_name, **args), AIMessage(content="MODEL MUST NOT RUN AGAIN"))
    saver = InMemorySaver()
    agent = build_agent(model, fallback_model=recorder(), checkpointer=saver,
                        tools=all_fake_tools(**{tool_name: effect}))
    events, outcome = await run_turn(agent, STATES[vset])
    assert len(model.bound) == 1  # the router ended the turn before a second model call
    assert outcome.end_reason == end_reason and outcome.final.text == line
    assert not [e for e in events if e.type == "token"]  # deterministic lines are never model tokens
    state = await agent.aget_state(run_config(SID))
    last = state.values["messages"][-1]
    assert isinstance(last, AIMessage) and last.content == line and last.id == f"route-{TID}"
    done = events[-1]
    assert done.type == "turn.completed" and done.end_reason == end_reason
    assert done.final.escalated is (kind == "escalate")
    if kind == "verify":
        vr = [e for e in events if e.type == "verification.required"]
        assert len(vr) == 1 and vr[0].reason == "action" and vr[0].prompt_text == line
        assert vr[0].fields == ["account_id", "pin"] and vr[0].attempts_left == 3
    else:
        assert not [e for e in events if e.type == "verification.required"]
    if kind == "readback":
        assert done.final.say == READBACK["voice"]
        assert state.values["pending"] == {"id": "PA-3F2A9C1B", "kind": "refund", "readback_turn_id": TID,
                                           "readback_interrupted": False}


async def test_voice_verify_writes_the_spoken_line_and_say():
    model = recorder(call("request_verification", reason="account_lookup"))
    agent = build_agent(model, fallback_model=recorder(), checkpointer=InMemorySaver(),
                        tools=all_fake_tools(request_verification=lambda c: c.set_route(
                            Route("verify", reason="account_lookup"))))
    events, outcome = await run_turn(agent, STATES["V0"], mode="voice")
    assert outcome.final.say == router_mod.VERIFY_SAY and outcome.final.text == router_mod.VERIFY_LINE
    assert outcome.end_reason == "verification_requested"


async def test_no_route_means_the_router_passes_through():
    control = TurnControl(session_id=SID, turn_id=TID)
    bind_turn_control(control)
    assert await RouterMiddleware().abefore_model({"messages": []}, None) is None


async def test_escalation_uses_the_ticket_from_the_hook_when_there_is_one():
    ticket = {"ticket_id": "TCK-26118213", "short_id": "8213", "display": "ticket ending 8213", "stage": "open",
              "priority": "P3", "queue": "billing", "target_label": "invoice ending 0412", "verified": True}
    seen: list[str | None] = []

    async def open_ticket(c: TurnControl) -> dict[str, Any]:
        """What: fake escalation hook. Use when: this test. Inputs: control. Output: ticket.
        Side effects: records the reason. Role: fake."""
        seen.append(c.route.reason if c.route else None)
        return ticket

    control = TurnControl(session_id=SID, turn_id=TID)
    control.set_route(Route("escalate", reason="tool_failure", tool="prepare_refund"))
    bind_turn_control(control)
    out = await RouterMiddleware(open_ticket=open_ticket).abefore_model({"messages": []}, None)
    assert out is not None and out["jump_to"] == "end" and seen == ["tool_failure"]
    assert out["messages"][0].content == ("Our billing system didn't respond, so nothing was changed. "
                                          "I've opened a ticket for our team, ticket ending 8213.")
    reply = route_reply(control)
    assert reply.escalated and reply.ticket == ticket and reply.end_reason == "escalated"


def test_retrieval_unavailable_line_never_says_not_in_docs():
    control = TurnControl(session_id=SID, turn_id=TID)
    control.set_route(Route("escalate", reason="retrieval_unavailable"))
    text = route_reply(control).text
    assert text.startswith("I couldn't search our help articles just now, so I won't guess.")
    assert "don't cover" not in text


def test_first_route_wins():
    control = TurnControl(session_id=SID, turn_id=TID)
    assert control.set_route(Route("readback", readback=READBACK))
    assert not control.set_route(Route("escalate", reason="tool_failure"))
    assert control.route is not None and control.route.kind == "readback"
