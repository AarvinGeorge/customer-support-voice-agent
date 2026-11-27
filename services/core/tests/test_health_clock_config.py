"""What: /healthz, /readyz, the injectable clock and the config guards.
Use when: changing core/app.py, core/clock.py or core/config.py. Inputs: none.
Output: pytest results. Side effects: none. Role: CS-E-03.S13.T1 (doc 07 s3.6, s1.1, tz rule)."""
from datetime import date, datetime, timezone

import pytest
from contracts import VERSION
from contracts.policy import policy_version

from tests.conftest import POLICY
from core.clock import FixedClock, SystemClock
from core.config import ConfigError, Settings


def test_healthz_needs_no_token(client):
    """What: /healthz is {"status":"alive"} without auth. Use when: always. Inputs: client.
    Output: assertions. Side effects: none. Role: Compose healthcheck."""
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "alive"}


def test_readyz_shape(client):
    """What: /readyz carries status, contract_version, policy_version, demo_faults, seed_date, no auth.
    Use when: always. Inputs: client. Output: assertions. Side effects: none. Role: s3.6."""
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ready", "contract_version": VERSION, "policy_version": policy_version(POLICY),
                    "demo_faults": True, "seed_date": "unseeded"}


def test_system_clock_is_new_york_not_utc():
    """What: SystemClock.now() is timezone-aware with the America/New_York zone. Use when: always.
    Inputs: none. Output: assertions. Side effects: reads wall time. Role: business dates are local (doc 07)."""
    now = SystemClock("America/New_York").now()
    assert now.tzinfo is not None and now.tzinfo.key == "America/New_York"
    assert abs((now - datetime.now(timezone.utc)).total_seconds()) < 5


def test_fixed_clock_today_uses_local_date():
    """What: today() is the local date even when UTC is already the next day. Use when: always.
    Inputs: none. Output: assertion. Side effects: none. Role: proves the tz is applied, not decorative."""
    c = FixedClock(datetime(2026, 11, 28, 3, 30, tzinfo=timezone.utc))  # 22:30 on the 27th in New York
    assert c.today() == date(2026, 11, 27)
    c.set(datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc))
    assert c.today() == date(2026, 11, 28)


def test_clock_is_injected_into_the_app(client, clock):
    """What: the app exposes the injected clock via app.state. Use when: always. Inputs: client, clock.
    Output: assertion. Side effects: none. Role: later tasks read time only from here."""
    assert client.app.state.clock is clock


def test_settings_reject_equal_or_empty_tokens():
    """What: duplicate or empty tokens fail at construction (a shared token would erase least privilege).
    Use when: always. Inputs: none. Output: assertions. Side effects: none. Role: s1.4 three distinct tokens."""
    with pytest.raises(ConfigError):
        Settings(token_agent="x", token_commit="x", token_ops="y", db_url=None, demo_faults=False)
    with pytest.raises(ConfigError):
        Settings(token_agent="", token_commit="b", token_ops="c", db_url=None, demo_faults=False)


def test_settings_from_env_requires_everything(monkeypatch):
    """What: from_env fails loudly when a token or CORE_DB_URL is missing. Use when: always.
    Inputs: monkeypatch. Output: assertions. Side effects: env only. Role: no silent half-configured start."""
    for k in ("CORE_TOKEN_AGENT", "CORE_TOKEN_COMMIT", "CORE_TOKEN_OPS", "CORE_DB_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("CORE_TOKEN_AGENT", "a")
    monkeypatch.setenv("CORE_TOKEN_COMMIT", "b")
    monkeypatch.setenv("CORE_TOKEN_OPS", "c")
    monkeypatch.setenv("CORE_DB_URL", "postgresql://x")
    monkeypatch.setenv("DEMO_FAULTS_ENABLED", "false")
    s = Settings.from_env()
    assert (s.token_agent, s.demo_faults, s.db_url) == ("a", False, "postgresql://x")
