"""What: tests for ingress step 9a (doc 07 s2.4 9a, s7.3 typed-PIN refusal, s11, s11.1): the PIN scan on chat text,
voice question and user_transcript before the thread, the model or any hook sees them; the refusal with the fixed
line and verification.required reason typed_pin_rejected; the redacting log formatter; the LangSmith client hide
functions.
Use when: pytest (in-process ASGI over the composed Ingress and the real create_agent graph with fake models).
Inputs: none. Output: pass or fail. Side effects: none (the LangSmith test captures requests in memory).
Role: CS-E-03.S7.T9 unit evidence; the Postgres sink test is tests/pg/test_pin_sinks_pg.py."""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import replace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent import composition
from agent.assembly import run_config
from agent.pin_guard import TYPED_PIN_LINE
from agent.structured.confirm import CoreCommitClient
from agent.voice import TranscriptMerger, VoiceSessionRow
from test_turns_route import SID, World, events_of

RAW = "QS-10423 4821"
PIN = "4821"


class Flags:
    """What: fake agent.sessions flag writer that also updates the memory session row.
    Use when: these tests. Inputs: the World. Output: set_awaiting_verification. Side effects: mutates the row.
    Role: fake of PgSessionFlags."""

    def __init__(self, w: World) -> None:
        """What: keep the world. Use when: tests. Inputs: world. Output: none. Side effects: none. Role: fake."""
        self.w, self.calls = w, []

    async def set_awaiting_verification(self, session_id: str, awaiting: bool,
                                        verified_user_id: str | None = None) -> None:
        """What: record and apply. Use when: the refusal. Inputs: id, flag. Output: none. Side effects: row.
        Role: fake."""
        self.calls.append((session_id, awaiting))
        s = self.w.turns.sessions[session_id]
        self.w.turns.sessions[session_id] = replace(s, awaiting_verification=awaiting)


def guarded(*replies: AIMessage) -> tuple[World, Flags]:
    """What: a World whose Ingress has the awaiting-verification writer. Use when: every test here.
    Inputs: scripted replies. Output: world and flags. Side effects: none. Role: harness."""
    w = World(*replies)
    flags = Flags(w)
    w.ingress = composition.build_ingress(w.runtime, store=w.turns, lock=w.lock, served_by="agent_a",
                                          call_keys=w.keys, merge_transcript=TranscriptMerger(w.vstore), flags=flags,
                                          commit=CoreCommitClient(w.cfg.core_url, "commit-tok", http=w.http))
    w.app.state.ingress = w.ingress
    w.voice.ingress = w.ingress
    return w, flags


async def thread_text(w: World) -> str:
    """What: everything the checkpointed thread holds, as text. Use when: assertions. Inputs: world.
    Output: repr of the state values. Side effects: one read. Role: helper."""
    state = await w.runtime.agent.aget_state(run_config(SID))
    return repr(state.values)


def model_text(w: World) -> str:
    """What: everything the fake models were sent. Use when: assertions. Inputs: world. Output: text.
    Side effects: none. Role: helper."""
    return repr(w.registry.primary.seen)


async def chat(w: World, text: str) -> list[Any]:
    """What: one chat turn. Use when: tests. Inputs: world, text. Output: events. Side effects: a turn.
    Role: helper."""
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/turns", json={"client_turn_id": str(uuid.uuid4()), "text": text})
    assert r.status_code == 200, r.text
    return events_of(r)


async def test_first_turn_account_and_pin_is_refused_with_the_fixed_line() -> None:
    w, flags = guarded(AIMessage(content="should never be called"))
    assert PIN in RAW  # the secret is in the fixture
    evs = await chat(w, RAW)
    types = [e.type for e in evs]
    (vr,) = [e for e in evs if e.type == "verification.required"]
    assert vr.reason == "typed_pin_rejected" and vr.prompt_text == TYPED_PIN_LINE
    assert vr.fields == ["account_id", "pin"]
    done = evs[-1]
    assert types[0] == "turn.started" and done.type == "turn.completed"
    assert done.final.text == TYPED_PIN_LINE and done.end_reason == "verification_requested"
    assert done.usage.model_calls == 0 and w.registry.primary.seen == []
    assert flags.calls == [(SID, True)] and w.turns.sessions[SID].awaiting_verification is True
    thread = await thread_text(w)
    assert "QS-10423 [PIN]" in thread and TYPED_PIN_LINE in thread and PIN not in thread
    assert PIN not in repr([e.model_dump() for e in evs])


@pytest.mark.parametrize("text", ["4821 is my PIN", "my pin is 4821"])
async def test_pin_word_messages_are_refused_even_when_not_awaiting(text: str) -> None:
    w, _ = guarded(AIMessage(content="never"))
    evs = await chat(w, text)
    assert evs[-1].final.text == TYPED_PIN_LINE and w.registry.primary.seen == []
    assert PIN not in await thread_text(w)


