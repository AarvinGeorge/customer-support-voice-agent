"""What: injectable clocks that return aware datetimes in the policy timezone (America/New_York).
Use when: any core code needs the time or the business date; tests inject FixedClock.
Inputs: an IANA timezone name. Output: Clock, SystemClock, FixedClock.
Side effects: SystemClock reads wall time.
Role: the single time source for core (rules, expiry, audit), so time is deterministic in tests."""
from __future__ import annotations

from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/New_York"


class Clock(Protocol):
    """What: the clock interface. Use when: type hints. Inputs: none. Output: now() and today().
    Side effects: none. Role: lets handlers take any clock."""

    def now(self) -> datetime:
        """What: current time. Use when: stamping. Inputs: none. Output: aware datetime.
        Side effects: none. Role: protocol."""
        ...

    def today(self) -> date:
        """What: current business date. Use when: date rules. Inputs: none. Output: local date.
        Side effects: none. Role: protocol."""
        ...


class SystemClock:
    """What: real time in the given timezone. Use when: production. Inputs: tz name.
    Output: aware datetimes. Side effects: reads wall time. Role: default clock."""

    def __init__(self, tz: str = DEFAULT_TZ) -> None:
        """What: bind the timezone. Use when: constructing. Inputs: IANA name. Output: none.
        Side effects: loads tz data. Role: setup."""
        self._tz = ZoneInfo(tz)

    def now(self) -> datetime:
        """What: now in the zone. Use when: stamping. Inputs: none. Output: aware datetime.
        Side effects: reads wall time. Role: see class."""
        return datetime.now(self._tz)

    def today(self) -> date:
        """What: today's local date. Use when: date rules. Inputs: none. Output: date.
        Side effects: reads wall time. Role: see class."""
        return self.now().date()


class FixedClock:
    """What: a settable clock for tests. Use when: asserting time-dependent output. Inputs: a start datetime
    (aware). Output: that time, converted to the zone, until set(). Side effects: none.
    Role: deterministic time."""

    def __init__(self, start: datetime, tz: str = DEFAULT_TZ) -> None:
        """What: store the start time. Use when: constructing. Inputs: aware datetime, tz name. Output: none.
        Side effects: none. Role: setup; a naive datetime is rejected."""
        if start.tzinfo is None:
            raise ValueError("FixedClock needs an aware datetime")
        self._tz = ZoneInfo(tz)
        self._now = start.astimezone(self._tz)

    def set(self, value: datetime) -> None:
        """What: jump to a time. Use when: a test moves time. Inputs: aware datetime. Output: none.
        Side effects: changes the clock. Role: test control."""
        self._now = value.astimezone(self._tz)

    def now(self) -> datetime:
        """What: the stored time in the zone. Use when: stamping. Inputs: none. Output: aware datetime.
        Side effects: none. Role: see class."""
        return self._now

    def today(self) -> date:
        """What: the stored local date. Use when: date rules. Inputs: none. Output: date.
        Side effects: none. Role: see class."""
        return self._now.date()
