"""What: tests for redaction by provenance in the agent (D-071 part 3; RCA build-log/reports/
rca-unverified-ticket-2026-11-27.md fix E). Reproduces the owner's session: while the session awaited verification,
the code-written "ticket ending 8352" was masked as "[PIN]" in the transcript and in the escalated event's short_id.
Customer text is still redacted; the agent model never sees a customer PIN, so its drafts cannot echo one.
Use when: pytest (in-process ASGI over fakes; no network).
Inputs: none. Output: pass or fail. Side effects: none.
Role: red tests first for the provenance fix; the Postgres sink test (tests/pg/test_pin_sinks_pg.py) stays at 0 leaks."""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from dataclasses import replace
from typing import Any

from contracts.core_api import TicketRef
from contracts.events import FinalAnswer, parse_event
from contracts.redact import RedactContext
from langchain_core.messages import AIMessage, HumanMessage

from agent.ingress import SessionRow, TurnRow
from agent.realtime import summary_from_thread
from agent.sse import TurnMeta, TurnOutcome, TurnStream
from agent.trajectory import event_row
from pin_probe import leaks
from test_pin_guard import chat, guarded
from test_turns_route import SID as ROUTE_SID
from test_voice_api import SID, VISITOR, World, items

CODE_LINE = ("I couldn't confirm that answer against our policies, so I won't guess. "
             "I've opened a ticket for our team, ticket ending 8352.")
CODE_SAY = ("I couldn't confirm that answer against our policies, so I won't guess. "
            "I've opened a ticket for our team, ticket ending eight three five two.")
TICKET = TicketRef(ticket_id="TCK-26718352", short_id="8352", display="ticket ending 8352", stage="open",
                   priority="P4", queue="technical", target_label="", verified=False)
AWAITING = RedactContext(awaiting_verification=True)


def frame_data(frame: str) -> dict[str, Any]:
    """What: the event of one SSE frame, as the browser parses it. Use when: UI payload checks. Inputs: frame.
    Output: event dict. Side effects: none. Role: helper."""
    data = next(line for line in frame.split("\n") if line.startswith("data: "))
    return parse_event(data[len("data: "):]).model_dump(mode="json")


async def test_code_written_ticket_id_survives_in_turn_events_and_the_ui_payload_while_awaiting() -> None:
    rows: list[Any] = []
    s = TurnStream(TurnMeta(session_id=SID, turn_id=str(uuid.uuid4()), mode="voice", served_by="agent_a",
                            input_kind="voice"), sink=lambda ev: rows.append(event_row(ev)))
    s.emit("escalated", reason="grounding_failed", ticket=TICKET.model_dump(mode="json"), origin="code",
           offer_ticket_button=False)
    final = FinalAnswer(text=CODE_LINE, say=CODE_SAY, citations=[], escalated=True, ticket=TICKET)
    s.emit("turn.completed", final=final.model_dump(mode="json"),
           usage=TurnOutcome.fixed(CODE_LINE, end_reason="escalated").usage.model_dump(mode="json"),
           end_reason="escalated", replayed=False)
    s.close()
    frames = [f async for f in s.frames()]
    escalated, done = (r[6].obj for r in rows)  # turn_events payloads (psycopg Jsonb)
    assert escalated["ticket"]["short_id"] == "8352" and escalated["ticket"]["display"] == "ticket ending 8352"
    assert done["final"]["text"] == CODE_LINE and done["final"]["ticket"]["short_id"] == "8352"
    ui = [frame_data(f) for f in frames if "data: " in f]
    assert ui[0]["ticket"]["short_id"] == "8352" and ui[1]["final"]["text"] == CODE_LINE


