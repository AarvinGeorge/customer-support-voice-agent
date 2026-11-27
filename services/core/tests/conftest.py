"""What: shared fixtures for core unit tests (no database).
Use when: pytest collects services/core/tests. Inputs: none.
Output: fixtures `settings`, `clock`, `client`, `TOKENS`. Side effects: none.
Role: one place that builds the app with fake tokens and a fixed clock."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from core.app import create_app
from core.clock import FixedClock
from core.config import Settings

POLICY = Path(__file__).resolve().parents[3] / "data" / "policy.yaml"
TOKENS = {"agent_tools": "tok-agent", "agent_commit": "tok-commit", "ops": "tok-ops"}


@pytest.fixture
def settings() -> Settings:
    """What: settings with three distinct fake tokens and no database. Use when: building the app.
    Inputs: none. Output: Settings. Side effects: none. Role: unit-test configuration."""
    return Settings(token_agent=TOKENS["agent_tools"], token_commit=TOKENS["agent_commit"],
                    token_ops=TOKENS["ops"], db_url=None, demo_faults=True,
                    policy_path=str(POLICY))


@pytest.fixture
def clock() -> FixedClock:
    """What: a clock fixed at 2026-11-27 09:30 America/New_York. Use when: asserting time-dependent output.
    Inputs: none. Output: FixedClock. Side effects: none. Role: deterministic time."""
    return FixedClock(datetime(2026, 11, 27, 9, 30, tzinfo=ZoneInfo("America/New_York")))


@pytest.fixture
def client(settings: Settings, clock: FixedClock):
    """What: a TestClient on a fresh app with the lifespan run. Use when: calling endpoints.
    Inputs: settings, clock. Output: TestClient. Side effects: loads data/policy.yaml.
    Role: the HTTP-level fixture for T1 tests."""
    with TestClient(create_app(settings, clock=clock)) as c:
        yield c
