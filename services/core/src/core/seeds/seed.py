"""What: load data/seeds/*.yaml, plan the rows for a given day (relative dates, PIN HMACs) and write them.
Use when: the seed CLI runs, and tests that need the demo data.
Inputs: the seed directory, a clock (or an aware datetime), PIN_PEPPER, a database connection.
Output: SeedData, PlannedRows, load_seed_data, plan_rows, pin_hmac, apply_seed, run_seed, SeedError.
Side effects: apply_seed and run_seed write to core tables, core.meta and core.audit_events.
Role: doc 09 CS-E-09.S4.T1 (L1 s3, s4; doc 07 s3.6, s15.1 row 4). Dates: every charge and history row is
`days_ago` whole local days before the seeding day (America/New_York), so refund windows hold on demo day.
Core stores only HMAC-SHA256(PIN_PEPPER, "account_id:pin"); the plain demo PINs live only in personas.yaml."""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from contracts.agent_api import Persona
from pydantic import BaseModel, ConfigDict

from core.audit import AuditEvent, write_audit
from core.clock import DEFAULT_TZ, Clock

# Tables a reseed empties (audit_events is never touched; it is append-only). One statement so foreign keys agree.
_RESET_TABLES = ("accounts, users, charges, session_claims, verify_attempts, pending_actions, action_history, "
                 "tickets, ticket_events, idempotency, meta")


class SeedError(Exception):
    """What: raised for bad seed data or configuration. Use when: loading or planning. Inputs: message.
    Output: exception. Side effects: none. Role: fail loudly instead of seeding half a data set."""


