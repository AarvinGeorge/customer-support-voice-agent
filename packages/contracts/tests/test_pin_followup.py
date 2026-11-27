"""What: the PR #57 follow-up (build-log/reports/rereview-pr57-r2.md): misspelled and joined "pin" (P01, P02 and the
listed variants), filled pauses and commas inside spoken digit runs (P13), the two false refusals, and "I forgot my
PIN" not expecting a PIN.
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: red tests of the follow-up (CS-E-03.S7.T9)."""
import pytest

from contracts.redact import RedactContext, scan_pins

UNVERIFIED = RedactContext()
VERIFIED = RedactContext(verified=True)

LEAKS = [
    ("my pni is 4821", "4821"),  # P01
    ("mypin is 4821", "4821"),  # P02
    ("my pin is four eight, uh, two one", "four eight"),  # P13 (voice)
    ("my pim is 4821", "4821"),
    ("my pinn is 4821", "4821"),
    ("pin# 4821", "4821"),
    ("my pn is 4821", "4821"),
    ("my pin number is four, um, eight two one", "eight two one"),
    ("my pin is four eight er two one", "four eight"),
    ("my pin is like four eight two one", "four eight two one"),
    ("my pin is four, eight, uh, two, one", "four, eight"),
    ("mypinis 4821", "4821"),
    ("MyPIN: 4821", "4821"),
]


@pytest.mark.parametrize(("text", "raw"), LEAKS)
def test_misspelled_pin_and_spoken_fillers_are_caught(text: str, raw: str) -> None:
    for ctx in (UNVERIFIED, VERIFIED):
        scan = scan_pins(text, ctx)
        assert scan.redacted >= 1 and raw not in scan.text and scan.credential_like, (ctx, scan)


@pytest.mark.parametrize(("text", "ctx"), [
    ("My account is QS-20318 and we need to go from 1200 to 1500 seats", UNVERIFIED),
    ("The code 4012 keeps showing up when I export", VERIFIED),
    ("we have about 1200 users on QS-20318", UNVERIFIED),
    ("pinning 4821 to the top of the board", UNVERIFIED),  # "pinning" is not "pin"
    ("the spinner shows 4821 items", UNVERIFIED),
])
def test_false_refusals_are_gone(text: str, ctx: RedactContext) -> None:
    scan = scan_pins(text, ctx)
    assert not scan.credential_like and scan.text == text, scan


@pytest.mark.parametrize("text", ["I forgot my PIN", "I forgot my pin, how do I reset it", "where do I enter my PIN",
                                  "how do I change my PIN", "I lost my PIN"])
def test_asking_about_the_pin_does_not_expect_one(text: str) -> None:
    assert not scan_pins(text, UNVERIFIED).expects_pin


def test_announcing_the_pin_still_expects_one() -> None:
    assert scan_pins("my PIN is", UNVERIFIED).expects_pin
    assert scan_pins("here's my pin", UNVERIFIED).expects_pin
