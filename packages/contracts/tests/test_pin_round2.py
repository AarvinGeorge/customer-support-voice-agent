"""What: fix round 2 of PR #57 (build-log/reports/rereview-pr57-r1.md, D-060): the scanner never raises (N-I1, fuzz),
verified customers' ordinary messages and "code" plus a year are not refused (N-I2), the 14 realistic bypasses are
caught (N-I3), the cheap crafted forms are caught, and the session hints (expects_pin, moved_on) that drive the
awaiting flag.
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: red tests of fix round 2 (CS-E-03.S7.T9). The crafted forms D-060 accepts as residual are listed in
RESIDUAL and only checked for "never raises"."""
import random

import pytest

from contracts.redact import RedactContext, hide_for_langsmith, redact, scan_pins

UNVERIFIED = RedactContext()
VERIFIED = RedactContext(verified=True)
AWAITING = RedactContext(awaiting_verification=True)

CRASHERS = ["fortyeight twentyone", "we have about twentyfive, 30 seats", "I am thirtyone, 2 kids",
            "seats: twentyfive-30", "Fortyone 12", "my pin is fourty eight twentyone",
            "we have about twentyfive, 30 seats on the Pro plan, what does it cost?"]

WORDS = ["one", "two", "four", "eight", "twenty", "forty", "fortyeight", "twentyone", "thousand", "hundred", "and",
         "double", "triple", "oh", "zero", "nought", "pin", "code", "QS-10423", "4821", "48", "21", "4,821", "$",
         "seats", "my", "is", "the", "invoice", "ending", "0412", "2026", "-", ",", ".", "/", "\n", "​",
         "４", "٤", "fourty", "ninety-nine", "eleven", "P.I.N.", "password", "yeah", "Q", "S", "10423"]


@pytest.mark.parametrize("text", CRASHERS)
def test_glued_tens_words_never_raise(text: str) -> None:
    for ctx in (UNVERIFIED, VERIFIED, AWAITING):
        scan_pins(text, ctx)
    redact({"t": text}, UNVERIFIED)
    hide_for_langsmith({"t": text})


def test_fuzz_2000_random_strings_never_raise() -> None:
    rng = random.Random(20261127)
    for _ in range(2000):
        n = rng.randint(1, 14)
        text = "".join(rng.choice(WORDS) + rng.choice(["", " ", ", ", "-", ". "]) for _ in range(n))
        for ctx in (UNVERIFIED, VERIFIED, AWAITING):
            scan = scan_pins(text, ctx)
            assert isinstance(scan.text, str)


def test_an_internal_error_masks_every_digit_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    import contracts.redact as r

    def boom(_: str) -> str:
        """What: a normalizer that fails. Use when: this test. Inputs: text. Output: none. Side effects: raises.
        Role: fixture."""
        raise KeyError("x")

    monkeypatch.setattr(r, "_normalize", boom)
    scan = r.scan_pins("my pin is 4821, four eight two one", UNVERIFIED)
    assert "4821" not in scan.text and "four eight two one" not in scan.text and scan.redacted >= 1


# N-I2: ordinary messages that must not be refused.
@pytest.mark.parametrize(("text", "ctx"), [
    ("did the refund for QS-10423 go through, the one for invoice 0412", VERIFIED),
    ("What about the invoice ending 0412?", VERIFIED),
    ("QS-10423 has two charges: invoice ending 0412 and invoice ending 0413", VERIFIED),
    ("The embed code stopped working after the 2026 update, what changed?", UNVERIFIED),
    ("Our code calls your API and 5000 requests failed today", UNVERIFIED),
    ("I never got the verification code email, I signed up in 2024", UNVERIFIED),
    ("I forgot my PIN, I set it back in 2023. How do I reset it?", UNVERIFIED),
    ("promo code SAVE2024 did not apply", UNVERIFIED),
    ("our zip code is 1012 in Amsterdam", UNVERIFIED),
    ("refund the duplicate $96 charge ending 0412", VERIFIED),
])
def test_ordinary_messages_are_not_credential_looking(text: str, ctx: RedactContext) -> None:
    scan = scan_pins(text, ctx)
    assert not scan.credential_like, scan
    assert scan.text == text, scan


