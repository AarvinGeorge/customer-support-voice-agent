"""What: model tool argument models, strict JSON schemas, visibility sets, tool result shapes and the
Realtime ask_support_agent function (doc 07 s5.1, s5.2, s6.6).
Use when: binding tools to the agent model, validating tool arguments, or minting a Realtime session.
Inputs: none.
Output: one args model per thin-slice tool, TOOL_ARGS, TOOL_DESCRIPTIONS, VISIBILITY, strict_json_schema,
tool_schemas, parse_tool_args, result models, ASK_SUPPORT_AGENT_TOOL, FunctionCallOutput.
Side effects: none.
Role: the tool surface. No tool takes account_id, user_id, role, tier, a PIN or an amount, and there
is no commit, submit, cancel, verify or escalate tool. prepare_billing_email_change,
prepare_grace_extension and update_notification_preferences are designed only (doc 09 s1.2)."""
import copy
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from contracts.base import StrictModel
from contracts.core_api import CHARGE_ID_PATTERN, PolicyRef, RefundReason, TicketCategory, TicketRef
from contracts.knowledge_api import ERROR_CODE_PATTERN, Verdict


class SearchKbArgs(StrictModel):
    """What: arguments of search_kb. Use when: the model searches help articles.
    Inputs: query, optional error code. Output: model. Side effects: none. Role: doc 07 s5.1 row 1."""

    query: str = Field(min_length=3, max_length=300)
    error_code: str | None = Field(default=None, pattern=ERROR_CODE_PATTERN)


class RequestVerificationArgs(StrictModel):
    """What: arguments of request_verification. Use when: unverified and account data or an action
    is needed. Inputs: reason. Output: model. Side effects: none. Role: doc 07 s5.1 row 2, s6.3."""

    reason: Literal["account_lookup", "action", "ticket_status"]


class GetAccountSummaryArgs(StrictModel):
    """What: arguments of get_account_summary (none). Use when: the model reads the profile.
    Inputs: none. Output: model. Side effects: none. Role: doc 07 s5.1 row 3."""


class ListChargesArgs(StrictModel):
    """What: arguments of list_charges. Use when: a billing role asks about charges.
    Inputs: ending, on, since_days. Output: model. Side effects: none. Role: doc 07 s5.1 row 4."""

    ending: str | None = Field(default=None, pattern=r"^\d{4}$")
    on: date | None = None
    since_days: int = Field(default=90, ge=1, le=400)


class ListTicketsArgs(StrictModel):
    """What: arguments of list_tickets. Use when: the customer asks about tickets.
    Inputs: status, ending. Output: model. Side effects: none. Role: doc 07 s5.1 row 5."""

    status: Literal["open", "all"] = "open"
    ending: str | None = Field(default=None, pattern=r"^\d{4}$")


class PrepareRefundArgs(StrictModel):
    """What: arguments of prepare_refund. Use when: the customer asks for a refund.
    Inputs: charge_id or ending and on, reason, note. Output: model. Side effects: none.
    Role: doc 07 s5.1 row 6 (the wrapper builds charge_ref; core decides; nothing is changed)."""

    charge_id: str | None = Field(default=None, pattern=CHARGE_ID_PATTERN)
    ending: str | None = Field(default=None, pattern=r"^\d{4}$")
    on: date | None = None
    reason: RefundReason
    note: str = Field(max_length=300)


class CreateTicketArgs(StrictModel):
    """What: arguments of create_ticket. Use when: the customer asks for a human, a commitment is
    needed, or other model-owned escalations. Inputs: category, summary, steps, blocking.
    Output: model. Side effects: none. Role: doc 07 s5.1 row 10 (sent with origin "model")."""

    category: TicketCategory
    summary: str = Field(min_length=1, max_length=500)
    steps_tried: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=8)
    blocking: bool = False


TOOL_ARGS: dict[str, type[StrictModel]] = {
    "search_kb": SearchKbArgs,
    "request_verification": RequestVerificationArgs,
    "get_account_summary": GetAccountSummaryArgs,
    "list_charges": ListChargesArgs,
    "list_tickets": ListTicketsArgs,
    "prepare_refund": PrepareRefundArgs,
    "create_ticket": CreateTicketArgs,
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "search_kb": "Search Quillstack help articles. Use for any product, policy or billing-rule question.",
    "request_verification": "Ask the customer to verify identity before account data or an action.",
    "get_account_summary": "Read the verified customer's account and profile summary.",
    "list_charges": "List the verified account's recent charges, optionally by last four digits or date.",
    "list_tickets": "List the customer's support tickets, optionally by last four digits.",
    "prepare_refund": "Prepare a refund for one charge. Changes nothing; the rules decide and the customer confirms later.",
    "create_ticket": "Open a ticket for the support team when the customer asks for a human or needs a commitment.",
}

VISIBILITY: dict[str, tuple[str, ...]] = {
    "V0": ("create_ticket", "request_verification", "search_kb"),
    "V1": ("create_ticket", "get_account_summary", "list_tickets", "prepare_refund", "search_kb"),
    "V2": ("create_ticket", "get_account_summary", "list_charges", "list_tickets", "prepare_refund",
           "search_kb"),
}

