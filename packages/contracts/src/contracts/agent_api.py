"""What: agent public API request and response shapes (doc 07 s2.2, s2.3) used by the thin slice.
Use when: the agent validates a browser body, or the web app builds one.
Inputs: none.
Output: TurnRequest and its inputs, VoiceAskRequest, VoiceToken, transcript shapes, AccessRequest,
Me, Persona, SessionCreated, VerifiedPerson, AgentReady.
Side effects: none.
Role: the browser-to-agent contract. SessionSnapshot, TurnStatus, TraceDetail, VoiceUsagePost and
operator bodies are designed only (doc 09 s1.2) and left out."""
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from contracts.base import StrictModel
from contracts.core_api import PENDING_ID_PATTERN, ContractStatus, Role, Tier


class ConfirmInput(StrictModel):
    """What: a Confirm or Cancel press on the confirmation card.
    Use when: TurnRequest.confirm. Inputs: pending id, decision, optional step-up PIN.
    Output: model. Side effects: none. Role: doc 07 s2.3 (the PIN never reaches the model)."""

    pending_action_id: str = Field(pattern=PENDING_ID_PATTERN)
    decision: Literal["confirm", "cancel"]
    step_up_pin: str | None = Field(default=None, pattern=r"^[0-9]{4}$")


class VerificationInput(StrictModel):
    """What: the masked verification card values. Use when: TurnRequest.verification.
    Inputs: account_id, pin. Output: model. Side effects: none. Role: doc 07 s2.3."""

    account_id: str = Field(min_length=1, max_length=20)
    pin: str = Field(pattern=r"^[0-9]{4}$")


class VerificationUnavailable(StrictModel):
    """What: the verification card's "I don't have my PIN" press. Use when: TurnRequest.verification, instead of
    the account id and PIN. Inputs: no_pin (only true). Output: model. Side effects: none.
    Role: D-070 forgotten-PIN path as a structured input (no text matching, no model call; PR #78 review I-4)."""

    no_pin: Literal[True]


class TicketOffer(StrictModel):
    """What: the Create ticket button after "both models unavailable".
    Use when: TurnRequest.ticket_offer. Inputs: accept (only true). Output: model.
    Side effects: none. Role: doc 07 s2.3, s6.7."""

    accept: Literal[True]


class TurnRequest(StrictModel):
    """What: body of POST /api/sessions/{sid}/turns.
    Use when: every chat turn. Inputs: client_turn_id and exactly one of text, confirm,
    verification, ticket_offer. Output: model or ValueError (422 VALIDATION_ERROR).
    Side effects: none. Role: doc 07 s2.3 TurnRequest."""

    client_turn_id: UUID
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    confirm: ConfirmInput | None = None
    verification: VerificationInput | VerificationUnavailable | None = None
    ticket_offer: TicketOffer | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "TurnRequest":
        """What: enforce exactly one input kind. Use when: validation. Inputs: self.
        Output: self or ValueError. Side effects: none. Role: doc 07 s2.3 rule."""
        present = [n for n in ("text", "confirm", "verification", "ticket_offer")
                   if getattr(self, n) is not None]
        if len(present) != 1:
            raise ValueError("exactly one of text, confirm, verification, ticket_offer is required")
        return self


class VoiceAskRequest(StrictModel):
    """What: body of POST /api/sessions/{sid}/voice/ask (the ask_support_agent relay).
    Use when: the browser relays a Realtime function call. Inputs: ids, question, transcript fields.
    Output: model. Side effects: none. Role: doc 07 s2.3; question never confirms or verifies."""

    client_turn_id: UUID
    voice_session_id: str
    realtime_call_id: str
    question: str = Field(min_length=1, max_length=2000)
    user_item_id: str | None = None
    user_transcript: str | None = None
    transcript_final: bool
    last_assistant_interrupted: bool


class VoiceToken(StrictModel):
    """What: body of POST /voice/token. Use when: the browser starts a Realtime call.
    Inputs: client secret, expiry, model, voice session id, max seconds. Output: model.
    Side effects: none. Role: doc 07 s2.3 VoiceToken."""

    client_secret: str
    expires_at: str
    model: str
    voice_session_id: str
    max_session_s: int


class TranscriptItemIn(StrictModel):
    """What: one client-reported voice transcript item. Use when: TranscriptPost.items.
    Inputs: item id, role, text, interrupted, client time. Output: model. Side effects: none.
    Role: doc 07 s2.3 (untrusted display text)."""

    realtime_item_id: str
    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)
    interrupted: bool
    client_ts: str


class TranscriptPost(StrictModel):
    """What: body of POST /transcript. Use when: the browser posts voice chatter.
    Inputs: voice session id, 1..50 items. Output: model. Side effects: none. Role: doc 07 s2.3."""

    voice_session_id: str
    items: list[TranscriptItemIn] = Field(min_length=1, max_length=50)


class TranscriptItem(StrictModel):
    """What: one stored transcript item (chat or voice). Use when: GET /transcript.
    Inputs: id, turn, role, mode, source, text, interrupted, time. Output: model.
    Side effects: none. Role: doc 07 s2.3 TranscriptItem."""

    id: str
    turn_id: str | None = None
    role: Literal["user", "assistant"]
    mode: Literal["chat", "voice"]
    source: Literal["server", "client"]
    text: str
    interrupted: bool
    created_at: str


class VerifiedPerson(StrictModel):
    """What: who the session is verified as, for display. Use when: verification.result.
    Inputs: name, role, tier, contract status, account id. Output: model. Side effects: none.
    Role: doc 07 s2.3 VerifiedPerson."""

    display_name: str
    role: Role
    tier: Tier
    contract_status: ContractStatus
    account_id: str


class AccessRequest(StrictModel):
    """What: body of POST /api/access. Use when: the visitor enters the demo code.
    Inputs: code. Output: model. Side effects: none. Role: doc 07 s2.2."""

    code: str = Field(min_length=1, max_length=64)


class Me(StrictModel):
    """What: body of GET /api/me. Use when: the web app boots. Inputs: flags.
    Output: model. Side effects: none. Role: doc 07 s2.2."""

    visitor: Literal[True]
    operator: bool
    demo_faults_enabled: bool


class Persona(StrictModel):
    """What: one demo persona on the landing page. Use when: GET /api/demo/personas.
    Inputs: identity fields, PIN, scenarios, description. Output: model. Side effects: none.
    Role: doc 07 s2.2 (demo PINs are public by design)."""

    name: str
    role: Role
    tier: Tier
    contract_status: ContractStatus
    account_id: str
    pin: str
    scenarios: list[Literal["S1", "S2", "S3", "S4", "S5", "rules"]] = Field(min_length=1, max_length=6)
    description: str = Field(max_length=140)


class SessionCreated(StrictModel):
    """What: body of a 201 from POST /api/sessions. Use when: starting a conversation.
    Inputs: session id, created_at. Output: model. Side effects: none. Role: doc 07 s2.2."""

    session_id: str
    created_at: str


class AgentReady(StrictModel):
    """What: body of GET /api/readyz. Use when: readiness checks.
    Inputs: status, contract version, dependency states, fault flag. Output: model.
    Side effects: none. Role: doc 07 s2.2 last row (dependency values are free strings)."""

    status: str
    contract_version: str
    redis: str
    postgres: str
    core: str
    knowledge: str
    demo_faults: bool
