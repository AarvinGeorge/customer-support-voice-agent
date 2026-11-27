"""What: the pending-action helpers of doc 07 s7.3 and s6.2: the adjacency rule, the confirmation card built from a
PrepareResult, the confirm button label, the state reference written with a read-back, and the ids of the
deterministic lines.
Use when: the readback route (confirmation.required on the prepare turn) and ingress step 13 (adjacency).
Inputs: thread values, PendingPublic or PrepareResult dicts, the turn id.
Output: is_adjacent, card_from_prepare, confirm_label, pending_ref, ingress_message_id, route_message_id.
Side effects: none.
Role: pure helpers with no client or token; safe for tool modules to import (it never imports confirm).
"""
from __future__ import annotations

from typing import Any

from contracts.core_api import PendingPublic
from langchain_core.messages import AIMessage
from pydantic import ValidationError

CONFIRM_LABELS = {"refund": "Confirm refund", "billing_email": "Confirm email change"}
SUBMIT_LABEL = "Send for approval"


def ingress_message_id(turn_id: str) -> str:
    """What: the id of a deterministic ingress line. Use when: appending a reply. Inputs: turn id. Output: id.
    Side effects: none. Role: lets adjacency find "a code re-prompt about it"."""
    return f"ingress-{turn_id}"


def route_message_id(turn_id: str) -> str:
    """What: the id RouterMiddleware gives its line. Use when: adjacency. Inputs: turn id. Output: id.
    Side effects: none. Role: matches agent.middleware.router (route-{turn_id})."""
    return f"route-{turn_id}"


def is_adjacent(values: dict[str, Any], pending: dict[str, Any], previous: tuple[str, str] | None) -> bool:
    """What: doc 07 s7.3 Adjacent (chat): state.pending names the current pending, readback_interrupted is false,
    the thread's last message is that read-back or a code re-prompt (the line written in the turn recorded as
    readback_turn_id), and that turn was delivered: it is the session's previous turn and its final row says
    completed (fix round 1, I1). Use when: a strict yes or no on typed text. Inputs: thread values, current pending,
    previous = (turn_id, status) of the session's previous turn, or None when unknown. Output: bool.
    Side effects: none. Role: rule; fails closed (None, a failed or running read-back turn, or any later turn or
    message means the read-back is shown again). The voice clause is CS-E-05.S1.T1."""
    ref = values.get("pending") or {}
    if ref.get("id") != pending.get("pending_action_id") or ref.get("readback_interrupted"):
        return False
    turn = ref.get("readback_turn_id")
    if not turn or previous != (turn, "completed"):
        return False
    messages = values.get("messages") or []
    last = messages[-1] if messages else None
    return isinstance(last, AIMessage) and last.id in (route_message_id(turn), ingress_message_id(turn))


def pending_ref(card: dict[str, Any], turn_id: str) -> dict[str, Any]:
    """What: the state.pending value written with a read-back in this turn. Use when: a read-back is repeated.
    Inputs: card, turn id. Output: PendingRef dict. Side effects: none. Role: s7.3 "sets readback_turn_id, clears
    readback_interrupted" (kind is the action type, as RouterMiddleware writes it)."""
    return {"id": card["pending_action_id"], "kind": card.get("action_type") or "refund",
            "readback_turn_id": turn_id, "readback_interrupted": False}


def card_from_prepare(res: dict[str, Any], action_type: str) -> dict[str, Any] | None:
    """What: the confirmation card (PendingPublic) for an allowed or needs_approval PrepareResult: core's `card` when
    present (D-054), else built from the PrepareResult fields (title from the amount, reason_code the primary
    reason). Use when: prepare_refund sets route readback. Inputs: PrepareResult JSON, action type.
    Output: validated PendingPublic dict, or None when declined, incomplete or core's card is malformed. Side effects: none.
    Role: card fields for confirmation.required and FinalAnswer.pending_action (doc 07 s3.3 r4)."""
    if res.get("decision") not in ("allowed", "needs_approval") or not res.get("pending_action_id"):
        return None
    if isinstance(res.get("card"), dict):  # a malformed card after core prepared: no card, never a tool failure
        try:
            return PendingPublic.model_validate(res["card"]).model_dump(mode="json")
        except ValidationError:
            return None
    readback = res.get("readback") or {}
    details = res.get("details") or {}
    amount = details.get("amount_cents")
    title = f"Refund ${amount / 100:,.2f}" if action_type == "refund" and isinstance(amount, int) else "Confirm this change"
    kind = res.get("pending_kind") or ("submit_for_approval" if res["decision"] == "needs_approval" else "commit")
    state = res.get("state") or ("awaiting_customer" if kind == "submit_for_approval" else "prepared")
    card = {"pending_action_id": res["pending_action_id"], "action_type": action_type, "kind": kind, "state": state,
            "readback_text": readback.get("chat") or "", "readback_say": readback.get("voice") or readback.get("chat") or "",
            "amount_cents": amount if isinstance(amount, int) else None, "title": title,
            "invoice_ref": details.get("invoice_ref"), "charge_date": details.get("charge_date"),
            "reason_code": (res.get("reason_codes") or [""])[0], "requires_step_up": bool(res.get("requires_step_up")),
            "expires_at": res.get("expires_at"), "ticket": None}
    return PendingPublic.model_validate(card).model_dump(mode="json")


def confirm_label(card: dict[str, Any]) -> str:
    """What: the card's button label. Use when: confirmation.required. Inputs: card. Output: "Confirm refund",
    "Send for approval" or "Confirm email change". Side effects: none. Role: doc 07 s6.2 confirm_label."""
    if card.get("kind") == "submit_for_approval":
        return SUBMIT_LABEL
    return CONFIRM_LABELS.get(str(card.get("action_type")), "Confirm")
