"""What: CS-E-05.S1.T1 voice adjacency and yes signal 3 (doc 07 s7.3, s6.6) end to end through Ingress, the
TurnRunner, the step 12 transcript merger and the step 13 handler against the contracts core stub: a spoken yes
commits only from a final `user_transcript` with `user_item_id` set, not interrupted, no later client user item, right
after a delivered read-back; the realtime model's `question` never commits; an interrupted read-back is repeated; the
Confirm card is shown in voice too.
Use when: pytest (fake model, in-memory checkpointer, turn store, lock and voice store; ASGI stub; no network).
Inputs: none. Output: pass/fail. Side effects: none.
Role: Done when of CS-E-05.S1.T1."""
from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from contracts.agent_api import TranscriptItemIn, VoiceAskRequest
from contracts.stubs import examples as ex
from langchain_core.messages import AIMessage, HumanMessage

from agent.ingress import Ingress, IngressInput, IngressStream
from agent.structured.voice_yes import REPROMPT_LINE, final_transcript, voice_gate
from agent.voice import TranscriptMerger
from ingress_fakes import MemoryTurnLock
from test_structured_input import OWNER, PREPARE_CALL, SID, VISITOR, COMMIT_TOKEN, Fake, World, of
from voice_fakes import MemoryVoiceStore

VSID = "vs-1"
REFUND_ASK = "Please refund the duplicate $96 charge from 21 November"


class VoiceWorld(World):
    """What: the structured-input World plus the step 12 merger on an in-memory voice store, and a voice/ask sender.
    Use when: each test. Inputs: model replies. Output: helpers. Side effects: none. Role: wiring."""

    def __init__(self, replies: list[AIMessage] | None = None) -> None:
        """What: build it. Use when: tests. Inputs: replies. Output: none. Side effects: none. Role: constructor."""
        super().__init__(replies)
        self.voice_store = MemoryVoiceStore()
        self.ingress = Ingress(self.store, MemoryTurnLock(), served_by="agent_a", run_agent=self.ingress.run_agent,
                               merge_transcript=TranscriptMerger(self.voice_store))
        self.n = 0

    async def ask(self, question: str, *, transcript: str | None = None, final: bool = True,
                  item: str | None = "auto", interrupted: bool = False) -> tuple[Any, list[Any]]:
        """What: one voice/ask turn (the ask_support_agent relay). Use when: tests. Inputs: the realtime model's
        question, the user transcript, transcript_final, user_item_id ("auto" makes one), last_assistant_interrupted.
        Output: (outcome, events). Side effects: as a turn. Role: helper."""
        self.n += 1
        req = VoiceAskRequest(client_turn_id=uuid.uuid4(), voice_session_id=VSID, realtime_call_id=f"call-{self.n}",
                              question=question, user_item_id=f"item-u{self.n}" if item == "auto" else item,
                              user_transcript=transcript, transcript_final=final, last_assistant_interrupted=interrupted)
        res = await self.ingress.handle(IngressInput(session_id=SID, visitor_id=VISITOR,
                                                     client_turn_id=str(req.client_turn_id), mode="voice",
                                                     input_kind="voice", text=req.question,
                                                     realtime_call_id=req.realtime_call_id, body=req))
        assert isinstance(res, IngressStream) and res.task is not None
        _ = [f async for f in res.stream.frames()]
        return await res.task, res.stream.events

    async def post_items(self, *items: tuple[str, str, bool]) -> None:
        """What: POST /transcript of client chatter (role, text, interrupted). Use when: later-item tests.
        Inputs: items. Output: none. Side effects: voice store rows. Role: helper."""
        self.n += 1
        await self.voice_store.add_transcript_items(SID, VSID, [
            TranscriptItemIn(realtime_item_id=f"item-c{self.n}-{i}", role=role, text=text, interrupted=intr,  # type: ignore[arg-type]
                             client_ts="2026-11-28T13:00:00Z") for i, (role, text, intr) in enumerate(items)])


