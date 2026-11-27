"""What: GroundingMiddleware, Gate 2 (doc 07 s2.5 item 9, s6.4): after each model call that produced a final draft
(no tool calls) it decides skip or check; a check emits status checking_policy, calls the grounding_verifier
(5 s in chat, 3 s in voice per D-061; 0 retries, fail closed) and emits the `gate` event; pass keeps the draft (the SSE adapter flushes it with
the citations recorded here); fail removes the draft from the thread with RemoveMessage (spike 3) and ends the
turn with the fixed clarifying line, or on the second consecutive fail, or at once on commitment, with an
escalate(grounding_failed | needs_commitment) route line and its ticket.
Use when: in the create_agent stack (after_model hooks run in reverse order, so this runs first after the model).
Inputs: the agent state (messages, grounding_failures), the bound TurnControl, a GroundingVerifier, the optional
open_ticket hook (the escalation module, CS-E-03.S4.T1; until then the line gives the turn reference).
Output: GroundingMiddleware.
The code rules (agent.gates.enforce_code_rules) run on every verifier reply: a claim backed only by an
injection_suspect chunk or by no known source is unsupported; zero claims passes (D-059: the verifier prompt
decides what a claim is); a verifier error, timeout, reply cut at max_tokens or unreadable reply fails closed, and
the step detail and summary.reason say which, so the drawer never shows it as an unsupported claim.
Side effects: one verifier call per checked draft; events; thread updates (RemoveMessage, fixed line,
grounding_failures); TurnControl.grounding, citations, gate_message_id, route.
Role: nothing streams before this decides (the adapter buffers every model message), so a failed draft is never
shown and never reaches the model again. With no verifier wired the gate is off and grounding stays "not_run"
(tests only; the service wires it through build_agent_from_registry).
"""
from __future__ import annotations

from typing import Any

from contracts import limits
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage

from agent.gates import (
    CHECKING_POLICY_LINE,
    CLARIFY_LINE,
    ERROR_DETAILS,
    GroundingVerifier,
    citations_for,
    enforce_code_rules,
    grounded_evidence,
    needs_grounding,
    start_gate_step,
    turn_messages,
)
from agent.middleware.router import OpenTicket, route_message
from agent.turn_control import Route, current_control

_TITLES = {"answer": "Checked the answer against policy", "clarify": "Could not confirm the answer",
           "escalate": "Could not confirm the answer", "skip": "No policy check needed"}


class GroundingMiddleware(AgentMiddleware):
    """What: see module. Use when: item 9 of the stack. Inputs: verifier (None: gate off), open_ticket hook.
    Output: None (keep the draft) or a thread update with jump_to end. Side effects: see module. Role: Gate 2."""

    def __init__(self, verifier: GroundingVerifier | None = None, open_ticket: OpenTicket | None = None) -> None:
        """What: keep the verifier and the ticket hook. Use when: build_middleware. Inputs: verifier, hook.
        Output: none. Side effects: none. Role: constructor."""
        super().__init__()
        self.verifier = verifier
        self.open_ticket = open_ticket

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """What: Gate 2 on the model's final draft. Use when: after each model call. Inputs: state, runtime.
        Output: None, {"grounding_failures": 0}, or {messages: [RemoveMessage(draft), line], grounding_failures,
        jump_to: end}. Side effects: see module. Role: doc 07 s6.4."""
        control = current_control()
        messages = list(state.get("messages") or [])
        if control is None or self.verifier is None or not messages:
            return None
        draft = messages[-1]
        if not isinstance(draft, AIMessage) or draft.tool_calls:
            return None
        text = draft.text if isinstance(draft.text, str) else str(draft.content)
        turn = turn_messages(messages[:-1])
        if not needs_grounding(text, any(isinstance(m, ToolMessage) for m in turn)):
            control.grounding = "skipped"
            start_gate_step(control, "grounding_gate", "Checking the answer against policy").finish(
                gate="grounding", verdict="skipped", decision="skip", title=_TITLES["skip"], status="skipped")
            return None
        if control.events is not None:
            control.events.emit("status", kind="checking_policy", message=CHECKING_POLICY_LINE)
        step = start_gate_step(control, "grounding_gate", "Checking the answer against policy")
        evidence, sources = grounded_evidence(state, messages[:-1])
        result = await self.verifier.verify(control, text, evidence)
        result = enforce_code_rules(result, text, control.retrieval_chunks, tool_names=sources)
        failures = int(state.get("grounding_failures") or 0)
        claims = result.claims
        counts: dict[str, Any] = {"claims_total": len(claims), "claims_supported": sum(c["supported"] for c in claims),
                                  "claims": claims, "commitment": result.commitment,
                                  "chunk_ids": sorted(control.retrieval_chunks)}
        if result.passed:
            control.grounding = "pass"
            control.citations = citations_for(control, result)
            step.finish(gate="grounding", verdict="pass", decision="answer", title=_TITLES["answer"], **counts)
            return {"grounding_failures": 0} if failures else None
        control.grounding = "fail"
        if result.commitment:
            reason: str | None = "needs_commitment"
        elif failures >= limits.GATE2_FAILURES_CLARIFY_MAX:
            reason = "grounding_failed"
        else:
            reason = None
        decision = "clarify" if reason is None else "escalate"
        verdict = "fail" if result.verdict == "pass" else result.verdict  # a commitment is never a pass (M7)
        error = result.verdict == "error"
        step.finish(gate="grounding", verdict=verdict, decision=decision, title=_TITLES[decision],
                    status="error" if error else "ok", reason=result.reason if error else None,
                    detail=ERROR_DETAILS.get(result.reason or "") if error else None, **counts)
        removal = RemoveMessage(id=str(draft.id))
        if reason is None:
            line = AIMessage(CLARIFY_LINE, id=f"gate2-{control.turn_id}")
            control.gate_message_id = line.id
            return {"messages": [removal, line], "grounding_failures": failures + 1, "jump_to": "end"}
        control.route = None  # no route can be pending here: one would have ended the turn before this model call
        control.set_route(Route("escalate", reason=reason))
        if self.open_ticket is not None and control.route is not None and control.route.ticket is None:
            control.route.ticket = await self.open_ticket(control)
        line = route_message(control)  # with no hook, agent.escalation opens the ticket after the loop
        next_failures = 0 if reason == "grounding_failed" else failures
        return {"messages": [removal, line], "grounding_failures": next_failures, "jump_to": "end"}
