"""What: the frozen PolicyConfig models, the YAML loader and the policy_version hash for data/policy.yaml.
Use when: core loads the policy at startup and evaluates actions; knowledge fills {policy.key} placeholders
at ingest; tests and evals read limits. Run `python -m contracts.policy <file>` to validate a file.
Inputs: a policy.yaml path (default data/policy.yaml under the repo or POLICY_PATH).
Output: PolicyConfig (frozen, extra=forbid), policy_version string, rendered placeholder text.
Side effects: reads one file; the CLI prints the version or exits 1 with the error on stderr.
Role: doc 07 s13.1 shared config. It lives in packages/contracts, not services/core, because both core and
knowledge import it (s13.1 header: read by core, placeholders filled by knowledge) and contracts is the one
package all three services already depend on; services cannot import each other."""
from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, TypeVar, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (AfterValidator, BaseModel, ConfigDict, PositiveInt, ValidationError,
                      field_validator, model_validator)

from contracts.core_api import ContractStatus, Role, TicketCategory
from contracts.reason_codes import ReasonCode

APPROVAL_QUESTION = "Shall I send this to our team for approval?"
Decision = Literal["allowed", "needs_approval", "declined"]
Priority = Literal["P1", "P2", "P3", "P4"]
Queue = Literal["billing", "technical", "account", "approvals"]
_K = TypeVar("_K")
_V = TypeVar("_V")


class _FrozenDict(dict):
    """What: a dict that refuses every change after construction. Use when: a policy map must be
    read-only yet still serialize and deep-copy. Inputs: mapping. Output: dict subclass.
    Side effects: none. Role: deep freeze; unlike MappingProxyType it dumps to JSON and deep-copies."""

    def _blocked(self, *_args: object, **_kwargs: object) -> None:
        """What: reject a mutation. Use when: any mutating dict method is called. Inputs: any.
        Output: never returns. Side effects: raises TypeError. Role: immutability guard."""
        raise TypeError("policy config is read-only")

    __setitem__ = __delitem__ = update = pop = popitem = clear = setdefault = _blocked  # pyright: ignore[reportAssignmentType]  # blocked mutators are looser than dict signatures on purpose
    __ior__ = _blocked  # pyright: ignore[reportAssignmentType]

    def __reduce__(self):  # type: ignore[no-untyped-def]
        """What: rebuild through the constructor. Use when: copy or deepcopy runs. Inputs: none.
        Output: reduce tuple. Side effects: none. Role: stops copy replaying blocked __setitem__."""
        return (_FrozenDict, (dict(self),))


def _freeze(value: Mapping[_K, _V]) -> Mapping[_K, _V]:
    """What: wrap a parsed dict as a read-only dict. Use when: a policy field must not change
    after load. Inputs: mapping. Output: _FrozenDict copy. Side effects: none.
    Role: deep freeze for dict fields (pydantic frozen blocks attribute assignment only)."""
    return _FrozenDict(value)


FrozenMap = Annotated[dict[_K, _V], AfterValidator(_freeze)]


class PolicyError(Exception):
    """What: the policy file is unreadable or invalid.
    Use when: catching a startup failure. Inputs: message. Output: exception.
    Side effects: none. Role: one error type for every load failure."""


