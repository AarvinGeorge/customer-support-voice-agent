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


async def test_spoken_code_line_survives_in_the_transcript_while_awaiting() -> None:
    w = World()
    w.voice_session()
    w.turns.sessions[SID] = SessionRow(session_id=SID, visitor_id=VISITOR, awaiting_verification=True)
    now = datetime.now(UTC)
    turn = TurnRow(turn_id=str(uuid.uuid4()), session_id=SID, client_turn_id=str(uuid.uuid4()), mode="voice",
                   input_kind="voice", status="running", deadline_at=now, started_at=now)
    await w.turns.insert_turn(turn)
    await w.turns.finish_turn(turn.turn_id, TurnOutcome.fixed(CODE_LINE, end_reason="escalated"), now)
    body = {"voice_session_id": "vs-1", "items": items(
        ("item_a1", "assistant", CODE_SAY),                       # the realtime model speaking our line
        ("item_a2", "assistant", "Thanks. I heard 4821 from you."),  # an echo of a number we never issued
        ("item_u1", "user", "8352"))}                             # the customer typing or saying a bare number
    async with w.client() as c:
        assert (await c.post(f"/api/sessions/{SID}/transcript", json=body)).status_code == 204
    stored = {i.realtime_item_id: i.text for i in w.vstore.items}
    assert "eight three five two" in stored["item_a1"] or "8352" in stored["item_a1"]
    assert "[PIN]" not in stored["item_a1"]
    assert not leaks(stored["item_a2"], "4821") and "[PIN]" in stored["item_a2"]  # positive control: scanner runs
    assert stored["item_u1"] == "[PIN]"


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
