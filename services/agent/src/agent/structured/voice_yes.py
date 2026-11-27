"""What: the voice half of doc 07 s7.3 (CS-E-05.S1.T1): which text of a voice/ask may count as a spoken yes or no
(yes signal 3: the final verbatim `user_transcript` of `user_item_id`, never the realtime model's `question`), whether
the read-back was interrupted, and whether another client user item was posted after the read-back line.
Use when: the step 13 handler on a voice turn (agent.structured.handler).
Inputs: the parsed VoiceAskRequest body, the step 12 merged transcript text, the thread's state.pending.
Output: REPROMPT_LINE, final_transcript, merged_has_user_item, VoiceGate, voice_gate.
Side effects: none.
Role: pure rules; the handler decides the reply. Fails closed: a missing field means "not a spoken yes".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.structured.grammar import is_strict_no, is_strict_yes

REPROMPT_LINE = "I didn't catch that clearly. Please say yes, or press Confirm on screen."
# agent.voice.format_merged writes one line per client item: "customer: ..." or "customer (interrupted): ...".
USER_LINE_PREFIXES = ("customer: ", "customer (interrupted): ")


def final_transcript(body: Any) -> str | None:
    """What: the text yes signal 3 may read: `user_transcript` only when `transcript_final` is true, `user_item_id` is
    set and the transcript is not blank. Use when: a voice turn reaches step 13. Inputs: the request body (a
    VoiceAskRequest; any other body has no such fields). Output: the stripped transcript, or None.
    Side effects: none. Role: doc 07 s7.3 yes signal 3 preconditions; `question` is never read here."""
    if getattr(body, "transcript_final", False) is not True or not getattr(body, "user_item_id", None):
        return None
    said = (getattr(body, "user_transcript", None) or "").strip()
    return said or None


def merged_has_user_item(merged: str | None) -> bool:
    """What: whether the step 12 merge claimed a client user item for this turn (the item this call answers is left
    out by the merger, so any user line is another utterance after the read-back line).
    Use when: the voice adjacency check. Inputs: IngressInput.merged_transcript (format_merged output or None).
    Output: bool. Side effects: none. Role: doc 07 s7.3 "in voice, no other client user item was posted after that
    line"; a stray match only repeats the read-back (fails closed)."""
    if not merged:
        return False
    return any(line.startswith(USER_LINE_PREFIXES) for line in merged.splitlines())


@dataclass(frozen=True)
class VoiceGate:
    """What: the voice turn's structured-input reading. Use when: the handler on a voice turn.
    Inputs: see fields. Output: value. Side effects: none.
    Role: yes or no come only from the final transcript; reprompt is the s7.3 row 2 case; blocked means not adjacent
    in voice (interrupted read-back or a later user item); interrupted feeds state.pending.readback_interrupted."""

    yes: bool
    no: bool
    reprompt: bool
    interrupted: bool
    blocked: bool


def voice_gate(body: Any, question: str, merged: str | None, ref: dict[str, Any] | None) -> VoiceGate:
    """What: read one voice/ask for step 13. Use when: a voice turn with a current pending. Inputs: request body,
    the realtime model's question (IngressInput.text), merged transcript, state.pending (or None).
    Output: VoiceGate. Side effects: none.
    Role: a yes-looking `question` with no final transcript gives reprompt (never a commit); an interrupted read-back
    (`last_assistant_interrupted`, or state already marked) or a later client user item blocks the spoken yes."""
    said = final_transcript(body)
    interrupted = getattr(body, "last_assistant_interrupted", False) is not False
    blocked = interrupted or bool((ref or {}).get("readback_interrupted")) or merged_has_user_item(merged)
    if said is None:
        return VoiceGate(False, False, is_strict_yes(question), interrupted, blocked)
    return VoiceGate(is_strict_yes(said), is_strict_no(said), False, interrupted, blocked)