class _Frozen(BaseModel):
    """What: base for every policy model. Use when: subclassing. Inputs: none. Output: base class.
    Side effects: none. Role: extra=forbid so a typo key fails at startup; frozen so it cannot drift."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Roles(_Frozen):
    """What: role sets. Use when: checking who may touch billing. Inputs: lists. Output: model.
    Side effects: none. Role: L1 s2 roles."""

    billing_roles: tuple[Role, ...]
    all_roles: tuple[Role, ...]


class Tier(_Frozen):
    """What: one plan tier. Use when: pricing placeholders. Inputs: price, billing, seats. Output: model.
    Side effects: none. Role: L1 s2 plans."""

    price_cents_per_seat_month: int | None
    billing: str
    seats: tuple[PositiveInt, PositiveInt] | None = None


class Tiers(_Frozen):
    """What: the four plan tiers. Use when: reading prices by tier. Inputs: four Tier. Output: model.
    Side effects: none. Role: L1 s2 plans, doc 07 s13.1 tiers."""

    free: Tier
    pro: Tier
    business: Tier
    enterprise: Tier


class Readback(_Frozen):
    """What: a chat and voice text pair. Use when: building a read-back. Inputs: chat, voice.
    Output: model. Side effects: none. Role: doc 07 s13.1 readback blocks."""

    chat: str
    voice: str


class RefundTier(_Frozen):
    """What: refund rule for one tier. Use when: refund evaluation. Inputs: eligibility and limits.
    Output: model. Side effects: none. Role: L1 s5 A1 table column."""

    eligible: bool
    window_days: PositiveInt | None = None
    late_band_days: PositiveInt | None = None
    auto_cap_cents: PositiveInt | None = None
    approval_only: bool = False


class RefundTiers(_Frozen):
    """What: refund rules per tier. Use when: refund evaluation. Inputs: four tiers. Output: model.
    Side effects: none. Role: L1 s5 A1."""

    free: RefundTier
    pro: RefundTier
    business: RefundTier
    enterprise: RefundTier


class RefundAction(_Frozen):
    """What: A1 refund settings. Use when: refund prepare and commit. Inputs: rules and texts.
    Output: model. Side effects: none. Role: L1 s5 A1."""

    enabled: bool
    repeat_window_days: PositiveInt
    tiers: RefundTiers
    blocked_statuses: tuple[ContractStatus, ...]
    pending_ttl_seconds: PositiveInt
    readback: Readback
    readback_approval: Readback


class GraceTier(_Frozen):
    """What: grace rule for one tier. Use when: A4 evaluation (disabled by default).
    Inputs: days, frequency. Output: model. Side effects: none. Role: L1 s5 A4."""

    eligible: bool
    days: PositiveInt | None = None
    once_per_months: PositiveInt | None = None
    approval_only: bool = False


class GraceTiers(_Frozen):
    """What: grace rules per tier. Use when: A4 evaluation. Inputs: four tiers. Output: model.
    Side effects: none. Role: L1 s5 A4."""

    free: GraceTier
    pro: GraceTier
    business: GraceTier
    enterprise: GraceTier


class GraceAction(_Frozen):
    """What: A4 grace extension settings, enabled false. Use when: A4 prepare if switched on.
    Inputs: rules and texts. Output: model. Side effects: none. Role: L1 s5 A4 (built only if Saturday is green)."""

    enabled: bool
    requires_status: ContractStatus
    tiers: GraceTiers
    pending_ttl_seconds: PositiveInt
    readback: Readback
    readback_approval: Readback


class Actions(_Frozen):
    """What: all action settings. Use when: any action service. Inputs: two blocks. Output: model.
    Side effects: none. Role: doc 07 s13.1 actions. A2 billing email and notification preference
    blocks are designed only (doc 09 s1.2); extra=forbid rejects them until A2 is built."""

    refund: RefundAction
    grace_extension: GraceAction


class AccountLock(_Frozen):
    """What: account lockout rule. Use when: verify. Inputs: failures and minutes. Output: model.
    Side effects: none. Role: doc 07 s3.1 lockout."""

    failures: PositiveInt
    window_minutes: PositiveInt
    lock_minutes: PositiveInt


class Identity(_Frozen):
    """What: PIN and session rules. Use when: verify. Inputs: counts and minutes. Output: model.
    Side effects: none. Role: L1 s10."""

    pin_digits: PositiveInt
    max_failures_per_session: PositiveInt
    account_lock: AccountLock
    session_ttl_minutes: PositiveInt


class TicketLimits(_Frozen):
    """What: ticket caps. Use when: ticket create. Inputs: one int. Output: model.
    Side effects: none. Role: L1 s5 A3 (D-070 removed the unverified per-session cap: no unverified tickets)."""

    verified_per_account_per_day: PositiveInt


class Tickets(_Frozen):
    """What: ticket priority, queue and cap rules. Use when: ticket create. Inputs: maps and lists.
    Output: model. Side effects: none. Role: L1 s5 A3."""

    priority_by_tier: FrozenMap[Literal["free", "pro", "business", "enterprise"], Priority]
    enterprise_blocking_priority: Priority
    target_label: FrozenMap[Literal["P1", "P2_enterprise", "P2", "P3", "P4"], str]
    queues: tuple[Queue, ...]
    category_queue: FrozenMap[TicketCategory, Queue]
    limits: TicketLimits

    @model_validator(mode="after")
    def _queues_known(self) -> Tickets:
        """What: every mapped queue is a listed queue. Use when: validation. Inputs: self.
        Output: self. Side effects: none. Role: catches a queue typo at startup."""
        bad = set(self.category_queue.values()) - set(self.queues)
        if bad:
            raise ValueError(f"category_queue names unknown queues: {sorted(bad)}")
        missing = set(get_args(TicketCategory)) - set(self.category_queue)
        if missing:
            raise ValueError(f"category_queue is missing categories: {sorted(missing)}")
        return self


class PolicyRefCfg(_Frozen):
    """What: an article and section slug. Use when: citing a rule. Inputs: two slugs. Output: model.
    Side effects: none. Role: doc 07 s13.1 policy_ref (title is added by core from the article)."""

    article: str
    section: str


class ReasonCodeCfg(_Frozen):
    """What: one reason code entry. Use when: building a decision. Inputs: decision, ref, message.
    Output: model. Side effects: none. Role: doc 07 s13.1 reason_codes."""

    decision: Decision
    policy_ref: PolicyRefCfg
    message: str | None = None
    followup_queue: Queue | None = None
    requires_step_up: bool = False

    @model_validator(mode="after")
    def _declined_has_message(self) -> ReasonCodeCfg:
        """What: declined codes carry a customer-safe message. Use when: validation. Inputs: self.
        Output: self. Side effects: none. Role: doc 07 s13.1 message rule."""
        if self.decision == "declined" and not self.message:
            raise ValueError("a declined reason code needs a message")
        return self


class PolicyConfig(_Frozen):
    """What: the whole policy file, validated. Use when: core startup, knowledge ingest, tests.
    Inputs: parsed YAML (and policy_version set by load_policy). Output: frozen model.
    Side effects: none. Role: doc 07 s13.1; validators enforce the ReasonCode enum match and the
    readback_approval ending."""

    version_label: str
    timezone: str
    currency: str
    roles: Roles
    tiers: Tiers
    actions: Actions
    identity: Identity
    tickets: Tickets
    reason_codes: FrozenMap[ReasonCode, ReasonCodeCfg]
    policy_version: str = ""

    @field_validator("timezone")
    @classmethod
    def _timezone_known(cls, value: str) -> str:
        """What: the timezone is a real IANA name. Use when: validation. Inputs: the string.
        Output: the string. Side effects: reads the tz database. Role: catches a typo at startup."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _checks(self) -> PolicyConfig:
        """What: reason codes equal the enum; every readback_approval ends with the fixed question.
        Use when: validation. Inputs: self. Output: self. Side effects: none.
        Role: Done-when items of CS-E-09.S2.T1."""
        have, want = {str(c) for c in self.reason_codes}, {c.value for c in ReasonCode}
        if have != want:
            raise ValueError(f"reason_codes differ from ReasonCode: missing {sorted(want - have)}, "
                             f"extra {sorted(have - want)}")
        for name in ("refund", "grace_extension"):
            ra = getattr(self.actions, name).readback_approval
            for channel in ("chat", "voice"):
                if not getattr(ra, channel).endswith(APPROVAL_QUESTION):
                    raise ValueError(f"readback_approval {name}.{channel} must end with: {APPROVAL_QUESTION}")
        return self


