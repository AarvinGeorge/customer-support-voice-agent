"""What: the confirmation code of doc 07 s7.3: the commit client on CORE_TOKEN_COMMIT (caller agent_commit) and the
Confirmer that commits, submits or cancels the current pending action with X-Turn-Id, maps core's last response to
the outcome wording, and runs the failure sequence (status check by idempotency key, budget-checked retry,
`unconfirmed`).
Use when: ingress step 13 has a qualifying yes (Confirm press or adjacent strict yes) or a qualifying no (Cancel
press or adjacent strict no) for the current pending action.
Inputs: a CoreCommitClient, a failure-ticket opener (agent token, one attempt), the turn's TurnControl, the
current pending action (PendingPublic dict from MeContext).
Output: CoreCommitClient, Confirmer, ActionReply, the outcome lines, agent_ticket_opener.
Side effects: core commit, submit, cancel and status calls; at most one failure ticket; step events.
Role: the ONLY module that uses the commit token (doc 07 s1.4). Tool modules must never import it: the CI import
guard (scripts/check_import_guard.py) and test_the_commit_token_is_not_reachable_from_tool_modules enforce it.
"Nothing was changed" is said only when core's last response says changed false.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from contracts import limits

from agent.clients.core_agent import CoreAgentClient
from agent.escalation import Escalator
from agent.clients.http import ToolCallError, base_headers, call_json_within
from agent.middleware.router import no_ticket_clause
from agent.middleware.tool_guard import clamp_timeout, retry_fits
from agent.settings import secret_problem
from agent.steps import Step
from agent.turn_control import TurnControl

COMMIT_TOKEN_ENV = "CORE_TOKEN_COMMIT"

CANCELLED_LINE = "Okay, I haven't changed anything."
TIMED_OUT_LINE = "That request timed out, so nothing was changed. Shall I set it up again?"
NOT_ACTIVE_LINE = "That request is no longer active, so nothing was changed. Ask me again if you still need it."
RULES_CHANGED_TAIL = "Nothing was changed."
STEP_UP_LINE = "That change needs your support PIN, which I can't take here, so nothing was changed."
REFUSED_LINE = "I couldn't complete that, so nothing was changed."
CANCEL_FAILED_LINE = "I couldn't cancel that just now. The request expires on its own in a few minutes."
VERIFY_LAPSED_LINE = ("Your verification has expired, so nothing was changed. Please verify again in the secure card, "
                      "then confirm.")
COMMIT_FAILED_LEAD = "Our billing system didn't respond, so nothing was changed."
SUBMIT_FAILED_LEAD = "I couldn't send this to our team just now, so nothing was sent."
FAILED_TICKET = "I've opened a ticket, ending {short}."
COMMIT_UNCONFIRMED = ("I couldn't confirm whether that went through, so I haven't tried again. Ticket ending {short} is "
                      "with our team, and you won't be charged or refunded twice.")
SUBMIT_UNCONFIRMED = ("I couldn't confirm whether your request reached our team. Ticket ending {short} is with them, "
                      "and it won't be approved twice.")
COMMIT_UNCONFIRMED_NO_TICKET = ("I couldn't confirm whether that went through, so I haven't tried again. {clause} "
                                "You won't be charged or refunded twice.")
SUBMIT_UNCONFIRMED_NO_TICKET = "I couldn't confirm whether your request reached our team. {clause} It won't be approved twice."
SUBMITTED_FROM_STATUS = ("I've sent this to our team for approval: {display}. You'll see the outcome when you ask "
                         "about your ticket.")
COMMITTED_FROM_STATUS = "That went through."

SUCCESS_STATE = {"commit": "committed", "submit": "awaiting_approval"}
OPEN_STATE = {"commit": "prepared", "submit": "awaiting_customer"}

OpenTicket = Callable[[TurnControl, str, dict[str, Any]], Awaitable[dict[str, Any] | None]]


class CoreCommitClient:
    """What: core calls on the commit token: commit, submit, cancel (with X-Turn-Id) and the status read.
    Use when: the Confirmer only. Inputs: base URL, the commit token, an httpx client, the fault flag.
    Output: JSON bodies or ToolCallError. Side effects: HTTP. Role: the commit token's one holder."""

    def __init__(self, base_url: str, token: str, *, http: httpx.AsyncClient, demo_faults_enabled: bool = False) -> None:
        """What: keep settings. Use when: app start or tests. Inputs: see class. Output: none. Side effects: none.
        Role: constructor."""
        self._base, self._token, self._http, self._faults = base_url.rstrip("/"), token, http, demo_faults_enabled

    @classmethod
    def from_env(cls, base_url: str, *, http: httpx.AsyncClient, demo_faults_enabled: bool = False) -> "CoreCommitClient":
        """What: build the client with the commit token from the environment. Use when: the app composition root.
        Inputs: base URL, client, fault flag. Output: client; ValueError naming the variable (never its value) when
        it is unset, empty, a placeholder or too short (review PR #58 I2: an empty token must not start healthy).
        Side effects: reads one env var (never logged). Role: the only read of the commit token in the agent."""
        token = os.environ.get(COMMIT_TOKEN_ENV)
        problem = secret_problem(token)
        if problem is not None or token is None:
            raise ValueError(f"{COMMIT_TOKEN_ENV} is {problem}: every commit would get 401")
        return cls(base_url, token, http=http, demo_faults_enabled=demo_faults_enabled)

    def _headers(self, control: TurnControl, *, idempotency_key: str | None = None, turn: bool = True) -> dict[str, str]:
        """What: s1.2 headers, X-Turn-Id, Idempotency-Key, and the forwarded tool_error fault (s10).
        Use when: every call. Inputs: control, key, whether X-Turn-Id is sent. Output: headers. Side effects: none.
        Role: header policy."""
        h = base_headers(self._token, control)
        if turn:
            h["X-Turn-Id"] = control.turn_id
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        if self._faults and control.demo_fault == "tool_error":
            h["X-Demo-Fault"] = "tool_error"
        return h

    async def commit(self, control: TurnControl, pending_id: str, timeout_s: float) -> Any:
        """What: POST /v1/actions/pending/{id}/commit, key {id}. Use when: kind commit. Inputs: control, id, timeout.
        Output: CommitResult JSON. Side effects: core may apply the action. Role: s3.3."""
        return await call_json_within(timeout_s, self._http, "POST", f"{self._base}/v1/actions/pending/{pending_id}/commit",
                                      self._headers(control, idempotency_key=pending_id), body={})

    async def submit(self, control: TurnControl, pending_id: str, timeout_s: float) -> Any:
        """What: POST /v1/actions/pending/{id}/submit, key {id}:submit. Use when: kind submit_for_approval.
        Inputs: control, id, timeout. Output: SubmitResult JSON. Side effects: core may create the approval ticket.
        Role: s3.3."""
        return await call_json_within(timeout_s, self._http, "POST", f"{self._base}/v1/actions/pending/{pending_id}/submit",
                                      self._headers(control, idempotency_key=f"{pending_id}:submit"), body={})

    async def cancel(self, control: TurnControl, pending_id: str, timeout_s: float) -> Any:
        """What: POST /v1/actions/pending/{id}/cancel. Use when: a qualifying no. Inputs: control, id, timeout.
        Output: {pending_action_id, state, changed false}. Side effects: core cancels the row. Role: s3.3."""
        return await call_json_within(timeout_s, self._http, "POST", f"{self._base}/v1/actions/pending/{pending_id}/cancel",
                                      self._headers(control), body={})

    async def status(self, control: TurnControl, pending_id: str, timeout_s: float) -> Any:
        """What: GET /v1/actions/pending/{id}. Use when: the failure sequence. Inputs: control, id, timeout.
        Output: PendingStatus JSON. Side effects: none at core. Role: s3.3 (scope actions.status)."""
        return await call_json_within(timeout_s, self._http, "GET", f"{self._base}/v1/actions/pending/{pending_id}",
                                      self._headers(control, turn=False))