class _Row(BaseModel):
    """What: strict base for seed file rows. Use when: subclassing. Inputs: none. Output: model that rejects
    unknown keys. Side effects: none. Role: a typo in a YAML key is an error, not silently ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SeedAccount(_Row):
    """What: one account row. Use when: parsing accounts.yaml. Inputs: id, name, tier, contract_status, seats,
    billing_email. Output: model. Side effects: none. Role: core.accounts."""

    id: str
    name: str
    tier: Literal["free", "pro", "business", "enterprise"]
    contract_status: Literal["trialing", "active", "past_due", "in_notice", "cancelled"]
    seats: int
    billing_email: str | None


class SeedUser(_Row):
    """What: one person. Use when: parsing users.yaml. Inputs: id, account_id, display_name, role.
    Output: model. Side effects: none. Role: core.users (the PIN hash comes from personas.yaml)."""

    id: str
    account_id: str
    display_name: str
    role: Literal["owner", "billing_admin", "member"]


class SeedCharge(_Row):
    """What: one charge with a relative date. Use when: parsing charges.yaml. Inputs: id, account_id, days_ago,
    time, amount_cents, status, invoice, description, refunded_days_ago. Output: model. Side effects: none.
    Role: core.charges."""

    id: str
    account_id: str
    days_ago: int
    time: str
    amount_cents: int
    status: Literal["paid", "failed", "refunded"]
    invoice: bool = False
    description: str
    refunded_days_ago: int | None = None


class SeedHistory(_Row):
    """What: one committed refund. Use when: parsing history.yaml. Inputs: account_id, user_id, charge_id,
    days_ago, committed_by. Output: model. Side effects: none. Role: core.action_history."""

    account_id: str
    user_id: str
    charge_id: str
    days_ago: int
    committed_by: Literal["customer", "operator"]


@dataclass(frozen=True)
class SeedData:
    """What: everything read from data/seeds. Use when: planning rows or testing. Inputs: built by load_seed_data.
    Output: accounts, users, charges, history, personas_raw (dicts validated as contracts Persona), triggers,
    seeds_dir. Side effects: none. Role: the parsed, cross-checked seed set."""

    accounts: list[SeedAccount]
    users: list[SeedUser]
    charges: list[SeedCharge]
    history: list[SeedHistory]
    personas_raw: list[dict[str, Any]]
    triggers: dict[str, dict[str, Any]]
    seeds_dir: Path


@dataclass(frozen=True)
class PlannedRows:
    """What: rows ready to INSERT, dates resolved and PINs hashed. Use when: apply_seed. Inputs: built by plan_rows.
    Output: lists of column dicts per table plus seed_date. Side effects: none. Role: the pure, testable middle."""

    accounts: list[dict[str, Any]]
    users: list[dict[str, Any]]
    charges: list[dict[str, Any]]
    history: list[dict[str, Any]]
    seed_date: str


def default_seeds_dir() -> Path:
    """What: where the seed files are. Use when: no directory is given. Inputs: env SEEDS_DIR.
    Output: Path (env value, else <repo>/data/seeds). Side effects: none. Role: containers mount the folder and
    set SEEDS_DIR; tests use the repository copy."""
    env = os.environ.get("SEEDS_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[5] / "data" / "seeds"


def _yaml(path: Path) -> Any:
    """What: read one YAML file with safe_load. Use when: loading. Inputs: path. Output: parsed data.
    Side effects: reads a file. Role: helper (raises SeedError when the file is missing)."""
    if not path.exists():
        raise SeedError(f"seed file missing: {path}")
    return yaml.safe_load(path.read_text())


def pin_hmac(pepper: str, account_id: str, pin: str) -> bytes:
    """What: HMAC-SHA256 of "account_id:pin" keyed by the pepper. Use when: storing or checking a PIN.
    Inputs: pepper, account id, PIN. Output: 32 bytes. Side effects: raises SeedError on an empty pepper.
    Role: draft C C-6; the pepper never reaches the database, so a dump alone cannot brute-force PINs."""
    if not pepper:
        raise SeedError("PIN_PEPPER is empty; refusing to hash PINs with no secret")
    return hmac.new(pepper.encode(), f"{account_id}:{pin}".encode(), hashlib.sha256).digest()


def load_seed_data(seeds_dir: Path | str | None = None) -> SeedData:
    """What: read and cross-check every seed file. Use when: planning rows, tests. Inputs: directory (default
    default_seeds_dir()). Output: SeedData. Side effects: reads files; raises SeedError on a broken link: a user
    without a persona, a persona that matches no user, a charge or history row for an unknown account, a refunded
    charge without its history row. Role: the loader; strict models reject unknown keys."""
    d = Path(seeds_dir) if seeds_dir else default_seeds_dir()
    accounts = [SeedAccount.model_validate(x) for x in _yaml(d / "accounts.yaml")["accounts"]]
    users = [SeedUser.model_validate(x) for x in _yaml(d / "users.yaml")["users"]]
    charges = [SeedCharge.model_validate(x) for x in _yaml(d / "charges.yaml")["charges"]]
    history = [SeedHistory.model_validate(x) for x in _yaml(d / "history.yaml")["history"]]
    personas = _yaml(d / "personas.yaml")
    for p in personas:
        Persona.model_validate(p)
    triggers = _yaml(d / "triggers.yaml")["refund_triggers"]
    account_ids = {a.id for a in accounts}
    for u in users:
        if u.account_id not in account_ids:
            raise SeedError(f"user {u.id} has unknown account {u.account_id}")
        if sum(1 for p in personas if (p["account_id"], p["name"]) == (u.account_id, u.display_name)) != 1:
            raise SeedError(f"user {u.id} ({u.display_name}) needs exactly one persona entry")
    for p in personas:
        if not any((u.account_id, u.display_name) == (p["account_id"], p["name"]) for u in users):
            raise SeedError(f"persona {p['name']} matches no user")
    charge_by_id = {c.id: c for c in charges}
    for c in charges:
        if c.account_id not in account_ids:
            raise SeedError(f"charge {c.id} has unknown account {c.account_id}")
        if (c.status == "refunded") != (c.refunded_days_ago is not None):
            raise SeedError(f"charge {c.id}: refunded_days_ago is set exactly when status is refunded")
    for h in history:
        if charge_by_id.get(h.charge_id) is None or charge_by_id[h.charge_id].status != "refunded":
            raise SeedError(f"history row for {h.charge_id}: the charge must exist and be refunded")
    refunded = {c.id for c in charges if c.status == "refunded"}
    if refunded != {h.charge_id for h in history}:
        raise SeedError("every refunded charge needs exactly one history row")
    return SeedData(accounts, users, charges, history, personas, triggers, d)


def _at(today: date, days_ago: int, clock_time: str) -> datetime:
    """What: the New York wall time `days_ago` days before `today`. Use when: resolving relative dates.
    Inputs: today, days, "HH:MM". Output: aware datetime (DST-correct because the zone is attached after the date
    arithmetic). Side effects: none. Role: helper."""
    hh, mm = (int(x) for x in clock_time.split(":"))
    return datetime.combine(today - timedelta(days=days_ago), time(hh, mm), tzinfo=ZoneInfo(DEFAULT_TZ))


def plan_rows(data: SeedData, now: datetime, pepper: str) -> PlannedRows:
    """What: turn the seed set into INSERT-ready rows for the day `now` falls on (New York).
    Use when: seeding and tests. Inputs: SeedData, aware datetime, pepper. Output: PlannedRows.
    Side effects: none (raises SeedError on an empty pepper). Role: the pure function every date assertion uses."""
    if now.tzinfo is None:
        raise SeedError("plan_rows needs an aware datetime")
    today = now.astimezone(ZoneInfo(DEFAULT_TZ)).date()
    pins = {(p["account_id"], p["name"]): p["pin"] for p in data.personas_raw}
    accounts = [a.model_dump() for a in data.accounts]
    users = [{"id": u.id, "account_id": u.account_id, "display_name": u.display_name, "role": u.role,
              "pin_hmac": pin_hmac(pepper, u.account_id, pins[(u.account_id, u.display_name)])} for u in data.users]
    charges = []
    for c in data.charges:
        charged_at = _at(today, c.days_ago, c.time)
        charges.append({
            "id": c.id, "account_id": c.account_id, "charged_at": charged_at, "amount_cents": c.amount_cents,
            "description": c.description, "status": c.status,
            "invoice_number": f"INV-{charged_at.year}-{c.id[-4:]}" if c.invoice else None,
            "refunded_at": _at(today, c.refunded_days_ago, "09:30") if c.refunded_days_ago is not None else None})
    history = [{"account_id": h.account_id, "user_id": h.user_id, "action_type": "refund",
                "target_key": f"refund:{h.charge_id}",
                "amount_cents": next(c.amount_cents for c in data.charges if c.id == h.charge_id),
                "effective_date": today - timedelta(days=h.days_ago),
                "committed_at": _at(today, h.days_ago, "09:30"), "committed_by": h.committed_by}
               for h in data.history]
    return PlannedRows(accounts, users, charges, history, today.isoformat())


def apply_seed(conn: Any, rows: PlannedRows, clock: Clock, *, reseed: bool) -> bool:
    """What: write the planned rows inside the caller's transaction. Use when: the CLI (as core_owner) and tests.
    Inputs: connection, rows, clock, reseed flag. Output: True if rows were written, False if the schema already
    had accounts and reseed was off (nothing changes, so a repeated deploy never wipes a demo in progress).
    Side effects: with reseed, TRUNCATE of every core table except audit_events (needs the owner role); INSERTs;
    upserts core.meta seed_date; one audit row (`seed` or `reseed`, counts only). No commit.
    Role: the single writer of seed data."""
    if reseed:
        conn.execute(f"TRUNCATE {_RESET_TABLES}")
    elif conn.execute("SELECT count(*) FROM accounts").fetchone()[0] > 0:
        return False
    for a in rows.accounts:
        conn.execute("INSERT INTO accounts (id, name, tier, contract_status, seats, billing_email) "
                     "VALUES (%(id)s, %(name)s, %(tier)s, %(contract_status)s, %(seats)s, %(billing_email)s)", a)
    for u in rows.users:
        conn.execute("INSERT INTO users (id, account_id, display_name, role, pin_hmac) "
                     "VALUES (%(id)s, %(account_id)s, %(display_name)s, %(role)s, %(pin_hmac)s)", u)
    for c in rows.charges:
        conn.execute("INSERT INTO charges (id, account_id, invoice_number, charged_at, amount_cents, description, "
                     "status, refunded_at) VALUES (%(id)s, %(account_id)s, %(invoice_number)s, %(charged_at)s, "
                     "%(amount_cents)s, %(description)s, %(status)s, %(refunded_at)s)", c)
    for h in rows.history:
        conn.execute("INSERT INTO action_history (account_id, user_id, action_type, target_key, amount_cents, "
                     "effective_date, committed_at, committed_by) VALUES (%(account_id)s, %(user_id)s, "
                     "%(action_type)s, %(target_key)s, %(amount_cents)s, %(effective_date)s, %(committed_at)s, "
                     "%(committed_by)s)", h)
    conn.execute("INSERT INTO meta (key, value) VALUES ('seed_date', %s) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (rows.seed_date,))
    write_audit(conn, AuditEvent(event="reseed" if reseed else "seed", actor="system", mode="system",
                                 args={"accounts": len(rows.accounts), "users": len(rows.users),
                                       "charges": len(rows.charges), "history": len(rows.history),
                                       "seed_date": rows.seed_date}), clock=clock)
    return True


def run_seed(owner_url: str, *, clock: Clock, pepper: str, seeds_dir: Path | str | None, reseed: bool) -> bool:
    """What: connect as core_owner, plan and apply the seeds in one transaction. Use when: the CLI.
    Inputs: owner database URL, clock, pepper, seeds directory, reseed flag. Output: whether rows were written.
    Side effects: one committed transaction (all or nothing). Role: the operational entry point."""
    import psycopg

    rows = plan_rows(load_seed_data(seeds_dir), clock.now(), pepper)
    with psycopg.connect(owner_url) as conn:
        written = apply_seed(conn, rows, clock, reseed=reseed)
        conn.commit()
    return written