async def voice_readback(extra_replies: list[AIMessage] | None = None) -> tuple[VoiceWorld, list[Any], Any]:
    """What: a world after the S2 prepare turn by voice (the model calls prepare_refund; the read-back ends the turn).
    Use when: voice confirm tests. Inputs: replies after the prepare turn. Output: (world, events, outcome).
    Side effects: as turns. Role: helper."""
    w = VoiceWorld([AIMessage(content="", tool_calls=[PREPARE_CALL]), *(extra_replies or [])])
    await w.seed({"messages": [HumanMessage("hi"), AIMessage("Hello", id="a0")], **OWNER})
    outcome, events = await w.ask(REFUND_ASK, transcript=REFUND_ASK)
    return w, events, outcome


# --- pure helpers ---------------------------------------------------------------------------------------------

def test_final_transcript_needs_final_item_id_and_text() -> None:
    def req(**kw: Any) -> VoiceAskRequest:
        base: dict[str, Any] = {"client_turn_id": uuid.uuid4(), "voice_session_id": VSID, "realtime_call_id": "c",
                                "question": "the user confirms", "user_item_id": "i1", "user_transcript": "yes",
                                "transcript_final": True, "last_assistant_interrupted": False}
        return VoiceAskRequest.model_validate({**base, **kw})
    assert final_transcript(req()) == "yes"
    assert final_transcript(req(transcript_final=False)) is None
    assert final_transcript(req(user_item_id=None)) is None
    assert final_transcript(req(user_transcript=None)) is None
    assert final_transcript(req(user_transcript="   ")) is None
    assert final_transcript(object()) is None  # a chat TurnRequest body has no transcript fields: fails closed


def test_voice_gate_blocks_on_a_later_user_item_and_on_an_unknown_merge() -> None:
    req = VoiceAskRequest(client_turn_id=uuid.uuid4(), voice_session_id=VSID, realtime_call_id="c", question="yes",
                          user_item_id="i1", user_transcript="yes", transcript_final=True,
                          last_assistant_interrupted=False)
    assert voice_gate(req, "yes", False, None).blocked is False
    assert voice_gate(req, "yes", True, None).blocked is True
    assert voice_gate(req, "yes", None, None).blocked is True  # merge failed or not wired: unknown fails closed


# --- Done when ------------------------------------------------------------------------------------------------

async def test_voice_readback_turn_shows_the_confirm_card() -> None:
    _, events, outcome = await voice_readback()
    assert outcome.end_reason == "readback" and outcome.final.say == ex.READBACK.voice
    (conf,) = of(events, "confirmation.required")
    assert conf.pending.pending_action_id == ex.PENDING_ID and conf.confirm_label == "Confirm refund"


async def test_clean_final_transcript_yes_after_a_delivered_readback_commits() -> None:
    w, _, _ = await voice_readback()
    outcome, events = await w.ask("the user says yes", transcript="Yes, go ahead!")
    assert outcome.end_reason == "committed" and outcome.final.say == ex.COMMIT_RESULT.summary_say
    (res,) = of(events, "action.result")
    assert res.outcome == "committed" and res.changed is True
    (commit,) = w.core_calls("/commit")
    assert commit.token == COMMIT_TOKEN and commit.headers["idempotency-key"] == ex.PENDING_ID
    assert len(Fake.seen) == 1  # only the prepare turn called the model
    assert (await w.values())["pending"] is None


async def test_clean_final_transcript_no_cancels() -> None:
    w, _, _ = await voice_readback()
    outcome, _ = await w.ask("the user declines", transcript="no thanks")
    assert outcome.end_reason == "cancelled" and len(w.core_calls("/cancel")) == 1 and w.core_calls("/commit") == []


async def test_paraphrased_question_the_user_confirms_never_commits() -> None:
    cases = (("yes", {"transcript": "hmm, what was the amount again", "final": True}),
             ("the user confirms", {"transcript": None, "final": False}),
             ("the user confirms", {"transcript": "the user confirms", "final": False}),
             ("the user confirms", {"transcript": "yes", "final": True, "item": None}))
    for question, kw in cases:  # each case right after a fresh read-back, so adjacency cannot mask a wrong source
        w, _, _ = await voice_readback([AIMessage("It is $96. Shall I go ahead?")])
        outcome, events = await w.ask(question, **kw)  # type: ignore[arg-type]
        assert w.core_calls("/commit") == [] and of(events, "action.result") == []
        assert outcome.end_reason == "answered", (question, kw)
    human = [m for m in Fake.seen[-1] if isinstance(m, HumanMessage)][-1]
    assert f"pending: {ex.READBACK.chat} awaiting yes or no" in human.content  # the model is told; it cannot commit


