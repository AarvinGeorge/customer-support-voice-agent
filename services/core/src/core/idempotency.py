"""What: the idempotency helper: one stored response per (caller, Idempotency-Key), replayed on a retry.
Use when: a mutation endpoint receives an Idempotency-Key (doc 07 s1.2, s3.3, s3.4).
Inputs: a connection (inside the request transaction), caller name, key, endpoint, request body, an effect function.
Output: IdempotentResult, run_idempotent.
Side effects: takes a transaction-scoped advisory lock, reads and inserts core.idempotency.
Role: makes a retried mutation have one effect. Effect and stored response commit together, so a crash between
them is impossible; two concurrent requests with one key serialise on the lock and the second one replays."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from core.errors import CoreError


@dataclass(frozen=True)
class IdempotentResult:
    """What: the outcome of run_idempotent. Use when: a handler returns the response.
    Inputs: status_code, body, replayed. Output: frozen value. Side effects: none.
    Role: replayed=True marks a stored response (CommitResult.replayed in doc 07 s3.3)."""

    status_code: int
    body: dict[str, Any]
    replayed: bool


def request_digest(endpoint: str, request: Any) -> bytes:
    """What: sha256 of the endpoint plus canonical JSON of the request. Use when: comparing a retry with the first call.
    Inputs: endpoint, request (JSON-able). Output: 32 bytes. Side effects: none.
    Role: detects a key reused for a different call. Callers pass the already redacted request if it may hold secrets."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{endpoint}\n{canonical}".encode()).digest()


def run_idempotent(conn: Any, *, caller: str, key: str, endpoint: str, request: Any,
                   fn: Callable[[], tuple[int, dict[str, Any]]]) -> IdempotentResult:
    """What: run `fn` once per (caller, key) and replay its stored result on later calls.
    Use when: inside a transaction that also holds the effect's writes. Inputs: connection, caller name, key,
    endpoint label, request body, fn returning (status_code, response dict).
    Output: IdempotentResult. Side effects: advisory lock until the transaction ends; one INSERT on first run.
    Raises CoreError VALIDATION_ERROR (details.reason idempotency_key_reused) when the key was used for a different
    endpoint or body; an exception from fn propagates and nothing is stored (the key stays usable).
    Role: see module."""
    digest = request_digest(endpoint, request)
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{caller}\x1f{key}",))
    row = conn.execute("SELECT request_sha256, status_code, response FROM idempotency WHERE caller = %s AND key = %s",
                       (caller, key)).fetchone()
    if row is not None:
        stored_digest, status_code, response = row
        if bytes(stored_digest) != digest:
            raise CoreError("VALIDATION_ERROR", "That Idempotency-Key was already used for a different request.",
                            details={"reason": "idempotency_key_reused"})
        return IdempotentResult(status_code, response, True)
    status_code, body = fn()
    conn.execute("INSERT INTO idempotency (key, caller, endpoint, request_sha256, status_code, response) "
                 "VALUES (%s,%s,%s,%s,%s,%s)", (key, caller, endpoint, digest, status_code, Jsonb(body)))
    return IdempotentResult(status_code, body, False)
