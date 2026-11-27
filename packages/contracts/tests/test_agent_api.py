"""What: tests for the agent public request schemas (doc 07 s2.3).
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: guards ingress body validation rules."""
import uuid

import pytest
from pydantic import ValidationError

from contracts.agent_api import ConfirmInput, TurnRequest, VoiceAskRequest


def _tid():
    """What: fresh client turn id. Use when: tests. Inputs: none. Output: str.
    Side effects: none. Role: test helper."""
    return str(uuid.uuid4())


def test_turn_request_exactly_one_input():
    """What: zero or two inputs fail, one passes. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: s2.3 exactly-one rule."""
    TurnRequest.model_validate({"client_turn_id": _tid(), "text": "hi"})
    with pytest.raises(ValidationError):
        TurnRequest.model_validate({"client_turn_id": _tid()})
    with pytest.raises(ValidationError):
        TurnRequest.model_validate({"client_turn_id": _tid(), "text": "hi",
                                    "ticket_offer": {"accept": True}})


def test_text_length_bounds():
    """What: text 1..2000 chars. Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: message size limit."""
    with pytest.raises(ValidationError):
        TurnRequest.model_validate({"client_turn_id": _tid(), "text": ""})
    with pytest.raises(ValidationError):
        TurnRequest.model_validate({"client_turn_id": _tid(), "text": "x" * 2001})
    TurnRequest.model_validate({"client_turn_id": _tid(), "text": "x" * 2000})


def test_confirm_patterns():
    """What: pending id and step-up PIN patterns. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: s2.3 ConfirmInput."""
    ConfirmInput.model_validate({"pending_action_id": "PA-0A1B2C3D", "decision": "confirm"})
    with pytest.raises(ValidationError):
        ConfirmInput.model_validate({"pending_action_id": "PA-xyz", "decision": "confirm"})
    with pytest.raises(ValidationError):
        ConfirmInput.model_validate({"pending_action_id": "PA-0A1B2C3D", "decision": "confirm",
                                     "step_up_pin": "12345"})


def test_ticket_offer_accept_must_be_true():
    """What: ticket_offer.accept only true. Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: s6.7 button body."""
    with pytest.raises(ValidationError):
        TurnRequest.model_validate({"client_turn_id": _tid(), "ticket_offer": {"accept": False}})


def test_voice_ask_requires_flags():
    """What: voice ask needs transcript_final and last_assistant_interrupted.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: s2.3."""
    ok = {"client_turn_id": _tid(), "voice_session_id": "vs1", "realtime_call_id": "call_1",
          "question": "is my refund done", "transcript_final": True,
          "last_assistant_interrupted": False}
    VoiceAskRequest.model_validate(ok)
    bad = dict(ok)
    del bad["transcript_final"]
    with pytest.raises(ValidationError):
        VoiceAskRequest.model_validate(bad)
