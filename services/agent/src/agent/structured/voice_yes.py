"""What: the voice half of doc 07 s7.3 (CS-E-05.S1.T1): which text of a voice/ask may count as a spoken yes or no
(yes signal 3: the final verbatim `user_transcript` of `user_item_id`, never the realtime model's `question`), whether
the read-back was interrupted, and whether another client user item was posted after the read-back line.
Use when: the step 13 handler on a voice turn (agent.structured.handler).
Inputs: the parsed VoiceAskRequest body, IngressInput.later_user_item from step 12, the thread's state.pending.
Output: REPROMPT_LINE, final_transcript, voice_thread_text, VoiceGate, voice_gate.
Side effects: none.
Role: pure rules; the handler decides the reply. Fails closed: a missing field, or an unknown step 12 result (merge
failed or not wired), means "not a spoken yes" (review PR #56 I2).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.structured.grammar import is_strict_no, is_strict_yes

REPROMPT_LINE = "I didn't catch that clearly. Please say yes, or press Confirm on screen."


def final_transcript(body: Any) -> str | None:
    """What: the text yes signal 3 may read: `user_transcript` only when `transcript_final` is true, `user_item_id` is
    set and the transcript is not blank. Use when: a voice turn reaches step 13. Inputs: the request body (a
    VoiceAskRequest; any other body has no such fields). Output: the stripped transcript, or None.
    Side effects: none. Role: doc 07 s7.3 yes signal 3 preconditions; `question` is never read here."""
    if getattr(body, "transcript_final", False) is not True or not getattr(body, "user_item_id", None):
        return None
    said = (getattr(body, "user_transcript", None) or "").strip()
    return said or None


def voice_thread_text(body: Any, question: str) -> str:
    """What: the thread's human line for a voice turn ended in step 13: the question, plus the transcript only when
    it qualifies as final (final_transcript). Use when: the handler's deterministic voice replies.
    Inputs: request body, question. Output: text. Side effects: none.
    Role: a transcript with no user_item_id or not final never reaches the thread here (review PR #56 M2)."""
    said = final_transcript(body)
    if said is None or said.lower() == question.strip().lower():
        return question.strip()
    return f"{question.strip()}\n(the customer said: {said})"


@dataclass(frozen=True)
class VoiceGate:
    """What: the voice turn's structured-input reading. Use when: the handler on a voice turn.
    Inputs: see fields. Output: value. Side effects: none.
    Role: yes or no come only from the final transcript; reprompt is the s7.3 row 2 case (said only when otherwise
    adjacent); blocked means not adjacent in voice (interrupted read-back, a later user item, or step 12 unknown);
    interrupted feeds state.pending.readback_interrupted."""

    yes: bool
    no: bool
    reprompt: bool
    interrupted: bool
    blocked: bool


def voice_gate(body: Any, question: str, later_user_item: bool | None, ref: dict[str, Any] | None) -> VoiceGate:
    """What: read one voice/ask for step 13. Use when: a voice turn with a current pending. Inputs: request body,
    the realtime model's question (IngressInput.text), IngressInput.later_user_item (None: unknown), state.pending.
    Output: VoiceGate. Side effects: none.
    Role: a yes-looking `question` with no final transcript gives reprompt (never a commit); an interrupted read-back
    (`last_assistant_interrupted`, or state already marked), a later client user item, or an unknown step 12 result
    blocks the spoken yes."""
    said = final_transcript(body)
    interrupted = getattr(body, "last_assistant_interrupted", False) is not False
    blocked = interrupted or bool((ref or {}).get("readback_interrupted")) or later_user_item is not False
    if said is None:
        return VoiceGate(False, False, is_strict_yes(question), interrupted, blocked)
    return VoiceGate(is_strict_yes(said), is_strict_no(said), False, interrupted, blocked)
