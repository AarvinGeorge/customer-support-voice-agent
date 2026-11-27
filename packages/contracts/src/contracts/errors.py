"""What: the error envelope and error code tables from doc 07 s1.3, s2.2, s4.1, s9.
Use when: returning or parsing any non-2xx body from agent, core or knowledge.
Inputs: none.
Output: ErrorBody, ErrorEnvelope, ERROR_CLASSES, CORE_ERROR_CODES, AGENT_ERROR_CODES, KNOWLEDGE_ERROR_CODES.
Side effects: none.
Role: one error shape across all services; business decisions are never errors (HTTP 200)."""
from typing import Any, Literal, get_args

from pydantic import Field

from contracts.base import StrictModel

ErrorClass = Literal[
    "provider_timeout", "provider_rate_limited", "provider_spend_cap", "provider_5xx",
    "provider_auth", "retrieval_failed", "core_client_error", "core_conflict", "core_unavailable",
    "db_unavailable", "redis_unavailable", "rate_limited", "admission_rejected",
    "turn_deadline_exceeded", "access_denied", "not_found", "validation",
]
ERROR_CLASSES: tuple[str, ...] = get_args(ErrorClass)


class ErrorBody(StrictModel):
    """What: the body of every error (JSON key "class" maps to class_).
    Use when: building or parsing an error response or an SSE error event.
    Inputs: class, code, message, retryable, changed, retry_after_s, fault_injected, trace_id, details.
    Output: validated error body; every dump emits the "class" key (input must use "class").
    Side effects: none.
    Role: doc 07 s1.3 ErrorBody. changed: false proven no effect, true applied, null unknown."""

    class_: ErrorClass = Field(alias="class")
    code: str
    message: str
    retryable: bool
    changed: bool | None
    retry_after_s: int | None = None
    fault_injected: bool
    trace_id: str | None = None
    details: dict[str, Any]


class ErrorEnvelope(StrictModel):
    """What: wrapper {"error": ErrorBody}.
    Use when: any non-2xx HTTP response.
    Inputs: error.
    Output: validated envelope.
    Side effects: none.
    Role: doc 07 s1.3 ErrorEnvelope."""

    error: ErrorBody


# code: (HTTP status, retryable, changed) from doc 07 s1.3.
CORE_ERROR_CODES: dict[str, tuple[int, bool, bool | None]] = {
    "AUTH_INVALID": (401, False, False),
    "SCOPE_DENIED": (403, False, False),
    "SESSION_NOT_VERIFIED": (401, False, False),
    "VERIFY_FAILED": (401, False, False),
    "VERIFY_LOCKED": (423, False, False),
    "IDENTITY_CONFLICT": (409, False, False),
    "STEP_UP_REQUIRED": (403, False, False),
    "ROLE_NOT_PERMITTED": (403, False, False),
    "NOT_FOUND": (404, False, False),
    "ACTION_DISABLED": (404, False, False),
    "AMBIGUOUS_REFERENCE": (409, False, False),
    "ACTION_IN_PROGRESS": (409, False, False),
    "PENDING_NOT_ACTIVE": (409, False, False),
    "CONFIRMATION_REQUIRED": (409, False, False),
    "RULES_CHANGED": (409, False, False),
    "INVALID_TRANSITION": (409, False, False),
    "PENDING_EXPIRED": (410, False, False),
    "VALIDATION_ERROR": (422, False, False),
    "TICKET_LIMIT_REACHED": (429, False, False),
    "TOOL_ERROR": (503, True, False),
    "INTERNAL": (500, True, None),
}

# Agent public codes (doc 07 s2.1, s2.2, s2.4, s9) with their HTTP status. Operator codes are
# designed only (doc 09 s1.2) and left out.
AGENT_ERROR_CODES: dict[str, int] = {
    "ACCESS_DENIED": 401,
    "ACCESS_ATTEMPTS": 429,
    "ORIGIN_MISMATCH": 403,
    "NOT_FOUND": 404,
    "VALIDATION_ERROR": 422,
    "TURN_IN_PROGRESS": 409,
    "DUPLICATE_CALL": 409,
    "VOICE_SESSIONS": 429,
    "VOICE_SESSION_EXPIRED": 429,
    "VOICE_UNAVAILABLE": 503,
    "SESSION_TURNS": 429,
    "IP_HOURLY": 429,
    "IP_SESSIONS": 429,
    "SESSION_POSTS": 429,
    "BUSY": 503,
    "REDIS_UNAVAILABLE": 503,
}

# Knowledge codes (doc 07 s4.1).
KNOWLEDGE_ERROR_CODES: dict[str, int] = {
    "UNAUTHORIZED": 401,
    "NOT_FOUND": 404,
    "INVALID_REQUEST": 422,
    "RETRIEVAL_UNAVAILABLE": 503,
}