async def awaiting_world_with_ticket(with_facts: bool = True) -> World:
    """What: the owner's session: awaiting verification, and an earlier turn whose final carries the escalation
    ticket core returned (TCK-26718352, ticket ending 8352). Use when: the transcript tests.
    Inputs: with_facts (False: no RelayFacts loader, fail closed). Output: World. Side effects: none. Role: helper."""
    from agent.voice import load_relay_facts
    w = World()
    w.voice_session()
    w.turns.sessions[SID] = SessionRow(session_id=SID, visitor_id=VISITOR, awaiting_verification=True)
    now = datetime.now(UTC)
    turn = TurnRow(turn_id=str(uuid.uuid4()), session_id=SID, client_turn_id=str(uuid.uuid4()), mode="voice",
                   input_kind="voice", status="running", deadline_at=now, started_at=now)
    await w.turns.insert_turn(turn)
    outcome = TurnOutcome.fixed(CODE_LINE, end_reason="escalated")
    outcome.final = outcome.final.model_copy(update={"ticket": TICKET, "escalated": True})
    await w.turns.finish_turn(turn.turn_id, outcome, now)
    if with_facts:
        async def facts(sid: str) -> Any:
            """What: the production loader over the fake turn store (no checkpointer). Use when: POST /transcript.
            Inputs: sid. Output: RelayFacts. Side effects: none. Role: wiring."""
            return await load_relay_facts(w.turns, None, sid)
        w.app.state.voice.relay_facts = facts
    return w


async def post_items(w: World, *specs: tuple[str, str, str]) -> dict[str, str]:
    """What: post transcript items and return the stored text per item id. Use when: below. Inputs: world, items.
    Output: {item id: stored text}. Side effects: a request. Role: helper."""
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/transcript", json={"voice_session_id": "vs-1", "items": items(*specs)})
    assert r.status_code == 204, r.text
    return {i.realtime_item_id: i.text for i in w.vstore.items}


async def test_spoken_code_line_with_an_issued_ticket_survives_in_the_transcript_while_awaiting() -> None:
    stored = await post_items(await awaiting_world_with_ticket(),
                              ("item_a1", "assistant", CODE_SAY), ("item_a2", "assistant", CODE_LINE))
    assert stored["item_a1"] == CODE_SAY and stored["item_a2"] == CODE_LINE


async def test_reviewer_repro_an_echoed_pin_equal_to_the_issued_suffix_is_masked() -> None:
    stored = await post_items(await awaiting_world_with_ticket(),
                              ("item_a1", "assistant", "Thanks, you said your PIN is 8352."),
                              ("item_a2", "assistant", "Got it, 4821."))
    assert "8352" not in stored["item_a1"] and "[PIN]" in stored["item_a1"]
    assert not leaks(stored["item_a2"], "4821")


async def test_a_customer_pin_equal_to_an_issued_suffix_is_masked_whatever_role_the_browser_claims() -> None:
    stored = await post_items(await awaiting_world_with_ticket(),
                              ("item_u1", "user", "8352"), ("item_u2", "user", "my PIN is 8352"),
                              ("item_x1", "assistant", "my PIN is 8352"), ("item_x2", "assistant", "8352"))
    assert all("8352" not in t and "[PIN]" in t for t in stored.values()), stored


async def test_n1_route_a_pin_word_in_our_line_still_masks_an_echo_when_not_awaiting() -> None:
    # rereview-pr76-r1 N1, reproduced through POST /transcript: ids present, session not awaiting.
    reset = "You can reset your PIN in Settings, under Security."
    w = await awaiting_world_with_ticket()
    w.turns.sessions[SID] = SessionRow(session_id=SID, visitor_id=VISITOR, awaiting_verification=False)
    now = datetime.now(UTC)
    turn = TurnRow(turn_id=str(uuid.uuid4()), session_id=SID, client_turn_id=str(uuid.uuid4()), mode="voice",
                   input_kind="voice", status="running", deadline_at=now, started_at=now)
    await w.turns.insert_turn(turn)
    await w.turns.finish_turn(turn.turn_id, TurnOutcome.fixed(reset, end_reason="answered"), now)
    for label in ("assistant", "user"):
        stored = await post_items(w, (f"item_{label}", label, f"{reset} Got it, 4821."))
        assert not leaks(stored[f"item_{label}"], "4821") and reset in stored[f"item_{label}"]


