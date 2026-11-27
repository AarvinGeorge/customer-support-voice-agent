"""What: the confirmation card helpers: core's PrepareResult.card (D-054) wins when present, the fallback build from
the PrepareResult fields, declined gives no card, and the doc 07 s6.2 button labels.
Use when: pytest. Inputs: none. Output: pass/fail. Side effects: none. Role: CS-E-03.S7.T8 card fields."""
from __future__ import annotations

from contracts.stubs import examples as ex

from agent.structured.pending import card_from_prepare, confirm_label

PREPARE = ex.PREPARE_ALLOWED.model_dump(mode="json")
CARD = ex.PENDING_PUBLIC.model_dump(mode="json")


def test_core_card_is_used_verbatim_when_the_prepare_result_carries_it() -> None:
    assert card_from_prepare({**PREPARE, "card": CARD}, "refund") == CARD


def test_fallback_card_from_the_prepare_fields_validates() -> None:
    card = card_from_prepare(PREPARE, "refund")
    assert card is not None and card["title"] == "Refund $96.00" and card["reason_code"] == "PRO_WITHIN_LIMITS"
    assert card["readback_text"] == ex.READBACK.chat and card["kind"] == "commit"


def test_declined_has_no_card() -> None:
    assert card_from_prepare({**PREPARE, "decision": "declined", "pending_action_id": None}, "refund") is None


def test_labels() -> None:
    assert confirm_label(CARD) == "Confirm refund"
    assert confirm_label({**CARD, "kind": "submit_for_approval"}) == "Send for approval"
    assert confirm_label({**CARD, "action_type": "billing_email"}) == "Confirm email change"