async def test_bare_pin_while_awaiting_is_refused() -> None:
    w, _ = guarded(AIMessage(content="never"))
    w.turns.sessions[SID] = replace(w.turns.sessions[SID], awaiting_verification=True)
    evs = await chat(w, "4821")
    assert evs[-1].final.text == TYPED_PIN_LINE and PIN not in await thread_text(w)


async def test_redacted_but_not_credential_looking_text_goes_to_the_model_redacted() -> None:
    w, flags = guarded(AIMessage(content="Let me check that invoice."))
    w.turns.sessions[SID] = replace(w.turns.sessions[SID], awaiting_verification=True)
    evs = await chat(w, "invoice ending 0412")
    assert evs[-1].final.text == "Let me check that invoice." and flags.calls == []
    assert "invoice ending [PIN]" in model_text(w) and "0412" not in model_text(w)
    assert "0412" not in await thread_text(w)


async def test_verified_invoice_ending_is_kept() -> None:
    w, _ = guarded(AIMessage(content="Looking."))
    w.turns.sessions[SID] = replace(w.turns.sessions[SID], verified_user_id="user-1")
    await chat(w, "invoice ending 0412")
    assert "invoice ending 0412" in model_text(w)


async def test_voice_question_and_transcript_are_redacted_before_the_model_and_storage() -> None:
    w, flags = guarded(AIMessage(content="Please use the card on screen."))
    w.vstore.voice["vs-1"] = VoiceSessionRow("vs-1", SID, w.voice.now(), w.voice.now())
    said = "Q S one zero four two three, four eight two one"
    body = {"client_turn_id": str(uuid.uuid4()), "voice_session_id": "vs-1", "realtime_call_id": "call_1",
            "question": f"Customer gave {RAW}", "user_item_id": "item_u1", "user_transcript": said,
            "transcript_final": True, "last_assistant_interrupted": False}
    async with w.client() as c:
        r = await c.post(f"/api/sessions/{SID}/voice/ask", json=body)
    evs = events_of(r)
    assert evs[-1].type == "turn.completed" and evs[-1].final.say == "Please use the card on screen."
    assert flags.calls == []  # voice is not refused (rule V is designed only); it is redacted
    seen = model_text(w)
    assert "QS-10423 [PIN]" in seen and PIN not in seen and "four eight two one" not in seen
    assert PIN not in await thread_text(w)
    (item,) = [i for i in w.vstore.items if i.realtime_item_id == "item_u1"]
    assert PIN not in item.text and "[PIN]" in item.text


def test_log_formatter_redacts_pins_and_emails() -> None:
    from agent.logs import RedactingFormatter
    rec = logging.LogRecord("agent.x", logging.INFO, __file__, 1, "text=%s mail=%s", (RAW, "priya@acme.com"), None)
    assert PIN in rec.getMessage()  # the secret is in the record
    out = RedactingFormatter("%(name)s %(message)s").format(rec)
    assert out == "agent.x text=QS-10423 [PIN] mail=p***@acme.com"


def test_install_log_redaction_covers_the_root_handlers() -> None:
    from agent.logs import RedactingFormatter, install_log_redaction
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        install_log_redaction()
        assert root.handlers and all(isinstance(h.formatter, RedactingFormatter) for h in root.handlers)
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)


def test_langsmith_client_hides_pins_in_the_posted_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests
    from langchain_core.runnables import RunnableLambda

    from agent.tracing import build_langsmith_client

    sent: list[bytes] = []

    class Capture(requests.Session):
        """What: a requests session that records bodies and answers 200. Use when: this test.
        Inputs: none. Output: session. Side effects: records. Role: fake LangSmith endpoint."""

        def request(self, method: str | bytes, url: str | bytes, *a: Any, **kw: Any) -> requests.Response:  # type: ignore[override]
            """What: record the body. Use when: the client posts. Inputs: request. Output: 200.
            Side effects: records. Role: fake."""
            body = kw.get("data")
            sent.append(body if isinstance(body, bytes) else str(body).encode())
            resp = requests.Response()
            resp.status_code, resp._content = 200, b"{}"
            return resp

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    client = build_langsmith_client(api_key="lsv2_test_not_real", api_url="http://langsmith.test", session=Capture(),
                                    auto_batch_tracing=False)
    from langchain_core.tracers.langchain import LangChainTracer
    def echo(x: dict[str, str]) -> str:
        """What: a traced step. Use when: this test. Inputs: dict. Output: text. Side effects: none. Role: fixture."""
        return f"echo {x['text']}"

    chain = RunnableLambda(echo)
    chain.invoke({"text": RAW}, config={"callbacks": [LangChainTracer(client=client)]})
    client.flush()
    blob = b"".join(sent)
    assert blob, "nothing was posted, so the search below would prove nothing"
    assert b"QS-10423" in blob  # the payload carries the message (the account id is kept)
    standalone = re.compile(rb"(?<![0-9A-Za-z-])4821(?![0-9A-Za-z])")  # not inside a uuid or a timestamp
    assert standalone.search(RAW.encode())  # the search can find the raw secret
    assert not standalone.search(blob)