async def test_yes_looking_question_without_a_final_transcript_reprompts_with_the_card_and_never_commits() -> None:
    for kw in ({"transcript": None, "final": False}, {"transcript": "yes", "final": False},
               {"transcript": "yes", "final": True, "item": None}):
        w, _, _ = await voice_readback()
        outcome, events = await w.ask("yes", **kw)  # type: ignore[arg-type]
        assert outcome.end_reason == "readback" and outcome.final.say == REPROMPT_LINE
        assert w.core_calls("/commit") == [] and len(of(events, "confirmation.required")) == 1
        assert len(Fake.seen) == 1
        done, _ = await w.ask("yes", transcript="yes")  # the re-prompt is a code line about the pending: adjacent
        assert done.end_reason == "committed" and len(w.core_calls("/commit")) == 1


async def test_interrupted_readback_is_repeated_not_committed_then_the_next_clean_yes_commits() -> None:
    w, _, _ = await voice_readback()
    again, events = await w.ask("yes", transcript="yes", interrupted=True)
    assert again.end_reason == "readback" and again.final.say == ex.READBACK.voice
    assert w.core_calls("/commit") == [] and len(of(events, "confirmation.required")) == 1
    ref = (await w.values())["pending"]
    assert ref["readback_interrupted"] is False and ref["readback_turn_id"] == events[0].turn_id
    done, _ = await w.ask("yes", transcript="yes")
    assert done.end_reason == "committed" and len(w.core_calls("/commit")) == 1


async def test_a_pending_marked_interrupted_is_repeated_not_committed() -> None:
    w, _, _ = await voice_readback()
    ref = (await w.values())["pending"]
    await w.seed({"pending": {**ref, "readback_interrupted": True}})
    again, _ = await w.ask("yes", transcript="yes")
    assert again.end_reason == "readback" and w.core_calls("/commit") == []


async def test_interrupted_readback_followed_by_another_question_sets_readback_interrupted() -> None:
    w, _, _ = await voice_readback([AIMessage("Refunds reach your card in 5 to 10 days.")])
    outcome, _ = await w.ask("how long do refunds take", transcript="how long do refunds take", interrupted=True)
    assert outcome.end_reason == "answered" and w.core_calls("/commit") == []
    assert (await w.values())["pending"]["readback_interrupted"] is True


async def test_a_later_client_user_item_blocks_the_spoken_yes_and_repeats_the_readback() -> None:
    w, _, _ = await voice_readback()
    await w.post_items(("assistant", ex.READBACK.voice, False), ("user", "wait, which card is that", False),
                       ("assistant", "Your Visa ending 4242.", False))
    again, _ = await w.ask("yes", transcript="yes")
    assert again.end_reason == "readback" and again.final.say == ex.READBACK.voice and w.core_calls("/commit") == []
    done, _ = await w.ask("yes", transcript="yes")
    assert done.end_reason == "committed"


async def test_only_assistant_chatter_after_the_readback_still_lets_the_spoken_yes_commit() -> None:
    w, _, _ = await voice_readback()
    await w.post_items(("assistant", ex.READBACK.voice, False))
    done, _ = await w.ask("yes", transcript="yes")
    assert done.end_reason == "committed" and len(w.core_calls("/commit")) == 1


async def test_confirm_press_in_voice_still_commits() -> None:
    w, _, _ = await voice_readback()
    done, _ = await w.send(mode="voice", confirm={"pending_action_id": ex.PENDING_ID, "decision": "confirm"})
    assert done.end_reason == "committed" and len(w.core_calls("/commit")) == 1


# --- fix round 1 (review PR #56): I1 re-prompt never arms a yes, I2 merge unknown fails closed, M1, M2 ----------