@dataclass
class ActionReply:
    """What: the result of a confirm or cancel: action.result fields plus the customer's line and end reason.
    Use when: the handler emits action.result and ends the turn. Inputs: see fields. Output: value.
    Side effects: none. Role: one shape for every s7.3 outcome row."""

    outcome: str
    changed: bool | None
    text: str
    say: str
    end_reason: str
    ticket: dict[str, Any] | None = None
    reason_codes: list[str] = field(default_factory=list)
    policy_refs: list[dict[str, Any]] = field(default_factory=list)
    repeat_readback: bool = False
    keep_pending: bool = False
    needs_verification: bool = False


def _line(text: str, outcome: str, changed: bool | None, end_reason: str = "answered", **kw: Any) -> ActionReply:
    """What: an ActionReply whose chat and voice lines are the same. Use when: fixed lines. Inputs: text, outcome,
    changed, end reason, extras. Output: ActionReply. Side effects: none. Role: helper."""
    return ActionReply(outcome=outcome, changed=changed, text=text, say=text, end_reason=end_reason, **kw)


def agent_ticket_opener(core: CoreAgentClient) -> OpenTicket:
    """What: the failure-path ticket opener for the Confirmer, on the agent token through the escalation module: one
    attempt, clamped to min(3 s, remaining) (it runs in the reserve), key {session_id}:{turn_id}:esc:{reason}, origin
    code, escalation_reason tool_failure (the L1 s7 row), category refund or billing.
    Use when: the Confirmer's failure and unconfirmed rows. Inputs: core client. Output: async opener returning a
    TicketRef dict or None. Side effects: at most one core ticket call and its escalation step.
    Role: doc 07 s8.1 failure-path ticket row (0 retries)."""
    escalator = Escalator(core)

    async def open_ticket(control: TurnControl, reason: str, pending: dict[str, Any]) -> dict[str, Any] | None:
        """What: open the ticket. Use when: a commit or submit failed or is unconfirmed. Inputs: control, reason
        (commit_failed, commit_unconfirmed, submit_failed, submit_unconfirmed), pending. Output: TicketRef dict or
        None. Side effects: one call. Role: opener."""
        category = "refund" if pending.get("action_type") == "refund" else "billing"
        summary = (f"{pending.get('title') or 'Pending action'}: {reason.replace('_', ' ')} "
                   f"({pending.get('pending_action_id')}).")
        return await escalator.open_ticket(control, "tool_failure", summary=summary, category=category,
                                           key_reason=reason)

    return open_ticket