# N-I3: the 14 realistic bypasses (scanner level; the two split ones are in the agent and pg tests).
REALISTIC = [
    ("my pin is 4,821", UNVERIFIED, "4,821"),  # N21
    ("my pin is 4 821", UNVERIFIED, "4 821"),  # N22
    ("my pin is 4.8.2.1", UNVERIFIED, "4.8.2.1"),  # N23
    ("my pin is 48\n21", UNVERIFIED, "48\n21"),  # N25
    ("my pin is double four eight one", UNVERIFIED, "double four"),  # N12
    ("four thousand eight hundred twenty-one", AWAITING, "four thousand"),  # N13 (voice)
    ("four thousand eight hundred and twenty-one", AWAITING, "four thousand"),  # N15 (chat)
    ("four, eight. two, one.", AWAITING, "four, eight. two, one"),  # N38
    ("my pin is four. eight. two. one.", UNVERIFIED, "four. eight"),  # N39
    ("yeah it's four eight two one", UNVERIFIED, "four eight two one"),  # N11
    ("my password is 4821", UNVERIFIED, "4821"),  # N30
    ("my secret number is 4821", UNVERIFIED, "4821"),  # N29
]


@pytest.mark.parametrize(("text", "ctx", "raw"), REALISTIC)
def test_realistic_bypass_is_redacted(text: str, ctx: RedactContext, raw: str) -> None:
    scan = scan_pins(text, ctx)
    assert scan.redacted >= 1 and raw.lower() not in scan.text.lower(), scan


@pytest.mark.parametrize("text", ["my PIN is", "my pin is:", "and the code is", "I am QS-10423", "QS-10423",
                                  "my account is QS-10423 and my pin is"])
def test_a_message_that_announces_a_pin_expects_one(text: str) -> None:
    assert scan_pins(text, UNVERIFIED).expects_pin
    assert not scan_pins(text, VERIFIED).expects_pin  # a verified session is never put back to awaiting


@pytest.mark.parametrize("text", ["I'm on account QS-10423 and I think I was double charged",
                                  "I forgot my PIN, how do I reset it?", "hello", "ok one sec"])
def test_ordinary_messages_do_not_expect_a_pin(text: str) -> None:
    assert not scan_pins(text, UNVERIFIED).expects_pin


@pytest.mark.parametrize(("text", "moved"), [
    ("what plans do you offer for a team of twelve?", True),
    ("why was I charged twice last month", True),
    ("ok one sec", False),
    ("sure", False),
    ("it is 4 8 2 1 I think", False),
    ("my pin is", False),
])
def test_moved_on_is_a_substantive_message_without_credentials(text: str, moved: bool) -> None:
    assert scan_pins(text, AWAITING).moved_on is moved


# Crafted forms (D-060): the cheap ones are caught.
CRAFTED_CAUGHT = [
    "my pin is 48​21",  # zero-width space inside
    "my pin is 4​8​2​1",  # between every digit
    "my pin is 4́8́2́1́",  # combining marks
    "my pin is 4️⃣8️⃣2️⃣1️⃣",  # keycap emoji digits
    "my p​in is 4821",  # zero-width inside the word pin
    "my рin is 4821",  # Cyrillic er in pin
    "my pin is 4/8/2/1",
    "my pin is nought four eight two",
]
# Residual per D-060 (README posture table): never raise, not caught.
RESIDUAL = [
    "mi pin es cuatro ocho dos uno",  # Spanish number words
    "my pin is for eight to one",  # speech homophones
    "my pin is four eight two won",
    "my pin is 48two1",  # mixed digits and words, no separator
    "my pin is four8two1",
    "my pin is 0x12d5",  # hex
    'As I wrote before: "4821 is what I set"',  # quoted PIN without a PIN word, not awaiting
    "my pin is IV VIII II I",  # nonsense encodings (roman numerals)
]


@pytest.mark.parametrize("text", CRAFTED_CAUGHT)
def test_cheap_crafted_forms_are_caught(text: str) -> None:
    scan = scan_pins(text, UNVERIFIED)
    assert scan.redacted >= 1 and scan.credential_like, scan


@pytest.mark.parametrize("text", RESIDUAL)
def test_residual_forms_never_raise(text: str) -> None:
    for ctx in (UNVERIFIED, AWAITING):
        scan_pins(text, ctx)