class _StrictLoader(yaml.SafeLoader):
    """What: a SafeLoader that rejects duplicate mapping keys. Use when: reading policy.yaml.
    Inputs: YAML text. Output: parsed data. Side effects: none.
    Role: PyYAML keeps the last duplicate silently; a policy limit must never be overridden that way."""

    def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
        """What: build a dict, failing on a repeated key. Use when: the parser meets a mapping.
        Inputs: node, deep flag. Output: dict. Side effects: none. Role: duplicate-key guard."""
        seen: set = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key: {key}", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep)


def _parse(data: bytes) -> object:
    """What: parse policy YAML strictly. Use when: loading or hashing the file. Inputs: bytes.
    Output: parsed data. Side effects: none. Role: one parser, duplicate keys rejected."""
    return yaml.load(data, Loader=_StrictLoader)  # noqa: S506 (SafeLoader subclass)


def default_path() -> Path:
    """What: where policy.yaml lives. Use when: no path is given. Inputs: env POLICY_PATH.
    Output: Path. Side effects: none. Role: one place for the default; containers set POLICY_PATH."""
    env = os.environ.get("POLICY_PATH")
    return Path(env) if env else Path(__file__).resolve().parents[4] / "data" / "policy.yaml"


def policy_version(path: Path | str | None = None) -> str:
    """What: version string "<version_label>+<first 12 hex of sha256 of the file bytes>".
    Use when: /readyz, audit rows, PrepareResult.policy_version. Inputs: path.
    Output: str. Side effects: reads a file. Role: detects any change to the file, even one byte."""
    p = Path(path) if path else default_path()
    data = p.read_bytes()
    label = _parse(data).get("version_label", "unlabelled")  # pyright: ignore[reportAttributeAccessIssue]
    return f"{label}+{hashlib.sha256(data).hexdigest()[:12]}"


