"""What: the mutable per-turn control object (TurnControl), the route it carries, and the context variables
that bind it to a turn and a tool call (doc 07 s2.5 item 1, s8.2; spike 15; D-048).
Use when: ingress (or stream_agent_turn) creates one TurnControl per turn and binds it ONCE with
bind_turn_control before the agent runs; tools, ToolGuard and the router only mutate that object and never
call .set() on TURN_CONTROL (a .set() inside a tool task is invisible to before_model, spike 15).
Inputs: session id, turn id, mode, optional monotonic deadline, demo fault, event sink.
Output: Route, TurnControl, EventSink, CallScope, bind_turn_control, current_control, require_control,
CURRENT_CALL.
Side effects: none beyond context variable binding.
Role: the one channel between tool wrappers and middleware inside a turn. Holds no secret and no token.
"""
from __future__ import annotations

import asyncio
import math
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

RouteKind = Literal["verify", "readback", "escalate", "decline_off_topic"]


def loop_clock() -> float:
    """What: the running event loop's clock (time.monotonic() outside a loop). Use when: the default TurnControl
    clock and the deadline arithmetic of a turn. Inputs: none. Output: seconds. Side effects: none.
    Role: the injectable clock: a test loop with virtual time (tests/virtual_clock.py) moves every deadline,
    clamp and asyncio timeout together; the default loop's time() is time.monotonic()."""
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return time.monotonic()


class EventSink(Protocol):
    """What: the part of TurnStream that ToolGuard and the router use. Use when: typing the sink.
    Inputs: see methods. Output: see methods. Side effects: events on the turn stream. Role: port."""

    def emit(self, type_: str, **fields: Any) -> Any:
        """What: emit one event. Use when: a step, route or escalation event. Inputs: type, fields.
        Output: the event. Side effects: queues the event. Role: port method."""
        ...

    def next_step_id(self) -> str:
        """What: the next step id. Use when: a step starts. Inputs: none. Output: id. Side effects: counter.
        Role: port method."""
        ...

    def ms(self) -> int:
        """What: milliseconds since the turn started. Use when: step timings. Inputs: none. Output: int.
        Side effects: none. Role: port method."""
        ...


@dataclass
class Route:
    """What: how the turn must end, decided in code (doc 07 s2.5 item 1).
    Use when: a tool or ToolGuard decides the turn ends with a fixed line: verify(reason), readback,
    escalate(reason), decline_off_topic.
    Inputs: kind; reason (verify reason or escalation reason); readback {chat, voice} and pending facts for
    readback; ticket (TicketRef dict) once one exists; tool (the failing tool, for tool_failure wording).
    Output: value. Side effects: none. Role: route payload read by RouterMiddleware and the SSE adapter."""

    kind: RouteKind
    reason: str | None = None
    readback: dict[str, str] | None = None
    pending: dict[str, Any] | None = None
    ticket: dict[str, Any] | None = None
    tool: str | None = None


@dataclass
class TurnControl:
    """What: one turn's mutable control state: route, model attempt role, tool counters, failure counts.
    Use when: created once per turn by ingress or stream_agent_turn; read and mutated by tools, ToolGuard,
    RouterMiddleware and DeadlineAndFaultMiddleware.
    Inputs: session_id, turn_id, mode, deadline (a value of `clock`; None means no deadline),
    demo_fault (already gated by DEMO_FAULTS_ENABLED at ingress), events (the turn stream or None), clock
    (default loop_clock). `ticket` is the one ticket opened this turn (model tool or escalation code), so a later
    escalation in the same turn reuses it instead of opening a second one (CS-E-03.S4.T1).
    Output: the object. Side effects: none.
    Role: D-048 counters live here (executed tool calls only; incremented before the first await); the model
    attempt fields (attempt_role, model_attempts, fallback_used, primary_error, primary_model, fault_fired) are written by
    DeadlineAndFaultMiddleware and read by the retry predicate and the usage builder.
    """

    session_id: str
    turn_id: str
    mode: str = "chat"
    deadline: float | None = None
    demo_fault: str | None = None
    events: EventSink | None = None
    route: Route | None = None
    route_message_id: str | None = None
    attempt_role: str = "primary"
    model_attempts: int = 0          # provider calls actually sent (DeadlineAndFaultMiddleware), for Usage.model_calls
    fallback_used: bool = False      # set when the fallback model ran this turn
    fallback_unfinished: bool = False  # the last fallback attempt started and did not return (failed, timed out or cut), D-058
    primary_error: BaseException | None = None  # last primary failure, for model.fallback reason
    primary_model: str | None = None  # id of the primary client, for model.fallback from_model
    fault_fired: bool = False        # an injected llm_429 was raised this turn
    tool_calls: int = 0
    per_tool: dict[str, int] = field(default_factory=dict)
    prepares: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    awaiting_verification: bool = False
    verify_attempts_left: int = 3
    ticket: dict[str, Any] | None = None
    model_node_failed: bool = False  # the agent model node raised (primary and fallback failed or skipped), s6.7
    clock: Callable[[], float] = loop_clock

    def remaining_s(self) -> float:
        """What: seconds left before the turn deadline. Use when: clamping a call. Inputs: none.
        Output: float (math.inf without a deadline). Side effects: reads the clock. Role: helper."""
        return math.inf if self.deadline is None else self.deadline - self.clock()

    def set_route(self, route: Route) -> bool:
        """What: set the route unless one is already set (the first decision wins).
        Use when: a tool or ToolGuard decides how the turn ends. Inputs: route.
        Output: True when this route was taken. Side effects: mutates self.route.
        Role: one route per turn; parallel tool calls cannot overwrite each other."""
        if self.route is not None:
            return False
        self.route = route
        return True


@dataclass(frozen=True)
class CallScope:
    """What: the tool call ToolGuard is running in this task: its id and the agent state it saw.
    Use when: a tool needs its tool_call_id (idempotency key) or the claims in state (search tier).
    Inputs: tool_call_id, state. Output: value. Side effects: none. Role: per-call context."""

    tool_call_id: str
    state: Any


TURN_CONTROL: ContextVar[TurnControl | None] = ContextVar("turn_control", default=None)
# Set by ToolGuard inside each tool call's own task (asyncio.gather gives every call its own context copy),
# so parallel calls never see each other's scope.
CURRENT_CALL: ContextVar[CallScope | None] = ContextVar("current_call", default=None)


def bind_turn_control(control: TurnControl) -> Token[TurnControl | None]:
    """What: bind the turn's control object to the context. Use when: once per turn, in the turn task,
    before agent.astream. Inputs: control. Output: the reset token. Side effects: sets TURN_CONTROL.
    Role: the only .set() on TURN_CONTROL (spike 15)."""
    return TURN_CONTROL.set(control)


def current_control() -> TurnControl | None:
    """What: the bound control object, or None outside a turn. Use when: middleware that may run in tests
    without a turn. Inputs: none. Output: TurnControl or None. Side effects: none. Role: accessor."""
    return TURN_CONTROL.get()


def require_control() -> TurnControl:
    """What: the bound control object; raises when none is bound. Use when: tools and ToolGuard, which must
    never run without counters. Inputs: none. Output: TurnControl. Side effects: none.
    Role: fail loudly instead of running a tool with no limits."""
    control = TURN_CONTROL.get()
    if control is None:
        raise RuntimeError("TurnControl is not bound for this turn (bind_turn_control before the run)")
    return control