class Confirmer:
    """What: commit, submit or cancel the current pending action and word the outcome (doc 07 s7.3).
    Use when: a qualifying yes or no. Inputs: commit client, ticket opener. Output: confirm(), cancel().
    Side effects: core calls, one failure ticket at most, step events. Role: confirmation code."""

    def __init__(self, client: CoreCommitClient, open_ticket: OpenTicket) -> None:
        """What: keep dependencies. Use when: app start or tests. Inputs: client, opener. Output: none.
        Side effects: none. Role: constructor."""
        self.client, self.open_ticket = client, open_ticket

    async def _call(self, control: TurnControl, op: str, pending_id: str) -> Any:
        """What: one commit or submit attempt, clamped to min(5 s, remaining - 3 s). Use when: the sequence.
        Inputs: control, op, id. Output: body; raises ToolCallError, or ToolCallError("timeout") when the clamp
        leaves less than 1 s (the call is skipped, nothing was sent). Side effects: one call. Role: helper."""
        timeout = clamp_timeout(limits.CORE_COMMIT_TIMEOUT_S, control.remaining_s())
        if timeout is None:
            raise ToolCallError("timeout", message="skipped: not enough time left", changed=False)
        fn = self.client.commit if op == "commit" else self.client.submit
        return await fn(control, pending_id, timeout)

    async def confirm(self, control: TurnControl, pending: dict[str, Any]) -> ActionReply:
        """What: commit (kind commit) or submit (kind submit_for_approval), then the failure sequence on 503, 500
        or timeout. Use when: a qualifying yes. Inputs: control, current pending. Output: ActionReply.
        Side effects: see class. Role: s7.3 Actions on a qualifying yes, outcome table, failure sequence."""
        op = "submit" if pending.get("kind") == "submit_for_approval" else "commit"
        pid = str(pending["pending_action_id"])
        step = Step.start(control.events, op, op, "Sending this for approval" if op == "submit" else "Making the change")
        reply = await self._sequence(control, op, pid, pending)
        step.done("ok" if reply.outcome in ("committed", "submitted") else "error",
                  {"committed": "Made the change", "submitted": "Sent for approval"}.get(reply.outcome, "Did not make the change"),
                  detail=reply.outcome.replace("_", " "))
        return reply

    async def _sequence(self, control: TurnControl, op: str, pid: str, pending: dict[str, Any]) -> ActionReply:
        """What: first attempt, then status check, budget-checked retry with the same key, else the last response
        decides. Use when: confirm. Inputs: control, op, id, pending. Output: ActionReply. Side effects: calls.
        Role: s7.3 "Sequence on 503, 500 or timeout"."""
        try:
            return self._success(op, await self._call(control, op, pid))
        except ToolCallError as err:
            if not err.retryable and err.changed is False:
                return self._client_error(op, err, pending)
            if not err.retryable:  # a 4xx that does not say changed false: unknown, never "nothing was changed"
                return await self._failure(control, op, err, pending)
            last = err
        status = await self._status(control, pid)
        state = (status or {}).get("state")
        if state == SUCCESS_STATE[op]:
            return self._from_status(op, status or {})
        if state == OPEN_STATE[op] and retry_fits(limits.CORE_COMMIT_TIMEOUT_S, control.remaining_s(),
                                                  limits.CORE_READ_RETRY_BACKOFF_S):
            await asyncio.sleep(limits.CORE_READ_RETRY_BACKOFF_S)
            try:
                return self._success(op, await self._call(control, op, pid))
            except ToolCallError as err:
                if not err.retryable and err.changed is False:
                    return self._client_error(op, err, pending)
                last = err
        elif state == "expired":
            return _line(TIMED_OUT_LINE, "expired", False)
        elif state in ("cancelled", "superseded", "rejected", "rules_changed"):
            return _line(NOT_ACTIVE_LINE, "expired", False)
        return await self._failure(control, op, last, pending)

    async def _status(self, control: TurnControl, pid: str) -> dict[str, Any] | None:
        """What: the 2 s status check (clamped); None when it fails or does not fit. Use when: the sequence.
        Inputs: control, id. Output: PendingStatus dict or None. Side effects: one call. Role: helper."""
        timeout = clamp_timeout(limits.CORE_STATUS_CHECK_TIMEOUT_S, control.remaining_s())
        if timeout is None:
            return None
        try:
            res = await self.client.status(control, pid, timeout)
        except ToolCallError:
            return None
        return res if isinstance(res, dict) else None

    @staticmethod
    def _success(op: str, body: Any) -> ActionReply:
        """What: commit or submit 200 (also replayed). Use when: a call returned. Inputs: op, body.
        Output: committed or submitted reply with core's summary (chat) and summary_say (voice), "That went
        through." when core sent none.
        Side effects: none. Role: outcome rows 1 and 2."""
        text = body.get("summary") or COMMITTED_FROM_STATUS  # core committed: a missing summary is never an error
        say = body.get("summary_say") or text
        if op == "commit":
            return ActionReply("committed", True, text, say, "committed")
        return ActionReply("submitted", True, text, say, "submitted", ticket=body.get("ticket"))

    @staticmethod
    def _from_status(op: str, status: dict[str, Any]) -> ActionReply:
        """What: success learned from the status check. Use when: state committed or awaiting_approval.
        Inputs: op, PendingStatus dict. Output: reply (submit cites PendingStatus.ticket). Side effects: none.
        Role: s7.3 sequence "report success"."""
        if op == "commit":
            result = status.get("result") or {}
            return ActionReply("committed", True, result.get("summary") or COMMITTED_FROM_STATUS,
                               result.get("summary_say") or COMMITTED_FROM_STATUS, "committed")
        ticket = status.get("ticket") or {}
        text = SUBMITTED_FROM_STATUS.format(display=ticket.get("display") or "your ticket")
        return ActionReply("submitted", True, text, text, "submitted", ticket=status.get("ticket"))

    @staticmethod
    def _client_error(op: str, err: ToolCallError, pending: dict[str, Any]) -> ActionReply:
        """What: map a 4xx from commit or submit. Use when: a non-retryable error. Inputs: op, error, pending.
        Output: reply. Side effects: none. Role: outcome rows RULES_CHANGED, PENDING_EXPIRED, PENDING_NOT_ACTIVE or
        404, CONFIRMATION_REQUIRED (repeat the read-back), SESSION_NOT_VERIFIED (D-055: keep the pending, ask for
        verification), STEP_UP_REQUIRED; any other 4xx is a plain refusal.
        The caller sends a 4xx whose body does not say changed false to _failure (unconfirmed) instead."""
        code = err.code or ""
        if code == "RULES_CHANGED":
            refs = list(err.details.get("policy_refs") or [])
            message = str(err.details.get("message") or err.message or "The rules for this request have changed.")
            ref = f" See {refs[0]['title']}." if refs and refs[0].get("title") else ""
            return _line(f"{message.rstrip()}{ref} {RULES_CHANGED_TAIL}", "rules_changed", False,
                         reason_codes=list(err.details.get("reason_codes") or []), policy_refs=refs)
        if code == "PENDING_EXPIRED":
            return _line(TIMED_OUT_LINE, "expired", False)
        if code in ("PENDING_NOT_ACTIVE", "NOT_FOUND") or err.status == 404:
            return _line(NOT_ACTIVE_LINE, "expired", False)
        if code == "CONFIRMATION_REQUIRED":
            return ActionReply("failed", False, str(pending.get("readback_text") or ""),
                               str(pending.get("readback_say") or pending.get("readback_text") or ""), "readback",
                               repeat_readback=True)
        if code == "SESSION_NOT_VERIFIED":  # D-055: core kept the pending prepared; show the card again
            return _line(VERIFY_LAPSED_LINE, "failed", False, "verification_requested", keep_pending=True,
                         needs_verification=True)
        if code == "STEP_UP_REQUIRED":
            return _line(STEP_UP_LINE, "failed", False, keep_pending=True)
        return _line(REFUSED_LINE, "failed", False)

    async def _failure(self, control: TurnControl, op: str, last: ToolCallError, pending: dict[str, Any]) -> ActionReply:
        """What: the wording when no response proved success: 503 with changed false is `failed` ("nothing was
        changed" or "nothing was sent"), anything else (timeout, 500, status check failed) is `unconfirmed`;
        both open one failure ticket. Use when: end of the sequence. Inputs: control, op, last error, pending.
        Output: reply (end_reason escalated). Side effects: one ticket attempt. Role: outcome rows 7 to 9."""
        proven_unchanged = last.changed is False
        reason = f"{op}_{'failed' if proven_unchanged else 'unconfirmed'}"
        ticket = await self.open_ticket(control, reason, pending)
        short = (ticket or {}).get("short_id")
        clause = no_ticket_clause(control.turn_id)
        if proven_unchanged:
            lead = COMMIT_FAILED_LEAD if op == "commit" else SUBMIT_FAILED_LEAD
            text = f"{lead} {FAILED_TICKET.format(short=short) if short else clause}"
            return _line(text, "failed", False, "escalated", ticket=ticket)
        if short:
            text = (COMMIT_UNCONFIRMED if op == "commit" else SUBMIT_UNCONFIRMED).format(short=short)
        else:
            text = (COMMIT_UNCONFIRMED_NO_TICKET if op == "commit" else SUBMIT_UNCONFIRMED_NO_TICKET).format(clause=clause)
        return _line(text, "unconfirmed", None, "escalated", ticket=ticket)

    async def cancel(self, control: TurnControl, pending: dict[str, Any]) -> ActionReply:
        """What: core cancel (2 s, 0 retries). Use when: a qualifying no or a Cancel press. Inputs: control, pending.
        Output: cancelled reply ("Okay, I haven't changed anything." because core returned changed false), the
        not-active line on 409 or 404, or the cancel-failed line. Side effects: one call. Role: s7.3 qualifying no."""
        step = Step.start(control.events, "cancel", "cancel", "Cancelling the request")
        timeout = clamp_timeout(limits.CORE_CANCEL_TIMEOUT_S, control.remaining_s())
        try:
            if timeout is None:
                raise ToolCallError("timeout", message="skipped", changed=False)
            res = await self.client.cancel(control, str(pending["pending_action_id"]), timeout)
        except ToolCallError as err:
            if err.code in ("PENDING_NOT_ACTIVE", "NOT_FOUND") or err.status == 404:
                step.done("error", "Request was no longer active")
                return _line(NOT_ACTIVE_LINE, "expired", False)
            step.done("timeout" if err.kind == "timeout" else "error", "Could not cancel the request")
            return _line(CANCEL_FAILED_LINE, "failed", err.changed, keep_pending=True)  # changed as core last said
        step.done("ok", "Cancelled the request")
        if res.get("changed") is not False:  # never claim "haven't changed anything" without core saying so
            return _line("Okay, I've cancelled that request.", "cancelled", None, "cancelled")
        return _line(CANCELLED_LINE, "cancelled", False, "cancelled")
