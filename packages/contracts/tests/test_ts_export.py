"""What: tests that ts/contracts.ts is generated from the Python models and up to date.
Use when: CI (contract sync check). Inputs: none. Output: pass or fail. Side effects: none.
Role: keeps the web lane's types identical to the Python contract."""
from conftest import PKG_ROOT

from contracts.ts_export import render_ts

TS_FILE = PKG_ROOT / "ts" / "contracts.ts"


def test_generated_file_is_up_to_date():
    """What: the committed file equals a fresh render. Use when: CI. Inputs: none.
    Output: assertion with the regenerate command. Side effects: none. Role: sync gate."""
    assert TS_FILE.read_text() == render_ts(), (
        "ts/contracts.ts is stale: run `uv run python scripts/gen_ts.py` in packages/contracts")


def test_key_types_present():
    """What: the export carries the event union, requests, limits and version.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: coverage."""
    ts = render_ts()
    for needle in ("export type SseEvent =", "export interface TurnRequest",
                   "export interface TurnCompletedEvent", "export interface ErrorEnvelope",
                   'export const CONTRACT_VERSION = "2026-11-26.r4"', "export const LIMITS",
                   "export const UNTRUSTED_WRAPPER_TAGS", '"class": ', "export type EndReason"):
        assert needle in ts, needle
    assert "\u2014" not in ts