# Status payloads ToolGuard returns instead of calling core (doc 07 s5.1, s6.3, s2.4 step 10).
STATUS_TOOL_NOT_AVAILABLE = {"status": "tool_not_available"}
STATUS_ONE_ACTION_AT_A_TIME = {"status": "one_action_at_a_time"}
STATUS_VERIFICATION_REQUESTED = {"status": "verification_requested"}
STATUS_NOT_RUN = {"status": "not_run", "reason": "turn_interrupted"}
WEAK_RETRIEVAL_INSTRUCTION = "Retrieval was weak. Ask exactly one clarifying question and state no policy."


def _strictify(node: Any) -> Any:
    """What: rewrite one JSON schema node for provider strict mode, recursively.
    Use when: inside strict_json_schema. Inputs: a schema node. Output: the rewritten node.
    Side effects: mutates the given copy. Role: strict-mode encoder."""
    if isinstance(node, list):
        return [_strictify(n) for n in node]
    if not isinstance(node, dict):
        return node
    node.pop("title", None)
    node.pop("default", None)
    for key, val in list(node.items()):
        node[key] = _strictify(val)
    if node.get("type") == "object":
        props = node.setdefault("properties", {})
        required = set(node.get("required", []))
        for name, prop in props.items():
            if name not in required and not (
                    isinstance(prop.get("anyOf"), list) and {"type": "null"} in prop["anyOf"]):
                props[name] = {"anyOf": [prop, {"type": "null"}]}
        node["required"] = list(props)
        node["additionalProperties"] = False
    return node


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """What: a strict JSON schema for one args model: every object closed, every property required,
    optional properties nullable, no titles or defaults, no $refs.
    Use when: binding a tool to a provider in strict mode.
    Inputs: an args model class. Output: JSON schema dict. Side effects: none.
    Role: doc 07 s5.1 "strict JSON schema". Pass the model's raw arguments through parse_tool_args."""
    schema = copy.deepcopy(model.model_json_schema())
    if "$defs" in schema:
        raise ValueError(f"{model.__name__}: nested models are not supported in tool args")
    return _strictify(schema)


def tool_schemas(names: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """What: function specs {name, description, parameters} for the given tools (all if None).
    Use when: building the tool list for one visibility set. Inputs: tool names.
    Output: list of dicts in the given order. Side effects: none. Role: tool binding source."""
    chosen = names if names is not None else tuple(TOOL_ARGS)
    return [{"name": n, "description": TOOL_DESCRIPTIONS[n], "parameters": strict_json_schema(TOOL_ARGS[n])}
            for n in chosen]


def parse_tool_args(name: str, raw: dict[str, Any]) -> StrictModel:
    """What: validate raw model arguments, treating null as "not given" so defaults apply.
    Use when: ToolGuard receives a tool call. Inputs: tool name, raw args.
    Output: the args model; raises KeyError for an unknown tool, ValidationError for bad args.
    Side effects: none. Role: the inverse of strict_json_schema's nullable encoding."""
    model = TOOL_ARGS[name]
    return model.model_validate({k: v for k, v in raw.items() if v is not None})


class SearchKbToolResult(StrictModel):
    """What: what search_kb returns to the model (before wrapping). Use when: ToolGuard wraps it.
    Inputs: verdict, optional clarify instruction, wrapped doc blocks. Output: model.
    Side effects: none. Role: doc 07 s5.1 result shapes."""

    verdict: Verdict
    instruction: str | None = None
    chunks: list[str]


class PrepareToolResult(StrictModel):
    """What: what a prepare tool returns to the model (before wrapping). Use when: ToolGuard wraps it.
    Inputs: decision, reasons, message, refs, ticket. Output: model. Side effects: none.
    Role: doc 07 s5.1 (allowed or needs_approval also sets route readback; the model never writes it)."""

    decision: Literal["allowed", "needs_approval", "declined"]
    reason_codes: list[str]
    message: str | None = None
    policy_refs: list[PolicyRef]
    ticket: TicketRef | None = None


class AskSupportAgentArgs(StrictModel):
    """What: arguments the Realtime model sends to ask_support_agent. Use when: the browser relays.
    Inputs: question. Output: model. Side effects: none. Role: doc 07 s6.6."""

    question: str = Field(max_length=2000)


ASK_SUPPORT_AGENT_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "ask_support_agent",
    "description": ("Ask the Quillstack support agent. Use for anything about Quillstack, the customer's "
                    "account, billing, policies, tickets or actions, and whenever the customer answers a "
                    "question the agent asked (yes, no, account ID, PIN)."),
    "parameters": {
        "type": "object", "additionalProperties": False, "required": ["question"],
        "properties": {"question": {"type": "string", "maxLength": 2000,
                                    "description": "What the customer said or asked, in their words"}},
    },
}


class SpokenTicket(StrictModel):
    """What: ticket reference for speech. Use when: FunctionCallOutput.ticket.
    Inputs: spoken, full id. Output: model. Side effects: none. Role: doc 07 s6.6."""

    spoken: str
    full_id: str


class FunctionCallOutput(StrictModel):
    """What: the JSON the browser returns to Realtime as function_call_output.
    Use when: a voice/ask stream ends (built from turn.completed.final).
    Inputs: say, escalated, ticket, awaiting. Output: model. Side effects: none. Role: doc 07 s6.6."""

    say: str
    escalated: bool
    ticket: SpokenTicket | None = None
    awaiting: Literal["confirm", "verification"] | None = None