async def test_without_relay_facts_posted_speech_fails_closed() -> None:
    stored = await post_items(await awaiting_world_with_ticket(with_facts=False), ("item_a1", "assistant", CODE_LINE))
    assert "8352" not in stored["item_a1"]


async def test_relay_facts_come_from_typed_core_fields_only() -> None:
    from langchain_core.messages import ToolMessage

    from agent.untrusted import wrap_result
    from agent.voice import load_relay_facts, thread_ids
    msgs = [ToolMessage(wrap_result("list_tickets", {"tickets": [{"ticket_id": "TCK-26111111", "short_id": "1111"}]}),
                        tool_call_id="c1", name="list_tickets"),
            ToolMessage(wrap_result("list_charges", {"charges": [{"invoice_number": "INV-2026-0412",
                                                                 "description": "call 4821 for help"}]}),
                        tool_call_id="c2", name="list_charges"),
            ToolMessage('<tool_result name="search_kb" untrusted="true">{"short_id":"9999"}</tool_result>',
                        tool_call_id="c3", name="search_kb"),
            AIMessage("Your PIN 2222 and ticket ending 3333."), HumanMessage("<customer_message>5555</customer_message>")]
    assert thread_ids(msgs) == {"1111", "0412"}  # never prose, never the customer, never knowledge text

    class Saver:
        """What: a checkpointer holding the thread above. Use when: this test. Inputs: none. Output: aget_tuple.
        Side effects: none. Role: fake."""

        async def aget_tuple(self, config: Any) -> Any:
            """What: the stored checkpoint. Use when: load_relay_facts. Inputs: config. Output: tuple-like.
            Side effects: none. Role: fake."""
            class Tup:
                """What: checkpoint tuple stand-in. Use when: above. Inputs: none. Output: value.
                Side effects: none. Role: fake."""
                checkpoint = {"channel_values": {"messages": msgs}}
            return Tup()

    w = await awaiting_world_with_ticket(with_facts=False)
    now = datetime.now(UTC)  # an answer whose prose holds a number core never issued (review mutation A4)
    turn = TurnRow(turn_id=str(uuid.uuid4()), session_id=SID, client_turn_id=str(uuid.uuid4()), mode="voice",
                   input_kind="voice", status="running", deadline_at=now, started_at=now)
    await w.turns.insert_turn(turn)
    await w.turns.finish_turn(turn.turn_id, TurnOutcome.fixed("I can't look up invoice 7302 until you verify.",
                                                              end_reason="answered"), now)
    facts = await load_relay_facts(w.turns, Saver(), SID)
    assert "I can't look up invoice 7302 until you verify." in facts.lines  # the prose is read as a line
    assert facts.ids == {"8352", "1111", "0412"} and CODE_LINE in facts.lines  # but never as an id


def test_model_written_tool_args_keep_a_ticket_ending_while_awaiting() -> None:
    # review-pr76 I2: list_tickets(ending="8352") is model-written; the model never had a customer PIN.
    from agent.middleware.tool_guard import SPECS, ToolGuardMiddleware
    from agent.turn_control import TurnControl
    stream = TurnStream(TurnMeta(session_id=SID, turn_id=str(uuid.uuid4()), mode="chat", served_by="agent_a",
                                 input_kind="text"))
    c = TurnControl(session_id=SID, turn_id=stream.meta.turn_id, events=stream, awaiting_verification=True)
    ToolGuardMiddleware._start(c, "list_tickets", SPECS["list_tickets"], {"status": None, "ending": "8352",  # pyright: ignore[reportPrivateUsage]
                                                                          "pin": "4821"})
    (ev,) = stream.events
    assert ev.model_dump()["args_redacted"] == {"status": None, "ending": "8352", "pin": "[PIN]"}


