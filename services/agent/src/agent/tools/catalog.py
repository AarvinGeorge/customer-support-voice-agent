"""What: the thin-slice model tools (doc 07 s5.1 rows 1 to 6 and 10): search_kb, request_verification,
get_account_summary, list_charges, list_tickets, prepare_refund, create_ticket. The billing email, grace
extension and preference tools are designed only (doc 09 s1.2) and are not built.
Use when: build_tools(core, knowledge) once per process; the list goes to create_agent.
Inputs: a CoreAgentClient (agent token only) and a KnowledgeClient.
Output: build_tools, TOOL_NAMES.
Side effects: tools call core and knowledge; request_verification and prepare_refund set TurnControl.route.
Role: thin wrappers. Each tool validates its arguments with contracts.parse_tool_args, reads the turn from
TurnControl and its call id and state from CallScope (both set by ingress and ToolGuard), and returns
(content, artifact) where the artifact is the raw result. ToolGuard owns timeouts, the retry, limits and the
untrusted wrapping of every result the model sees. No tool takes an account, user, role, tier, PIN or amount.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, TypeVar

from contracts.base import StrictModel
from contracts.tools import (
    STATUS_VERIFICATION_REQUESTED,
    TOOL_ARGS,
    TOOL_DESCRIPTIONS,
    WEAK_RETRIEVAL_INSTRUCTION,
    CreateTicketArgs,
    GetAccountSummaryArgs,
    ListChargesArgs,
    ListTicketsArgs,
    PrepareRefundArgs,
    RequestVerificationArgs,
    SearchKbArgs,
    parse_tool_args,
    strict_json_schema,
)
from langchain_core.tools import BaseTool, StructuredTool

from agent.clients.core_agent import CoreAgentClient
from agent.clients.knowledge import KnowledgeClient
from agent.structured.pending import card_from_prepare
from agent.turn_control import CURRENT_CALL, CallScope, Route, TurnControl, require_control

TOOL_NAMES: tuple[str, ...] = ("search_kb", "request_verification", "get_account_summary", "list_charges",
                               "list_tickets", "prepare_refund", "create_ticket")
WEAK_VERDICTS = frozenset({"weak", "uncalibrated"})
Result = tuple[str, Any]
A = TypeVar("A", bound=StrictModel)


def _args(model: type[A], name: str, raw: dict[str, Any]) -> A:
    """What: validate raw model arguments (null means not given) into the typed args model.
    Use when: first line of every tool. Inputs: args class, tool name, raw args. Output: the args model;
    raises ValidationError (ToolNode turns it into an error result). Side effects: none. Role: helper."""
    parsed = parse_tool_args(name, raw)
    assert isinstance(parsed, model)
    return parsed


def _scope() -> CallScope:
    """What: the current tool call's scope; raises outside ToolGuard. Use when: a tool needs its call id or
    state. Inputs: none. Output: CallScope. Side effects: none. Role: fail loudly instead of a keyless call."""
    scope = CURRENT_CALL.get()
    if scope is None:
        raise RuntimeError("tool called outside ToolGuard (no CallScope)")
    return scope


def _state_get(state: Any, key: str) -> Any:
    """What: read a key from a dict-like state. Use when: claims and weak_streak. Inputs: state, key.
    Output: value or None. Side effects: none. Role: helper."""
    return state.get(key) if isinstance(state, dict) else getattr(state, key, None)


def _key(control: TurnControl, scope: CallScope) -> str:
    """What: the model-tool Idempotency-Key "{session_id}:{tool_call_id}" (doc 07 s3.2 to s3.4).
    Use when: prepare and ticket create; the retry reuses it. Inputs: control, scope. Output: key.
    Side effects: none. Role: one effect per tool call."""
    return f"{control.session_id}:{scope.tool_call_id}"


def charge_ref(charge_id: str | None, ending: str | None, on: Any) -> dict[str, Any] | None:
    """What: build prepare's charge_ref from the model's arguments: {id} from charge_id, else {ending, on},
    else null (doc 07 s5.1 row 6). Use when: prepare_refund. Inputs: charge id, ending, date or None.
    Output: dict or None. Side effects: none. Role: argument mapping (an `on` without `ending` gives null)."""
    if charge_id:
        return {"id": charge_id}
    if ending:
        return {"ending": ending, "on": on.isoformat() if hasattr(on, "isoformat") else on}
    return None


def build_tools(core: CoreAgentClient, knowledge: KnowledgeClient) -> list[BaseTool]:
    """What: the seven thin-slice tools bound to the given clients, with strict JSON schemas and the contract
    descriptions. Use when: once per process (build_agent tools=...). Inputs: core client, knowledge client.
    Output: list of StructuredTool (response_format content_and_artifact). Side effects: none at build time.
    Role: tool factory."""

    async def search_kb(**raw: Any) -> Result:
        """What: knowledge search with tier and verified from claims; relevant chunks only (s4.1, s5.1 row 1).
        Use when: the model searches help articles. Inputs: query, error_code. Output: ("ok", artifact with
        verdict, instruction, docs, chunk_ids, confidence). Side effects: one knowledge call. Role: tool."""
        args = _args(SearchKbArgs, "search_kb", raw)
        control, scope = require_control(), _scope()
        verified = bool(_state_get(scope.state, "verified"))
        claims = _state_get(scope.state, "claims_snapshot") or {}
        tier = claims.get("tier") if verified and claims.get("tier") else "public"
        body: dict[str, Any] = {"query": args.query, "tier": tier, "verified": verified and tier != "public"}
        if args.error_code:
            body["error_code"] = args.error_code
        resp = await knowledge.search(control, body)
        verdict = resp["confidence"]["verdict"]
        weak_before = int(_state_get(scope.state, "weak_streak") or 0) > 0
        docs = [{"id": c["chunk_id"], "article": c["article_slug"], "section": " > ".join(c["section_path"]),
                 "text": c["text"], "injection_suspect": "injection_suspect" in c.get("flags", [])}
                for c in resp["chunks"] if c.get("relevant")]
        return "ok", {"verdict": verdict,
                      "instruction": WEAK_RETRIEVAL_INSTRUCTION if verdict in WEAK_VERDICTS and not weak_before else None,
                      "docs": docs, "chunk_ids": [c["chunk_id"] for c in resp["chunks"]],
                      "confidence": resp["confidence"]}

    async def request_verification(**raw: Any) -> Result:
        """What: set route verify(reason) and mark the session awaiting verification (s6.3 step 2).
        Use when: unverified and account data or an action is needed. Inputs: reason.
        Output: ("ok", {"status": "verification_requested"}). Side effects: mutates TurnControl. Role: tool."""
        args = _args(RequestVerificationArgs, "request_verification", raw)
        control = require_control()
        control.set_route(Route("verify", reason=args.reason))
        control.awaiting_verification = True
        return "ok", dict(STATUS_VERIFICATION_REQUESTED)

    async def get_account_summary(**raw: Any) -> Result:
        """What: the verified profile (s5.1 row 3). Use when: account questions. Inputs: none.
        Output: ("ok", Profile JSON). Side effects: one core call. Role: tool."""
        _args(GetAccountSummaryArgs, "get_account_summary", raw)
        return "ok", await core.get_profile(require_control())

    async def list_charges(**raw: Any) -> Result:
        """What: recent charges, limit 10 (s5.1 row 4). Use when: billing roles ask about charges.
        Inputs: ending, on, since_days. Output: ("ok", {charges, ambiguous}). Side effects: one core call.
        Role: tool."""
        a = _args(ListChargesArgs, "list_charges", raw)
        on = a.on.isoformat() if a.on else None
        return "ok", await core.list_charges(require_control(), {"ending": a.ending, "on": on,
                                                                  "since_days": a.since_days, "limit": 10})

    async def list_tickets(**raw: Any) -> Result:
        """What: the customer's tickets (s5.1 row 5). Use when: ticket questions. Inputs: status, ending.
        Output: ("ok", {tickets}). Side effects: one core call. Role: tool."""
        a = _args(ListTicketsArgs, "list_tickets", raw)
        return "ok", await core.list_tickets(require_control(), {"status": a.status, "ending": a.ending})

    async def prepare_refund(**raw: Any) -> Result:
        """What: prepare a refund; core decides (s5.1 row 6, s3.3). An allowed or needs_approval result with a
        read-back sets route readback, so the model never writes the read-back.
        The route carries the confirmation card (core's PrepareResult.card, else built from the PrepareResult).
        Use when: the customer asks for a refund. Inputs: charge_id or ending and on, reason, note.
        Output: ("ok", {decision, reason_codes, message, policy_refs, ticket}). Side effects: one core call
        (same key on the retry); may set TurnControl.route. Role: tool."""
        a = _args(PrepareRefundArgs, "prepare_refund", raw)
        control, scope = require_control(), _scope()
        body = {"charge_ref": charge_ref(a.charge_id, a.ending, a.on), "reason": a.reason, "note": a.note}
        res = await core.prepare_refund(control, body, _key(control, scope))
        if res.get("decision") in ("allowed", "needs_approval") and res.get("readback"):
            control.set_route(Route("readback", readback=res["readback"], pending={
                "id": res.get("pending_action_id"), "kind": res.get("pending_kind"), "action_type": "refund",
                "requires_step_up": bool(res.get("requires_step_up")), "expires_at": res.get("expires_at"),
                "card": card_from_prepare(res, "refund")}))
        return "ok", {"decision": res["decision"], "reason_codes": res.get("reason_codes", []),
                      "message": res.get("message"), "policy_refs": res.get("policy_refs", []),
                      "ticket": res.get("ticket")}

    async def create_ticket(**raw: Any) -> Result:
        """What: open a ticket with origin "model" (s5.1 row 10, s3.4 owners). Use when: the customer asks for
        a human, a commitment is needed, or other model-owned cases. Inputs: category, summary, steps_tried,
        blocking. Output: ("ok", {ticket, merged, created}). Side effects: one core call (a ticket).
        Role: tool."""
        a = _args(CreateTicketArgs, "create_ticket", raw)
        control, scope = require_control(), _scope()
        body = {"category": a.category, "summary": a.summary, "steps_tried": list(a.steps_tried),
                "blocking": a.blocking, "origin": "model", "transcript_ref": control.session_id}
        return "ok", await core.create_ticket(control, body, _key(control, scope))

    funcs: dict[str, Callable[..., Awaitable[Result]]] = {
        "search_kb": search_kb, "request_verification": request_verification,
        "get_account_summary": get_account_summary, "list_charges": list_charges, "list_tickets": list_tickets,
        "prepare_refund": prepare_refund, "create_ticket": create_ticket}
    return [StructuredTool(name=name, description=TOOL_DESCRIPTIONS[name],
                           args_schema=strict_json_schema(TOOL_ARGS[name]), coroutine=funcs[name],
                           response_format="content_and_artifact")
            for name in TOOL_NAMES]
