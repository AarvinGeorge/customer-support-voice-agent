"""What: the PR #57 review bypasses (build-log/reports/review-pr57.md I1, I2, M1, M2, M6) and the fix round 1 design
rule: when an account id or the word PIN, code or passcode is in a message, every 4-digit sequence in it (digits,
digit words, paired number words, grouped digits) is a PIN candidate and the message is credential-looking; without
that context amounts, dates, invoice and ticket endings keep working.
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: red tests of fix round 1 (CS-E-03.S7.T9); every bypass string is also pushed through the pg sink test."""
import pytest

from contracts.redact import RedactContext, scan_pins

UNVERIFIED = RedactContext()
VERIFIED = RedactContext(verified=True)
AWAITING = RedactContext(awaiting_verification=True)

# The review's 13 end-to-end bypasses first, then the rest of its list, then my own.
BYPASSES = [
    "QS-10423,4821",  # I1
    "QS-10423.4821",  # I1
    "my PIN is four eight two one. One more thing",  # I2
    "oh, four eight two one is my pin",  # I2
    "I have 2, four eight two one is my PIN",  # I2
    "forty-eight twenty-one is my pin",  # I2
    "my pin is 48 21",  # I2
    "my PIN is ４８２１",  # M1 full-width
    "my PIN is ٤٨٢١",  # M1 Arabic-Indic
    "pin4821",  # M2
    "QS - 10423 4821",  # M2
    "my passcode is 4821",  # M2
    "the code is 4821",  # M2
    # rest of the review list
    "QS–10423 4821", "QS_10423 4821", "QS#10423 4821", "pincode 4821", "my P.I.N. is 4821",
    "my PIN, which I set up last year when I first opened the account, is 4821", "my pin is $4821",
    "PIN: 4821", "PIN:4821", "_4821_ is my pin",
    # account id joined to the PIN by any separator, either order
    "QS-10423/4821", "QS-10423:4821", "QS-10423-4821", "QS-104234821", "QS-10423   4821", "QS-10423\t4821",
    "4821,QS-10423", "4821.QS-10423", "4821QS-10423", "4821 QS-10423", "4821 / QS-10423", "qs10423,4821",
    # spoken and split forms with context
    "QS-10423 forty-eight twenty-one", "QS-10423 forty eight, twenty one", "QS-10423 48 21", "QS-10423 4-8-2-1",
    "my pin is 4-8-2-1", "my pin is forty eight twenty one", "Customer QS-10423: pin is four, eight, two, one.",
    "my PIN is four eight two one one", "my pin is four eight two oh", "code: 4 8 2 1",
    "Q S one zero four two three four eight two one", "my PIN is 4821 and my account is QS-10423",
    "pin number four-eight-two-one", "my passcode: forty eight twenty one",
]


@pytest.mark.parametrize("text", BYPASSES)
def test_bypass_is_redacted_and_credential_looking(text: str) -> None:
    """What: each bypass is caught for every session state (verified: the number is next to the account id or a PIN
    word is present). Use when: CI. Inputs: text. Output: assertion. Side effects: none. Role: round 1 red tests."""
    for ctx in (UNVERIFIED, VERIFIED, AWAITING):
        scan = scan_pins(text, ctx)
        assert scan.redacted >= 1, (text, ctx, scan.text)
        assert scan.credential_like, (text, ctx)
        for raw in ("4821", "48 21", "4-8-2-1", "four eight two one", "four, eight, two, one", "forty-eight",
                    "forty eight", "twenty-one", "twenty one", "４８２１", "٤٨٢١"):
            assert raw not in scan.text.lower(), (text, scan.text)


def test_awaiting_catches_a_pin_without_context_words() -> None:
    scan = scan_pins("it is 4 8 2 1 I think", AWAITING)
    assert scan.text == "it is [PIN] I think" and not scan.credential_like  # redacted, goes to the model


@pytest.mark.parametrize(("text", "ctx"), [
    ("I was charged $4821 twice", AWAITING),
    ("I was charged $1200 twice", UNVERIFIED),
    ("I paid 1200.00 on 2026-11-02", AWAITING),
    ("on March 3, 2026 I paid", AWAITING),
    ("since 11/02/2026", AWAITING),
    ("invoice ending 0412", VERIFIED),
    ("invoice ending 0412", UNVERIFIED),
    ("ticket ending 8213", VERIFIED),
    ("my invoices 0412 and 0413 both show it", VERIFIED),
    ("error code E-SYNC-042 since 2026-11-02", AWAITING),
    ("I got error code E-SYNC-042 on invoice 0412", VERIFIED),
    ("forty-eight twenty-one people saw it", VERIFIED),
    ("I have one or two questions", AWAITING),
    ("we have 10 20 seats", VERIFIED),
    ("the 12345 order", AWAITING),
    ("my account is QS-10423", UNVERIFIED),
    ("hello there", AWAITING),
])
def test_without_context_amounts_dates_and_endings_are_kept(text: str, ctx: RedactContext) -> None:
    scan = scan_pins(text, ctx)
    assert scan.text == text and scan.redacted == 0 and not scan.credential_like


def test_context_makes_every_4_digit_group_a_candidate() -> None:
    """What: a PIN word makes every group a candidate; an account id (unverified) only a number next to it, separated
    by punctuation or a short joining word (narrowed in the PR #57 follow-up, so "QS-20318 ... from 1200 to 1500 seats"
    is kept). Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: context rule."""
    scan = scan_pins("my pin: invoice 0412 for 4821", UNVERIFIED)
    assert scan.text == "my pin: invoice [PIN] for [PIN]" and scan.credential_like
    for text, want in (("QS-10423 4821", "QS-10423 [PIN]"), ("I'm QS-10423, 4821, I was charged twice",
                                                            "I'm QS-10423, [PIN], I was charged twice"),
                       ("QS-10423 and 4821", "QS-10423 and [PIN]"),
                       ("I am QS-10423 and the number is 4821", "I am QS-10423 and the number is [PIN]"),
                       ("4821 QS-10423", "[PIN] QS-10423")):
        scan = scan_pins(text, UNVERIFIED)
        assert scan.text == want and scan.credential_like, scan
    assert scan_pins("QS-10423 invoice 0412 for 4821", UNVERIFIED).redacted == 0
