"""What: agent tests for CS-E-02.F14 (build-log/reports/rca-pin-overmask.md): masking follows provenance, never our
own records. The Gate 2 verifier receives the verified account context with its numbers, and the trace of that
call keeps them; the PIN is never in our records (verification sends it to core only); a PIN the customer types is
still masked before the model and in the trace.
Use when: pytest (fakes, no network).
Inputs: none. Output: pass or fail. Side effects: none.
Role: pins the trace 01a22039-11d3 symptom ("U-[PIN]", "ticket ending [PIN]" in the verifier input) and the D-031
floor next to it."""
from __future__ import annotations

import json
from typing import Any

from contracts.redact import PIN_TOKEN, hide_for_langsmith
from contracts.stubs import examples as ex
from langchain_core.load import dumpd
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pin_probe import leaks
from test_structured_input import World

from agent.gates import GroundingVerifier, session_evidence
from agent.line_ids import INGRESS, code_line
from agent.middleware.router import VERIFY_LINE
from agent.structured.verification import VERIFIED_THREAD_LINE
from agent.turn_control import TurnControl

VERIFIED = {"verified": True, "claims_snapshot": ex.CLAIMS.model_dump(mode="json"),
            "context_snapshot": ex.ME_CONTEXT.model_dump(mode="json")}
DRAFT = "Your billing ticket ending 8213 about the invoice ending 0412 is open."


class RecordingVerifier:
    """What: a verifier model fake that records the messages it is sent and answers one supported claim.
    Use when: these tests. Inputs: messages. Output: AIMessage. Side effects: records. Role: fake (RC-3: the input
    as sent)."""

    model = "fake-gate"

    def __init__(self) -> None:
        """What: empty record. Use when: tests. Inputs: none. Output: none. Side effects: none. Role: constructor."""
        self.sent: list[list[BaseMessage]] = []

    async def ainvoke(self, messages: list[BaseMessage], config: Any = None, **_: Any) -> AIMessage:
        """What: record and answer. Use when: the verifier calls the model. Inputs: messages. Output: AIMessage.
        Side effects: records. Role: fake call."""
        self.sent.append(list(messages))
        verdict = {"claims": [{"text": DRAFT, "supported": True, "source_id": "account_context"}],
                   "commitment": False, "no_claims_reason": None}
        return AIMessage(json.dumps(verdict), response_metadata={"stop_reason": "end_turn"})


async def _verify() -> tuple[str, str, Any]:
    """What: run Gate 2's verifier once on the verified stub context. Use when: these tests. Inputs: none.
    Output: (the human message as sent, its trace copy, the result). Side effects: none. Role: helper."""
    model = RecordingVerifier()
    thread = [HumanMessage("<customer_message>\nwhat is open?\n</customer_message>"),
              code_line(INGRESS, "019a7c2e-5b20-7000-8000-0000000000d1", VERIFY_LINE, None),  # names the PIN
              HumanMessage(VERIFIED_THREAD_LINE)]
    evidence = session_evidence(VERIFIED, thread)
    result = await GroundingVerifier(model, timeout_s=1.0, min_s=0.0).verify(  # type: ignore[arg-type]
        TurnControl(session_id="s", turn_id="t"), DRAFT, evidence)
    (msgs,) = model.sent
    sent = str(msgs[-1].content)
    traced = json.dumps(hide_for_langsmith({"messages": [[dumpd(m) for m in msgs]]}))  # the llm run's inputs
    return sent, traced, result


async def test_the_verifier_receives_our_numbers_as_sent() -> None:
    sent, _, result = await _verify()
    assert "support PIN" in sent  # the evidence names the PIN (our verify line), as the real trace's article did
    assert not [s for s in ("U-0001", "ticket ending 8213", "invoice ending 0412") if s not in sent]
    assert PIN_TOKEN not in sent
    assert result.verdict == "pass"


async def test_the_trace_of_the_verifier_call_keeps_our_numbers() -> None:
    _, traced, _ = await _verify()
    assert not [s for s in ("U-0001", "ticket ending 8213", "invoice ending 0412") if s not in traced]
    assert PIN_TOKEN not in traced


async def test_card_verification_never_puts_the_pin_in_our_records() -> None:
    w = World([AIMessage("Thanks Priya, your ticket ending 8213 is open.")], verified=False)
    body = {"account_id": "QS-10423", "pin": ex.STUB_PIN}
    assert leaks(json.dumps(body), ex.STUB_PIN)  # positive control: the probe finds the PIN where it is
    outcome, events = await w.send(verification=body)
    values = await w.values()
    records = json.dumps({"claims": values["claims_snapshot"], "context": values["context_snapshot"],
                          "messages": [str(m.content) for m in values["messages"]],
                          "events": [e.model_dump(mode="json") for e in events],
                          "final": outcome.final.model_dump(mode="json")})
    assert values["verified"] is True and "ticket ending 8213" in records
    assert not leaks(records, ex.STUB_PIN)


async def test_positive_control_a_typed_pin_never_reaches_the_model_or_the_trace() -> None:
    from dataclasses import replace

    from test_structured_input import SID, Fake

    # A PIN typed with its cue is refused at step 9a: no model call, and nothing we send or keep holds it.
    w = World([AIMessage("unused")], verified=True)
    await w.seed(VERIFIED)
    outcome, events = await w.send(text=f"my PIN is {ex.STUB_PIN}, what about ticket ending 8213?")
    assert outcome.end_reason == "verification_requested" and Fake.seen == []
    sent = json.dumps({"final": outcome.final.model_dump(mode="json"),
                       "events": [e.model_dump(mode="json") for e in events]})
    assert not leaks(sent, ex.STUB_PIN) and not leaks(json.dumps(hide_for_langsmith(json.loads(sent))), ex.STUB_PIN)
    # A number while the card waits is masked at step 9a and reaches the model only as [PIN], in its frame.
    w = World([AIMessage("Please use the secure card.")], verified=False)
    w.store.sessions[SID] = replace(w.store.sessions[SID], awaiting_verification=True)
    await w.send(text=f"I think it is {ex.STUB_PIN} for the March invoice")
    (human,) = [m for m in Fake.seen[-1] if isinstance(m, HumanMessage)][-1:]
    assert f"I think it is {PIN_TOKEN} for the March invoice" in str(human.content)
    traced = json.dumps(hide_for_langsmith({"messages": [[dumpd(m) for m in Fake.seen[-1]]]}))
    state = json.dumps(hide_for_langsmith({"messages": [dumpd(m) for m in (await w.values())["messages"]]}))
    for text in (str(human.content), traced, state):
        assert not leaks(text, ex.STUB_PIN)