class RaisingMerger:
    """What: a step 12 merger that always fails (one failed Postgres call). Use when: I2 tests. Inputs: none.
    Output: raises. Side effects: none. Role: fault double."""

    async def __call__(self, inp: Any, row: Any) -> Any:
        """What: fail. Use when: Ingress._merge. Inputs: input, row. Output: never. Side effects: none. Role: fake."""
        raise RuntimeError("transcript_items unavailable")


async def reprompt_then_yes(w: VoiceWorld, **first: Any) -> tuple[Any, Any]:
    """What: the reviewer's detour: a yes-looking question with no final transcript, then a clean final yes.
    Use when: I1 tests. Inputs: world, extra fields for the first ask. Output: (first outcome, second outcome).
    Side effects: as turns. Role: helper."""
    one, _ = await w.ask("yes", transcript=None, final=False, **first)
    two, _ = await w.ask("yes", transcript="yes")
    return one, two


async def test_i1_p3_reprompt_after_an_interrupted_readback_repeats_the_readback_and_the_yes_never_skips_it() -> None:
    w, _, _ = await voice_readback()
    one, two = await reprompt_then_yes(w, interrupted=True)
    assert one.end_reason == "readback" and one.final.say == ex.READBACK.voice  # the full read-back, not the re-prompt
    assert two.end_reason == "committed"  # only after the read-back was said again


async def test_i1_p3b_reprompt_never_clears_a_marked_interrupted_flag() -> None:
    w, _, _ = await voice_readback()
    ref = (await w.values())["pending"]
    await w.seed({"pending": {**ref, "readback_interrupted": True}})
    one, _ = await w.ask("yes", transcript=None, final=False)
    assert one.end_reason == "readback" and one.final.say == ex.READBACK.voice and w.core_calls("/commit") == []


async def test_i1_p4b_reprompt_never_stands_in_for_an_undelivered_readback() -> None:
    w, _, _ = await voice_readback()
    (tid,) = list(w.store.turns)
    w.store.turns[tid] = replace(w.store.turns[tid], status="failed")  # the read-back turn was never delivered
    one, _ = await w.ask("yes", transcript=None, final=False)
    assert one.end_reason == "readback" and one.final.say == ex.READBACK.voice and w.core_calls("/commit") == []


async def test_i1_p4d_reprompt_after_model_turns_repeats_the_readback() -> None:
    w, _, _ = await voice_readback([AIMessage("Five to ten days."), AIMessage("Visa ending 4242.")])
    await w.ask("how long", transcript="how long do refunds take")
    await w.ask("which card", transcript="which card is it")
    one, _ = await w.ask("yes", transcript=None, final=False)
    assert one.end_reason == "readback" and one.final.say == ex.READBACK.voice and w.core_calls("/commit") == []


async def test_i2_p6_a_failed_merge_blocks_the_spoken_yes() -> None:
    w, _, _ = await voice_readback()
    await w.post_items(("user", "wait, which card", False))
    w.ingress.merge_transcript = RaisingMerger()
    again, _ = await w.ask("yes", transcript="yes")
    assert again.end_reason == "readback" and w.core_calls("/commit") == []


async def test_i2_p6b_no_merger_blocks_the_spoken_yes() -> None:
    w, _, _ = await voice_readback()
    w.ingress.merge_transcript = None
    again, _ = await w.ask("yes", transcript="yes")
    assert again.end_reason == "readback" and w.core_calls("/commit") == []


async def test_m1_p5_a_user_item_beyond_the_display_window_still_blocks_the_yes() -> None:
    w, _, _ = await voice_readback()
    await w.post_items(("user", "wait, which card", False), ("assistant", "a" * 4000, False),
                       ("assistant", "b" * 4000, False))
    again, _ = await w.ask("yes", transcript="yes")
    assert again.end_reason == "readback" and w.core_calls("/commit") == []


async def test_m2_p8_a_transcript_without_an_item_id_never_reaches_the_thread() -> None:
    w, _, _ = await voice_readback()
    await w.ask("yes", transcript="yes my pin is 4821", final=True, item=None)
    assert all("4821" not in str(m.content) for m in (await w.values())["messages"])
