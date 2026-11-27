"""What: unit tests (no database) for the seed CLI guards: DEMO_RESEED, missing pepper, missing owner URL.
Use when: changing core/seeds/__main__.py. Inputs: none. Output: pytest results. Side effects: none.
Role: CS-E-09.S4.T1 'reseed CLI behind DEMO_RESEED' (doc 07 s3.6: refuses unless DEMO_RESEED is set)."""
import pytest

from core.seeds.__main__ import main

GOOD = {"CORE_DB_OWNER_URL": "postgresql://nowhere.invalid:1/x", "PIN_PEPPER": "p"}


def test_reseed_refused_unless_demo_reseed_is_on(capsys):
    """What: --reseed exits 3 and prints a refusal when DEMO_RESEED is unset, false or 0, before any connection
    (the URL points at an invalid host, so reaching it would raise). Use when: always. Inputs: capsys.
    Output: assertions. Side effects: none. Role: a wipe must be an explicit act."""
    for value in (None, "false", "0", ""):
        env = dict(GOOD) | ({"DEMO_RESEED": value} if value is not None else {})
        assert main(["--reseed"], env) == 3
    assert "DEMO_RESEED" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["CORE_DB_OWNER_URL", "PIN_PEPPER"])
def test_missing_config_fails_loudly(missing, capsys):
    """What: a missing owner URL or pepper exits 2 naming the variable. Use when: always. Inputs: missing, capsys.
    Output: assertions. Side effects: none. Role: no half-configured seeding (an empty pepper would hash every PIN
    with a known key)."""
    env = {k: v for k, v in GOOD.items() if k != missing}
    assert main([], env) == 2
    assert missing in capsys.readouterr().err


def test_reseed_flag_values_that_enable_it(monkeypatch):
    """What: DEMO_RESEED values "1", "true" and "TRUE" pass the guard (the connection then fails on the invalid
    host, which proves the guard let it through). Use when: always. Inputs: monkeypatch. Output: assertions.
    Side effects: one failed connection attempt. Role: doc 07 s15.2 allows true; s3.6 says 1."""
    import core.seeds.__main__ as cli

    reached = []
    monkeypatch.setattr(cli, "run_seed", lambda *a, **k: reached.append(k.get("reseed")) or True)
    for value in ("1", "true", "TRUE"):
        assert main(["--reseed"], dict(GOOD, DEMO_RESEED=value)) == 0
    assert reached == [True, True, True]