def test_configure_tracing_sets_the_global_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from langchain_core.tracers.langchain import get_client

    from agent.tracing import configure_tracing

    from langsmith import run_trees

    previous = run_trees._CLIENT  # noqa: SLF001 - restored below so no other test traces with this client
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_not_real")
    try:
        client = configure_tracing()
        assert client is not None and get_client() is client
        assert client._hide_run_inputs({"q": "my PIN is 4821"}) == {"q": "my PIN is [PIN]"}  # noqa: SLF001
        assert client._hide_run_outputs({"a": RAW}) == {"a": "QS-10423 [PIN]"}  # noqa: SLF001
    finally:
        run_trees.configure(client=previous)


# ---- fix round 2 (rereview-pr57-r1: N-I1 crash, N-I2 over-refusal, N-I3 split PINs) ----

async def test_glued_tens_words_do_not_crash_the_turn() -> None:
    w, _ = guarded(AIMessage(content="Pro is priced per seat."))
    evs = await chat(w, "we have about twentyfive, 30 seats on the Pro plan, what does it cost?")
    assert evs[-1].end_reason == "answered" and evs[-1].final.text == "Pro is priced per seat."


async def test_verified_refusal_does_not_put_the_session_back_to_awaiting() -> None:
    w, flags = guarded(AIMessage(content="never"))
    w.turns.sessions[SID] = replace(w.turns.sessions[SID], verified_user_id="user-1")
    evs = await chat(w, "my PIN is 4821")
    assert evs[-1].final.text == TYPED_PIN_LINE and flags.calls == []
    assert w.turns.sessions[SID].awaiting_verification is False


async def test_verified_ordinary_message_with_account_and_invoice_goes_to_the_model() -> None:
    w, flags = guarded(AIMessage(content="Yes, it went through."))
    w.turns.sessions[SID] = replace(w.turns.sessions[SID], verified_user_id="user-1")
    evs = await chat(w, "did the refund for QS-10423 go through, the one for invoice 0412")
    assert evs[-1].final.text == "Yes, it went through." and flags.calls == []
    assert "invoice 0412" in model_text(w)


@pytest.mark.parametrize(("first", "second"), [
    ("my PIN is", "4821 by the way, which plan am I on?"),  # N18
    ("I am QS-10423", "and the number is 4821 if you need it"),  # N20
])
async def test_a_pin_split_over_two_messages_never_reaches_the_model(first: str, second: str) -> None:
    w, flags = guarded(AIMessage(content="Noted."), AIMessage(content="Noted again."))
    await chat(w, first)
    assert (SID, True) in flags.calls and w.turns.sessions[SID].awaiting_verification is True
    await chat(w, second)
    assert PIN not in model_text(w) and PIN not in await thread_text(w)


async def test_the_awaiting_flag_clears_when_the_topic_moves_on() -> None:
    w, flags = guarded(AIMessage(content="We have Starter, Pro and Enterprise."))
    w.turns.sessions[SID] = replace(w.turns.sessions[SID], awaiting_verification=True)
    await chat(w, "what plans do you offer for a team of twelve?")
    assert flags.calls == [(SID, False)] and w.turns.sessions[SID].awaiting_verification is False


async def test_a_short_ack_keeps_the_awaiting_flag() -> None:
    w, flags = guarded(AIMessage(content="Take your time."))
    w.turns.sessions[SID] = replace(w.turns.sessions[SID], awaiting_verification=True)
    await chat(w, "ok one sec")
    assert flags.calls == [] and w.turns.sessions[SID].awaiting_verification is True


def test_log_formatter_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.logs as logs

    def boom(text: str, ctx: object) -> str:
        """What: a failing redactor. Use when: this test. Inputs: text, ctx. Output: none. Side effects: raises.
        Role: fixture."""
        raise RuntimeError("x")

    monkeypatch.setattr(logs, "scan_text", boom)
    rec = logging.LogRecord("agent.x", logging.INFO, __file__, 1, "text=%s", (RAW,), None)
    out = logs.RedactingFormatter("%(message)s").format(rec)
    assert PIN not in out and "redaction failed" in out