def load_policy(path: Path | str | None = None) -> PolicyConfig:
    """What: read, validate and freeze the policy file.
    Use when: service startup (a PolicyError there must stop the process).
    Inputs: path (default data/policy.yaml). Output: PolicyConfig with policy_version set.
    Side effects: reads a file. Role: the single loader both core and knowledge call."""
    p = Path(path) if path else default_path()
    try:
        raw = _parse(p.read_bytes())
        cfg = PolicyConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError, AttributeError) as exc:
        raise PolicyError(f"invalid policy file {p}: {exc}") from exc
    return cfg.model_copy(update={"policy_version": policy_version(p)})


def render_placeholder(cfg: PolicyConfig, expr: str) -> str:
    """What: resolve "dotted.key" or "dotted.key|dollars" or "|days" against the config.
    Use when: knowledge ingest renders {policy.<key>} in an article. Inputs: config, expression
    without the policy. prefix. Output: text ($150 from 15000 cents; "14 days"). Side effects: none.
    Role: doc 07 s13.1 placeholders; a missing key raises PolicyError naming it (ingest fails loudly)."""
    key, _, fmt = expr.partition("|")
    node: object = cfg
    for part in key.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, BaseModel) and part in type(node).model_fields:
            node = getattr(node, part)
        else:
            raise PolicyError(f"unknown policy key: {key}")
    if isinstance(node, (BaseModel, Mapping, list, tuple)) or node is None:
        raise PolicyError(f"policy key is not a scalar: {key}")
    if fmt in ("dollars", "days") and (isinstance(node, bool) or not isinstance(node, int)):
        raise PolicyError(f"|{fmt} needs an integer value: {key}")
    if fmt == "dollars":
        return f"${int(node) // 100:,}" if int(node) % 100 == 0 else f"${int(node) / 100:,.2f}"  # pyright: ignore[reportArgumentType]
    if fmt == "days":
        return f"{node} days"
    if fmt:
        raise PolicyError(f"unknown formatter: {fmt}")
    return str(node)


def main(argv: list[str]) -> int:
    """What: CLI that validates a policy file. Use when: core startup check and CI.
    Inputs: optional path. Output: exit code 0 and "policy_version=..." on stdout, or 1 with the
    error on stderr. Side effects: prints. Role: Done-when 'unknown key makes core exit non-zero'."""
    try:
        cfg = load_policy(argv[1] if len(argv) > 1 else None)
    except PolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"policy_version={cfg.policy_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
