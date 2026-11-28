"""What: GroundingMiddleware, Gate 2 (doc 07 s2.5 item 9, s6.4 as amended by D-064): every model-written final
draft (no tool calls) is checked; a line our code wrote (agent.gates.is_code_line) is not a model draft and is
never checked. A check emits status checking_policy, calls the grounding_verifier
(5 s in chat, 3 s in voice per D-061; 0 retries, fail closed) and emits the `gate` event; pass keeps the draft (the SSE adapter flushes it with
the citations recorded here); fail removes the draft from the thread with RemoveMessage (spike 3) and ends the
turn with the fixed clarifying line, or on the second consecutive fail with an escalate(grounding_failed) route line
and its ticket. A commitment draft is a fail like any other (CS-E-03.F14): removed, never shown, and it does not by
itself escalate, because it says the draft over-promised, not that the customer's request needs our team.
Use when: in the create_agent stack (after_model hooks run in reverse order, so this runs first after the model).
Inputs: the agent state (messages, grounding_failures), the bound TurnControl, a GroundingVerifier, the optional
open_ticket hook (by default the escalation module opens the ticket after the loop and rewrites the line; the line
written here carries no ticket clause until an attempt is recorded, CS-E-03.F12).
Output: GroundingMiddleware.
The code rules (agent.gates.enforce_code_rules) run on every verifier reply: a claim backed only by an
injection_suspect chunk or by no known source is unsupported; zero claims passes (D-059: the verifier prompt
decides what a claim is) only when the verifier states why (D-069: every empty list needs a no_claims_reason, else
fail closed with reason no_claims_unexplained; no regex); a verifier error, timeout, reply cut at max_tokens or unreadable reply fails closed, and
the step detail and summary.reason say which, so the drawer never shows it as an unsupported claim.
Side effects: one verifier call per model draft; events; thread updates (RemoveMessage, fixed line,
grounding_failures); TurnControl.grounding, citations, gate_message_id, route.
Role: nothing streams before this decides (the adapter buffers every model message), so a failed draft is never
shown and never reaches the model again. With no verifier wired the gate is off and grounding stays "not_run"
(tests only; the service wires it through build_agent_from_registry).
"""
from __future__ import annotations

from typing import Any

from contracts import limits
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, RemoveMessage

from agent.line_ids import GATE2, code_line
from agent.gates import (
    CHECKING_POLICY_LINE,
    CLARIFY_LINE,
    ERROR_DETAILS,
    NO_CLAIMS_DETAIL,
    GroundingVerifier,
    citations_for,
    enforce_code_rules,
    is_code_line,
    session_evidence,
    start_gate_step,
)
from agent.middleware.router import OpenTicket, record_hook_ticket, route_message
from agent.turn_control import Route, current_control

_TITLES = {"answer": "Checked the answer against policy", "clarify": "Could not confirm the answer",
           "escalate": "Could not confirm the answer"}


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
        jump_to: end}. Side effects: see module. Role: doc 07 s6.4 as amended by D-064 (no skip for a model draft)."""
        control = current_control()
        messages = list(state.get("messages") or [])
        if control is None or self.verifier is None or not messages:
            return None
        draft = messages[-1]
        if not isinstance(draft, AIMessage) or draft.tool_calls or is_code_line(draft):
            return None
        text = draft.text if isinstance(draft.text, str) else str(draft.content)
        if control.events is not None:
            control.events.emit("status", kind="checking_policy", message=CHECKING_POLICY_LINE)
        step = start_gate_step(control, "grounding_gate", "Checking the answer against policy")
        evidence = session_evidence(state, messages[:-1], control)  # D-065, D-071: session-scoped, with provenance
        result = await self.verifier.verify(control, text, evidence)
        result = enforce_code_rules(result, text, evidence.chunks, tool_names=evidence.ids)
        failures = int(state.get("grounding_failures") or 0)
        claims = result.claims
        # The gate event names a help-article claim by its chunk id (the drawer's id), not by its doc@n sid (N1).
        shown = [{**c, "source_id": evidence.chunks[c["source_id"]]["chunk_id"]} if c.get("source_id") in evidence.chunks
                 else c for c in claims]
        counts: dict[str, Any] = {"claims_total": len(claims), "claims_supported": sum(c["supported"] for c in claims),
                                  "claims": shown, "commitment": result.commitment,
                                  "chunk_ids": sorted(str(c["chunk_id"]) for c in evidence.chunks.values())}  # M6
        sources = {c["source_id"]: evidence.provenance[c["source_id"]] for c in claims
                   if c["supported"] and c.get("source_id") in evidence.provenance}  # review PR #71 I4
        if result.passed:
            control.grounding = "pass"
            control.grounding_supported = counts["claims_supported"]
            control.citations = citations_for(control, result, evidence.chunks)
            step.finish(gate="grounding", verdict="pass", decision="answer", title=_TITLES["answer"],
                        sources=sources, **counts)
            return {"grounding_failures": 0} if failures else None
        control.grounding = "fail"
        # CS-E-03.F14: a commitment fails the DRAFT (the floor: it never leaves) and takes the ordinary fail path. It
        # is not evidence that the customer's request needs our team; that call is the model's (create_ticket
        # category commitment, doc 07 s3.4), so code never writes a "needs a decision from our team" line here.
        reason: str | None = "grounding_failed" if failures >= limits.GATE2_FAILURES_CLARIFY_MAX else None
        decision = "clarify" if reason is None else "escalate"
        verdict = "fail" if result.verdict == "pass" else result.verdict  # a commitment is never a pass (M7)
        error = result.verdict == "error"
        detail = ERROR_DETAILS.get(result.reason or "") if error else (
            NO_CLAIMS_DETAIL if result.reason == "no_claims_unexplained" else None)
        step.finish(gate="grounding", verdict=verdict, decision=decision, title=_TITLES[decision],
                    status="error" if error else "ok", reason=result.reason, detail=detail, sources=sources, **counts)
        removal = RemoveMessage(id=str(draft.id))
        if reason is None:
            line = code_line(GATE2, control.turn_id, CLARIFY_LINE, control.identity)
            control.gate_message_id = line.id
            return {"messages": [removal, line], "grounding_failures": failures + 1, "jump_to": "end"}
        control.route = None  # no route can be pending here: one would have ended the turn before this model call
        control.set_route(Route("escalate", reason=reason))
        if self.open_ticket is not None and control.route is not None and control.route.ticket is None:
            control.route.ticket = record_hook_ticket(control, await self.open_ticket(control))
        line = route_message(control)  # with no hook, agent.escalation opens the ticket after the loop
        return {"messages": [removal, line], "grounding_failures": 0, "jump_to": "end"}  # reason is grounding_failed
