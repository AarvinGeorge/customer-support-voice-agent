"""What: spot checks of the limits table (doc 07 s8.1).
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: guards constants every service imports."""
from contracts import limits


def test_turn_budgets():
    """What: deadlines, reserve and lock TTL. Use when: CI. Inputs: none. Output: assertion.
    Side effects: none. Role: s8.1 guard."""
    assert limits.CHAT_TURN_DEADLINE_S == 45
    assert limits.VOICE_TURN_DEADLINE_S == 20
    assert limits.ESCALATION_RESERVE_S == 3
    assert limits.TURN_LOCK_TTL_S == 60
    assert limits.BROWSER_VOICE_RELAY_S == 25


def test_per_turn_counts():
    """What: model, tool, search and turn caps. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: s8.1 guard."""
    assert (limits.MODEL_CALLS_PER_TURN, limits.TOOL_CALLS_PER_TURN,
            limits.SEARCH_KB_PER_TURN, limits.TURNS_PER_SESSION) == (6, 8, 3, 40)
    assert limits.MESSAGE_MAX_CHARS == 2000


def test_export_dict_is_plain():
    """What: as_dict exposes every UPPER constant for the TS mirror. Use when: CI.
    Inputs: none. Output: assertion. Side effects: none. Role: TS export source."""
    d = limits.as_dict()
    assert d["CHAT_TURN_DEADLINE_S"] == 45
    assert all(isinstance(v, (int, float)) for v in d.values())
