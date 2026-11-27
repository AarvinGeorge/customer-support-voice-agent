"""What: the checkpointed agent state schema SupportState (doc 07 s12 "Checkpoint state").
Use when: building the agent (create_agent state_schema) and reading or updating the thread.
Inputs: none.
Output: SupportState and PendingRef TypedDicts.
Side effects: none.
Role: shape of the LangGraph thread (thread_id = session_id). awaiting_verification and
user_turns are NOT here: they live only in agent.sessions (one authority per fact).
"""
from __future__ import annotations

from typing import Any, Literal

from langchain.agents.middleware.types import AgentState
from typing_extensions import NotRequired, TypedDict


class PendingRef(TypedDict):
    """What: reference to core's pending action (no expiry, read-back text or step-up flag copied).
    Use when: a prepare tool succeeded and the read-back is waiting for a yes or no.
    Inputs: id, kind, readback_turn_id, readback_interrupted.
    Output: dict shape.
    Side effects: none.
    Role: doc 07 s12 `pending`; core is the authority.
    """

    id: str
    kind: Literal["refund", "billing_email_change", "plan_change"] | str
    readback_turn_id: str
    readback_interrupted: bool


class SupportState(AgentState[Any]):
    """What: AgentState plus the doc 07 s12 fields.
    Use when: create_agent(state_schema=SupportState).
    Inputs: messages (from AgentState); verified; claims_snapshot and context_snapshot (cached copies
    for prompt building, core is the authority); pending; weak_streak; grounding_failures;
    last_turn_delivered.
    Output: TypedDict schema.
    Side effects: none.
    Role: checkpointed working memory.
    """

    verified: NotRequired[bool]
    claims_snapshot: NotRequired[dict[str, Any] | None]
    context_snapshot: NotRequired[dict[str, Any] | None]
    pending: NotRequired[PendingRef | None]
    weak_streak: NotRequired[int]
    grounding_failures: NotRequired[int]
    last_turn_delivered: NotRequired[bool]
