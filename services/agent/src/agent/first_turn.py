"""What: D-074 "identify early": on the first customer message of an unverified session, code always offers the
verification card, whatever the model did. The model only decides whether the message needs account data (it calls
request_verification, and the router then greets and asks for the card) and phrases its answer to a general
question; this module adds the card to a turn that did not already ask for it.
Use when: TurnRunner, after the agent loop and the escalation step, before turn.completed.
Inputs: the turn stream (its events so far), the turn's TurnControl (first_message from ingress), verified.
Output: OFFER_PROMPT, offer_card.
Side effects: at most one verification.required event; sets TurnControl.awaiting_verification (the runner then writes
agent.sessions.awaiting_verification, so Gate 2 and the escalation step know the card is on screen).
Role: the code guarantee of principle 22: meaning (does this need my account?) stays with the model, the offer of the
card on the first message is a floor in code. The card is non-blocking: the composer stays open and nothing waits.
"""
from __future__ import annotations

from agent.sse import TurnStream
from agent.turn_control import TurnControl

# prompt_text of the offered card (doc 07 s6.2 verification.required); the web shows its own card heading.
OFFER_PROMPT = "If you want help with your own account, you can verify in the secure card."


def offer_card(stream: TurnStream, control: TurnControl, verified: bool) -> bool:
    """What: emit verification.required (reason account_lookup) once on the first customer message of an unverified
    session when this turn has not already asked for the card (verify route, D-070 hold, typed PIN refusal) and
    verification is not locked.
    Use when: the end of TurnRunner._run, before the awaiting flag is written. Inputs: stream, control, whether the
    thread is verified. Output: True when the card was offered here. Side effects: one event; sets
    control.awaiting_verification. Role: D-074 floor (never doubles a card, never shown to a verified session)."""
    if not control.first_message or verified or stream.completed:
        return False
    if any(e.type == "verification.required" for e in stream.events):
        return False
    if control.verify_attempts_left <= 0 or (control.requester_refusal or {}).get("verify_locked"):
        return False  # verification is locked: a card that cannot work is never offered (D-070 reach-support line)
    stream.emit("verification.required", reason="account_lookup", prompt_text=OFFER_PROMPT,
                attempts_left=control.verify_attempts_left, fields=["account_id", "pin"])
    control.awaiting_verification = True
    return True