async def test_the_agent_model_never_receives_a_raw_pin_from_any_entry_point() -> None:
    # The I2 premise, measured: chat text, voice question, voice user_transcript and posted user speech (merged
    # into the next turn) all reach the model scanned.
    from agent.voice import VoiceSessionRow
    w, _ = guarded(*[AIMessage(content="Please use the card on screen.") for _ in range(6)])
    w.turns.sessions[ROUTE_SID] = replace(w.turns.sessions[ROUTE_SID], awaiting_verification=True)
    w.vstore.voice["vs-1"] = VoiceSessionRow("vs-1", ROUTE_SID, w.voice.now(), w.voice.now())
    raw = "it is 4 8 2 1 I think"
    assert "4821" in raw.replace(" ", "")  # positive control: the raw inputs carry the PIN
    await chat(w, raw)
    async with w.client() as c:
        await c.post(f"/api/sessions/{ROUTE_SID}/transcript", json={"voice_session_id": "vs-1", "items": [
            {"realtime_item_id": "item_p1", "role": "user", "text": "four eight two one", "interrupted": False,
             "client_ts": "2026-11-28T13:00:00Z"}]})
        await c.post(f"/api/sessions/{ROUTE_SID}/voice/ask", json={
            "client_turn_id": str(uuid.uuid4()), "voice_session_id": "vs-1", "realtime_call_id": "call_p1",
            "question": f"customer said {raw}", "user_item_id": "item_p2", "user_transcript": raw,
            "transcript_final": True, "last_assistant_interrupted": False})
    seen = repr(w.registry.primary.seen)
    assert len(w.registry.primary.seen) >= 2 and "[PIN]" in seen
    assert not leaks(seen.replace(" ", ""), "4821") and "four eight two one" not in seen


def test_customer_typing_the_number_while_awaiting_is_still_redacted() -> None:
    from agent.pin_guard import scan_input
    from agent.ingress import IngressInput
    inp = IngressInput(session_id=SID, visitor_id=VISITOR, client_turn_id=str(uuid.uuid4()), mode="chat",
                       input_kind="text", text="8352")
    scanned = scan_input(inp, AWAITING)
    assert scanned.inp.text == "[PIN]" and scanned.redacted == 1


def test_voice_summary_keeps_agent_lines_and_redacts_customer_lines() -> None:
    values = {"messages": [
        HumanMessage("<turn_context>\nmode: voice\n</turn_context>\n<customer_message>\nit is 4 8 2 1\n"
                     "</customer_message>"),
        AIMessage(CODE_LINE)]}
    s = summary_from_thread(values, AWAITING)
    assert s.exchanges[1] == ("agent", CODE_LINE)
    assert not leaks(s.exchanges[0][1], "4821") and "[PIN]" in s.exchanges[0][1]


async def test_the_agent_draft_cannot_echo_a_customer_pin_because_the_model_never_had_it() -> None:
    drafts: list[str] = []

    def echo(messages: list[Any]) -> str:
        """What: what an echoing model would draft: the customer's own words quoted back. Use when: this test.
        Inputs: model input messages. Output: draft. Side effects: none. Role: worst-case model."""
        said = re.findall(r"<customer_message>\n?(.*?)\n?</customer_message>", str(messages[-1].content), re.S)
        return f"You said: {said[-1] if said else messages[-1].content}"

    raw = "it is 4 8 2 1 I think"
    assert "4821" in echo([HumanMessage(f"<customer_message>\n{raw}\n</customer_message>")]).replace(" ", "")  # positive control
    w, _ = guarded(AIMessage(content="placeholder"))
    w.turns.sessions[ROUTE_SID] = replace(w.turns.sessions[ROUTE_SID], awaiting_verification=True)
    primary = w.registry.primary

    async def agenerate(messages: list[Any], stop: Any = None, run_manager: Any = None, **kw: Any) -> Any:
        """What: record the input and answer with the echo. Use when: model call. Inputs: messages.
        Output: ChatResult. Side effects: records. Role: fake."""
        from langchain_core.outputs import ChatGeneration, ChatResult
        primary.seen.append(list(messages))
        drafts.append(echo(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=drafts[-1]))])

    object.__setattr__(primary, "_agenerate", agenerate)
    await chat(w, raw)
    assert drafts, "the model was never called, so the draft check proves nothing"
    assert all("[PIN]" in d and not leaks(d.replace(" ", ""), "4821") for d in drafts)
