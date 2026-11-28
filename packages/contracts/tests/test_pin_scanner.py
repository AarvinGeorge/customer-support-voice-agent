"""What: tests for the doc 07 s11.1 free-text PIN scanner (scan_pins, scan_text, redact) and the LangSmith hide
function (hide_for_langsmith).
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: every scanner condition, the spoken forms, the kept cases (amounts, dates, invoice endings, error codes), the
credential-looking flag that drives the s7.3 refusal, and the second net for traces."""
import pytest

from contracts.redact import PIN_TOKEN, RedactContext, hide_for_langsmith, normalize_spoken_digits, redact, \
    scan_pins, scan_text

UNVERIFIED = RedactContext()
VERIFIED = RedactContext(verified=True)
AWAITING = RedactContext(awaiting_verification=True)
STEP_UP = RedactContext(verified=True, step_up_pending=True)


@pytest.mark.parametrize(("text", "ctx", "expected"), [
    ("QS-10423 4821", UNVERIFIED, "QS-10423 [PIN]"),  # condition 3: first turn, account id in the same message
    ("I'm QS-10423, 4821, I was charged twice", UNVERIFIED, "I'm QS-10423, [PIN], I was charged twice"),
    ("4821 is my PIN", VERIFIED, "[PIN] is my PIN"),  # condition 2: the word PIN within 3 tokens
    ("my pin number is 4821", VERIFIED, "my pin number is [PIN]"),
    ("PIN:4821", VERIFIED, "PIN:[PIN]"),
    ("yes 4821", STEP_UP, "yes [PIN]"),  # condition 4
    ("4821", AWAITING, "[PIN]"),  # condition 1 (and 5)
    ("4821", VERIFIED, "[PIN]"),  # condition 5: only the candidate
    ("ok 4821 thanks", VERIFIED, "ok [PIN] thanks"),  # condition 5 with fillers
    ("invoice ending 0412", AWAITING, "invoice ending [PIN]"),  # condition 1 redacts every candidate
    ("qs10423 4821", UNVERIFIED, "qs10423 [PIN]"),  # account id without the hyphen, any case
])
def test_each_condition_redacts(text: str, ctx: RedactContext, expected: str) -> None:
    assert "4821" in text or "0412" in text  # the secret is in the fixture (a zero is possible)
    assert scan_pins(text, ctx).text == expected


@pytest.mark.parametrize(("text", "ctx"), [
    ("invoice ending 0412", VERIFIED),  # the kept case of s11.1
    ("ticket ending 8213 please", VERIFIED),
    ("I was charged $1200 twice", AWAITING),  # amount
    ("I was charged $ 1200 twice", AWAITING),
    ("the charge of 1200.00 USD", AWAITING),  # decimal amount
    ("on 2026-11-02 I saw it", AWAITING),  # ISO date
    ("on 11/02/2026 I saw it", AWAITING),  # slash date
    ("since March 2026 and Nov 3, 2026", AWAITING),  # month-name dates
    ("error E-SYNC-042 again", AWAITING),  # error code (three digits, never a candidate)
    ("my account is QS-10423", UNVERIFIED),  # the account id itself is kept (doc 07 s11)
    ("the 12345 order", AWAITING),  # five digits is not a candidate
    ("invoice 0412 for QS-10423", VERIFIED),  # round 2: verified, the account id alone is not context
])
def test_kept_cases(text: str, ctx: RedactContext) -> None:
    scan = scan_pins(text, ctx)
    assert scan.text == text and scan.redacted == 0 and not scan.credential_like


@pytest.mark.parametrize(("spoken", "normal"), [
    ("four eight two one", "4821"),
    ("Four, eight, two, one.", "4821."),
    ("4 8 2 1", "4821"),
    ("four-eight-two-one", "4821"),
    ("Q S one zero four two three", "QS-10423"),
    ("q s one oh four two three", "QS-10423"),
    ("QS one zero four two three four eight two one", "QS-10423 4821"),
])
def test_spoken_digits_are_normalized(spoken: str, normal: str) -> None:
    assert normalize_spoken_digits(spoken) == normal


@pytest.mark.parametrize("text", ["I have one or two questions", "oh no", "one moment", "two three times",
                                  "I bought 2 seats and 3 more"])
def test_ordinary_number_words_are_left_alone(text: str) -> None:
    assert normalize_spoken_digits(text) == text


def test_spoken_pin_in_a_voice_transcript_is_redacted() -> None:
    raw = "Q S one zero four two three, my PIN is four eight two one"
    out = scan_pins(raw, AWAITING)
    assert out.text == "QS-10423, my PIN is [PIN]" and out.redacted == 1 and out.credential_like


@pytest.mark.parametrize(("text", "ctx", "credential"), [
    ("QS-10423 4821", UNVERIFIED, True),  # account id pattern
    ("4821 is my PIN", VERIFIED, True),  # the word PIN
    ("4821", AWAITING, True),  # digits only
    ("ok 4821", VERIFIED, True),
    ("invoice ending 0412", AWAITING, False),  # redacted, but goes to the model (s7.3)
    ("hello there", AWAITING, False),
])
def test_credential_looking_flag(text: str, ctx: RedactContext, credential: bool) -> None:
    assert scan_pins(text, ctx).credential_like is credential


def test_scan_text_and_redact_apply_the_scanner_and_email_mask() -> None:
    raw = {"note": "QS-10423 4821, mail priya@acme.com", "items": ["my PIN is 4821"]}
    assert "4821" in repr(raw) and "priya@acme.com" in repr(raw)
    out = redact(raw, UNVERIFIED)
    assert "4821" not in repr(out) and "priya@acme.com" not in repr(out)
    assert out["note"] == f"QS-10423 {PIN_TOKEN}, mail p***@acme.com"
    assert scan_text("invoice ending 0412", VERIFIED) == "invoice ending 0412"


class Opaque:
    """What: an object redact cannot walk (stands in for a LangChain run object in a trace payload).
    Use when: the hide function fallback test. Inputs: none. Output: object. Side effects: none. Role: fixture."""

    def __repr__(self) -> str:
        """What: a repr that carries the secret. Use when: str() fallback. Inputs: none. Output: text.
        Side effects: none. Role: fixture."""
        return "Opaque(text='QS-10423 4821')"


def test_hide_for_langsmith_redacts_any_payload_shape() -> None:
    payload = {"messages": [[{"type": "human", "content": "QS-10423 4821"}]], "pin": "7302",
               "obj": Opaque(), "email": "priya@acme.com"}
    assert "4821" in repr(payload) and "7302" in repr(payload)
    out = hide_for_langsmith(payload)
    text = repr(out)
    assert isinstance(out, dict)
    assert "4821" not in text and "7302" not in text and "priya@acme.com" not in text
    assert "QS-10423" in text  # the account id is kept


def test_hide_for_langsmith_never_raises_and_never_passes_raw() -> None:
    class Broken:
        """What: an object whose repr fails. Use when: this test. Inputs: none. Output: object.
        Side effects: none. Role: fixture."""

        def __repr__(self) -> str:
            """What: raise. Use when: repr. Inputs: none. Output: none. Side effects: raises. Role: fixture."""
            raise RuntimeError("no repr")

    out = hide_for_langsmith({"x": Broken(), "msg": "my PIN is 4821"})
    assert "4821" not in repr(out)
