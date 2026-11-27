"""What: renders the recorded SSE fixtures (packages/contracts/fixtures/*.jsonl) from the contract
models and the stub example bodies, so every line is valid by construction and stays in sync.
Use when: scripts/build_fixtures.py writes them; tests/test_fixtures.py checks they are current.
Inputs: none.
Output: render_all() -> {file name: JSONL text}.
Side effects: none.
Role: one S1 chat conversation (4 turns: ask, verify plus prepare, confirm, follow-up answer), one
voice turn, and edge turns (fallback with a skipped grounding gate, tool failure escalation,
deadline path with an error event and the deadline ticket) so lane C's reducer
can assert on every doc 07 s6.2 event type, and the S4 unsure turns (off-topic decline, weak then
clarify, weak again then escalate not_in_docs) for the step trail gate rows."""
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from contracts import VERSION
from contracts.agent_api import VerifiedPerson
from contracts.events import Envelope, parse_event
from contracts.stubs import examples as ex

_T0 = datetime(2026, 11, 27, 13, 0, 0, tzinfo=timezone.utc)
AGENT_MODEL = "claude-sonnet-4-5"
FALLBACK_MODEL = "gpt-4.1-mini"

REFUND_CHUNK = {
    "chunk_id": "refund-policy#2", "label": "Refund policy, Refunds on the Pro plan",
    "ref": "refund-policy#2", "spoken": "our refund policy",
    "url": "https://quillstack.notion.site/Refund-policy#refunds-on-the-pro-plan",
    "section_title": "Refunds on the Pro plan",
    "excerpt": ("Pro customers can get a refund for a duplicate or accidental charge within 30 days. "
                "Refunds up to $150 are issued right away after you confirm. Refunds reach your card "
                "in 5 to 10 business days."),
    "score": 0.71,
}
REFUND_TIMING_TEXT = ("Refunds reach your card in 5 to 10 business days after they are issued. "
                      "Yours was issued today, so expect it by 11 December.")


class _Turn:
    """What: builds one turn's events with seq, ids and timestamps filled in.
    Use when: rendering a fixture turn. Inputs: session id, turn id, mode, start offset.
    Output: list of validated event dicts via .events. Side effects: none. Role: builder helper."""

    def __init__(self, session_id: str, turn_id: str, mode: str, start_s: float) -> None:
        """What: start a turn. Use when: rendering. Inputs: ids, mode, start seconds after T0.
        Output: none. Side effects: none. Role: builder helper."""
        self.session_id, self.turn_id, self.mode = session_id, turn_id, mode
        self.t = _T0 + timedelta(seconds=start_s)
        self.events: list[dict[str, Any]] = []
        self.steps = 0

    def add(self, type_: str, dt_ms: int = 50, **fields: Any) -> None:
        """What: append one event, validated. Use when: rendering. Inputs: type, time step, fields.
        Output: none. Side effects: appends to self.events. Role: builder helper."""
        self.t += timedelta(milliseconds=dt_ms)
        ev = {"v": 1, "type": type_, "seq": len(self.events) + 1, "session_id": self.session_id,
              "turn_id": self.turn_id, "trace_id": self.turn_id,
              "ts": self.t.isoformat().replace("+00:00", "Z"), "mode": self.mode, **fields}
        model: Envelope = parse_event(ev)
        self.events.append(model.model_dump(mode="json", by_alias=True))

    def step(self, kind: str, name: str, label: str, title: str, ms: int, args: dict | None = None,
             detail: str | None = None, status: str = "ok", attempt: int = 1, **extra: Any) -> str:
        """What: a step.started plus step.completed pair. Use when: rendering a step chip.
        Inputs: kind, name, labels, duration, args, detail, status, attempt, extra completed fields.
        Output: the step id. Side effects: appends two events. Role: builder helper."""
        self.steps += 1
        sid = f"{self.turn_id}:{self.steps}"
        self.add("step.started", step_id=sid, kind=kind, name=name, label=label, args_redacted=args or {})
        self.add("step.completed", dt_ms=ms, step_id=sid, status=status, ms=ms, title=title,
                 detail=detail, summary={}, attempt=attempt, **extra)
        return sid

    def model_step(self, ms: int, inp: int, out: int, cache: int = 0, model: str = AGENT_MODEL,
                   status: str = "ok") -> str:
        """What: a model step with token counts. Use when: rendering. Inputs: ms, tokens, model.
        Output: step id. Side effects: appends events. Role: builder helper."""
        return self.step("model", "agent", "Thinking", "Thought it through", ms, model=model,
                         input_tokens=inp, output_tokens=out, cache_read_tokens=cache, status=status)

    def tokens(self, text: str) -> None:
        """What: flush text as ~40-character token events. Use when: Gate 2 passed.
        Inputs: text. Output: none. Side effects: appends events. Role: doc 07 s6.4 flush."""
        for i in range(0, len(text), 40):
            self.add("token", dt_ms=5, text=text[i:i + 40])


def _usage(latency: int, model_calls: int, tool_calls: int, inp: int, out: int, cache: int = 0,
           models: list[str] | None = None, fallback: bool = False, ttft: int | None = None) -> dict:
    """What: a Usage dict. Use when: turn.completed. Inputs: totals. Output: dict.
    Side effects: none. Role: builder helper."""
    return {"input_tokens": inp, "output_tokens": out, "cache_read_tokens": cache, "cache_write_tokens": 0,
            "cost_usd_est": round(inp * 3e-6 + out * 15e-6, 6), "first_status_ms": 40,
            "ttft_ms": ttft if ttft is not None else latency, "latency_ms": latency,
            "model_calls": model_calls, "tool_calls": tool_calls,
            "models_used": models if models is not None else ([AGENT_MODEL] if model_calls else []),
            "fallback_used": fallback}


def _final(text: str, say: str, citations: list | None = None, escalated: bool = False,
           ticket: dict | None = None, pending: dict | None = None, confidence: dict | None = None) -> dict:
    """What: a FinalAnswer dict. Use when: turn.completed. Inputs: answer parts. Output: dict.
    Side effects: none. Role: builder helper."""
    return {"text": text, "say": say, "citations": citations or [], "escalated": escalated,
            "ticket": ticket, "pending_action": pending, "confidence": confidence}


def _search_gate(t: _Turn, query: str, verdict: str = "pass", top: float = 0.71) -> None:
    """What: a search_kb step plus its retrieval gate event. Use when: rendering.
    Inputs: turn, query, verdict, top cosine. Output: none. Side effects: appends events. Role: helper."""
    sid = t.step("tool", "search_kb", "Searching help articles", "Searched help articles", 420,
                 args={"query": query}, detail="3 relevant sections")
    t.add("gate", step_id=sid, gate="retrieval", verdict=verdict, decision="answer", top_cosine=top,
          tau_low=0.30, tau_high=0.45, calibration_status="provisional", code_match=False,
          chunk_ids=["refund-policy#2", "refund-policy#3", "billing-faq#1"])


def _grounding(t: _Turn, claims: list[dict]) -> None:
    """What: status checking_policy plus a passing grounding gate. Use when: rendering.
    Inputs: turn, claims. Output: none. Side effects: appends events. Role: helper."""
    t.add("status", kind="checking_policy", message="Checking the answer against policy...")
    t.add("gate", dt_ms=900, step_id=f"{t.turn_id}:{t.steps}", gate="grounding", verdict="pass",
          decision="answer", chunk_ids=["refund-policy#2"], claims_total=len(claims),
          claims_supported=sum(c["supported"] for c in claims), claims=claims, commitment=False)


def _s1_chat() -> list[dict]:
    """What: the S1 chat conversation, four turns. Use when: rendering s1_chat.jsonl.
    Inputs: none. Output: event dicts. Side effects: none. Role: fixture body."""
    sid = ex.SESSION_ID
    person = VerifiedPerson(display_name=ex.CLAIMS.display_name, role=ex.CLAIMS.role, tier=ex.CLAIMS.tier,
                            contract_status=ex.CLAIMS.contract_status, account_id=ex.ACCOUNT_ID)
    out: list[dict] = []

    t = _Turn(sid, "019a7c2e-5b20-7000-8000-0000000000c1", "chat", 0)
    t.add("turn.started", served_by="agent_a", input_kind="text", contract_version=VERSION)
    t.model_step(1800, 2100, 60)
    _search_gate(t, "refund duplicate charge")
    t.model_step(1500, 3400, 40, cache=2100)
    t.step("tool", "request_verification", "Asking you to verify", "Asked you to verify", 5,
           args={"reason": "action"})
    verify_line = ("To look at your account I need to verify you. Please enter your account ID and "
                   "support PIN in the secure card.")
    t.add("verification.required", reason="action", prompt_text=verify_line, attempts_left=3,
          fields=["account_id", "pin"])
    t.add("turn.completed", final=_final(verify_line, verify_line),
          usage=_usage(3900, 2, 2, 5500, 100, cache=2100), end_reason="verification_requested",
          replayed=False)
    out += t.events

    t = _Turn(sid, "019a7c2e-5b30-7000-8000-0000000000c2", "chat", 30)
    t.add("turn.started", served_by="agent_b", input_kind="verification", contract_version=VERSION)
    t.step("verify", "identity_verify", "Verifying your identity", "Verified identity", 140,
           args={"account_id": ex.ACCOUNT_ID, "pin": "[PIN]"}, detail="Priya Raman, owner, Pro")
    t.add("verification.result", status="verified", person=person.model_dump(mode="json"), attempts_left=None,
          ticket=None)
    t.step("memory", "me_context", "Loading your account", "Loaded your account", 90,
           detail="1 open ticket")
    t.model_step(1600, 3900, 45, cache=3300)
    t.step("tool", "list_charges", "Checking your billing history", "Checked billing history", 180,
           args={"ending": None, "on": "2026-11-21", "since_days": 90}, detail="2 charges of $96.00 on 21 Nov")
    t.model_step(1700, 4600, 55, cache=3900)
    t.step("tool", "prepare_refund", "Preparing your refund", "Prepared refund", 260,
           args={"charge_id": "CH-26112102", "reason": "duplicate", "note": "Charged twice on 21 Nov"},
           detail="$96.00, allowed on Pro",
           rule={"decision": "allowed", "reason_code": "PRO_WITHIN_LIMITS",
                 "policy_ref": ex.POLICY_REF.model_dump()})
    pending = ex.PENDING_PUBLIC.model_dump(mode="json")
    t.add("confirmation.required", pending=pending, confirm_label="Confirm refund")
    t.add("turn.completed", final=_final(pending["readback_text"], pending["readback_say"],
                                         citations=[REFUND_CHUNK], pending=pending),
          usage=_usage(4200, 2, 2, 8500, 100, cache=7200, ttft=4150), end_reason="readback", replayed=False)
    out += t.events

    t = _Turn(sid, "019a7c2e-5b40-7000-8000-0000000000c3", "chat", 60)
    t.add("turn.started", served_by="agent_a", input_kind="confirm", contract_version=VERSION)
    t.step("commit", "commit", "Issuing your refund", "Issued refund", 320, args={"pending_action_id": ex.PENDING_ID},
           detail="$96.00 to card")
    cr = ex.COMMIT_RESULT
    t.add("action.result", pending_action_id=ex.PENDING_ID, action_type="refund", outcome="committed",
          changed=True, reason_codes=["PRO_WITHIN_LIMITS"], policy_refs=[ex.POLICY_REF.model_dump()],
          result_text=cr.summary, ticket=None)
    t.add("turn.completed", final=_final(cr.summary, cr.summary_say),
          usage=_usage(420, 0, 0, 0, 0, ttft=400), end_reason="committed", replayed=False)
    out += t.events

    t = _Turn(sid, "019a7c2e-5b50-7000-8000-0000000000c4", "chat", 90)
    t.add("turn.started", served_by="agent_b", input_kind="text", contract_version=VERSION)
    t.model_step(1400, 4100, 30, cache=3900)
    _search_gate(t, "how long does a refund take to reach my card")
    t.model_step(2100, 5200, 70, cache=4100)
    claims = [{"text": "Refunds reach your card in 5 to 10 business days", "supported": True,
               "source_id": "refund-policy#2"}]
    _grounding(t, claims)
    t.tokens(REFUND_TIMING_TEXT)
    t.add("turn.completed", final=_final(
        REFUND_TIMING_TEXT,
        "Refunds reach your card in five to ten business days after they are issued. Yours was issued "
        "today, so expect it by the eleventh of December.",
        citations=[REFUND_CHUNK],
        confidence={"retrieval_verdict": "pass", "top_cosine": 0.71, "tau_low": 0.30, "tau_high": 0.45,
                    "grounding": "pass"}),
        usage=_usage(5100, 2, 1, 9300, 100, cache=8000, ttft=4950), end_reason="answered", replayed=False)
    out += t.events
    return out


def _voice() -> list[dict]:
    """What: one voice turn (refund timing question via ask_support_agent). Use when: rendering
    voice_turn.jsonl. Inputs: none. Output: event dicts. Side effects: none. Role: fixture body."""
    t = _Turn(ex.SESSION_ID, "019a7c2e-5c10-7000-8000-0000000000d1", "voice", 120)
    t.add("turn.started", served_by="agent_a", input_kind="voice", contract_version=VERSION)
    t.model_step(1300, 4300, 30, cache=3900)
    _search_gate(t, "when will my refund arrive")
    t.model_step(1900, 5400, 60, cache=4300)
    _grounding(t, [{"text": "Refunds reach your card in 5 to 10 business days", "supported": True,
                    "source_id": "refund-policy#2"}])
    text = "Refunds reach your card in 5 to 10 business days, so yours should arrive by 11 December."
    say = ("Refunds reach your card in five to ten business days, so yours should arrive by the "
           "eleventh of December.")
    t.tokens(text)
    t.add("turn.completed", final=_final(text, say, citations=[REFUND_CHUNK], confidence={
        "retrieval_verdict": "pass", "top_cosine": 0.71, "tau_low": 0.30, "tau_high": 0.45,
        "grounding": "pass"}), usage=_usage(4700, 2, 1, 9700, 90, cache=8200, ttft=4600),
        end_reason="answered", replayed=False)
    return t.events


def _edge() -> list[dict]:
    """What: edge turns: llm_429 fallback with a slow status, a tool failure escalation, and a
    deadline error. Use when: rendering edge_turns.jsonl. Inputs: none. Output: event dicts.
    Side effects: none. Role: covers model.fallback, status, escalated and error for lane C."""
    sid = "019a7c2e-6000-7000-8000-00000000e001"
    out: list[dict] = []

    t = _Turn(sid, "019a7c2e-6010-7000-8000-0000000000e1", "chat", 200)
    t.add("turn.started", served_by="agent_a", input_kind="text", contract_version=VERSION)
    t.model_step(15, 0, 0, status="error")
    t.add("model.fallback", from_model=AGENT_MODEL, to_model=FALLBACK_MODEL, reason="fault_llm_429")
    t.add("status", dt_ms=4000, kind="slow", message="This is taking a little longer than usual...")
    t.model_step(4800, 1900, 40, model=FALLBACK_MODEL)
    text = "Hi! I can help with billing, refunds, your account and tickets. What do you need?"
    t.add("gate", step_id=f"{t.turn_id}:{t.steps}", gate="grounding", verdict="skipped", decision="skip",
          chunk_ids=[])
    t.tokens(text)
    t.add("turn.completed", final=_final(text, text, confidence={"grounding": "skipped"}),
          usage=_usage(5000, 2, 0, 1900, 40, models=[AGENT_MODEL, FALLBACK_MODEL], fallback=True, ttft=4950),
          end_reason="answered", replayed=False)
    out += t.events

    t = _Turn(sid, "019a7c2e-6020-7000-8000-0000000000e2", "chat", 230)
    t.add("turn.started", served_by="agent_b", input_kind="text", contract_version=VERSION)
    t.model_step(1500, 3000, 50)
    args = {"charge_id": "CH-26112102", "reason": "duplicate", "note": "Charged twice"}
    t.step("tool", "prepare_refund", "Preparing your refund", "Refund prep failed, retrying", 40, args=args,
           status="retrying", detail="Billing system did not respond")
    t.add("status", kind="retrying", message="Retrying the billing system...")
    t.step("tool", "prepare_refund", "Preparing your refund", "Refund prep failed", 45, args=args,
           status="error", attempt=2, detail="Billing system did not respond")
    ticket = ex.TICKET_REF.model_dump(mode="json")
    t.step("escalation", "create_ticket", "Opening a ticket", "Opened a ticket", 120, detail="ticket ending 8213")
    t.add("escalated", reason="tool_failure", ticket=ticket, origin="code", offer_ticket_button=False)
    line = "Our billing system didn't respond, so nothing was changed. I've opened a ticket, ending 8213."
    say = ("Our billing system didn't respond, so nothing was changed. I've opened a ticket, ending eight "
           "two one three.")
    t.add("turn.completed", final=_final(line, say, escalated=True, ticket=ticket),
          usage=_usage(1900, 1, 3, 3000, 50, ttft=1850), end_reason="escalated", replayed=False)
    out += t.events

    t = _Turn(sid, "019a7c2e-6030-7000-8000-0000000000e3", "chat", 260)
    t.add("turn.started", served_by="agent_a", input_kind="text", contract_version=VERSION)
    t.model_step(10000, 3000, 0, status="timeout")
    t.add("error", **{"class": "turn_deadline_exceeded", "code": "TURN_DEADLINE", "message":
                      "I couldn't finish that in time.", "retryable": True, "changed": None,
                      "retry_after_s": None, "fault_injected": False, "details": {}})
    t.step("escalation", "create_ticket", "Opening a ticket", "Opened a ticket", 110, detail="ticket ending 8213")
    ticket = ex.TICKET_REF.model_dump(mode="json")
    t.add("escalated", reason="deadline", ticket=ticket, origin="code", offer_ticket_button=False)
    line = "I couldn't finish that in time, so I've opened a ticket for our team: ticket ending 8213."
    say = ("I couldn't finish that in time, so I've opened a ticket for our team, ticket ending eight "
           "two one three.")
    t.add("turn.completed", final=_final(line, say, escalated=True, ticket=ticket),
          usage=_usage(45000, 1, 1, 3000, 0, ttft=44900), end_reason="deadline", replayed=False)
    out += t.events
    return out


def _s4_search(t: _Turn, query: str, verdict: str, decision: str, top: float) -> None:
    """What: a search_kb step plus a retrieval gate with the given verdict and decision.
    Use when: rendering the S4 turns. Inputs: turn, query, verdict, decision, top cosine.
    Output: none. Side effects: appends events. Role: helper."""
    sid = t.step("tool", "search_kb", "Searching help articles", "Searched help articles", 380,
                 args={"query": query}, detail="no close match" if verdict == "none" else "1 partial match")
    t.add("gate", step_id=sid, gate="retrieval", verdict=verdict, decision=decision, top_cosine=top,
          tau_low=0.30, tau_high=0.45, calibration_status="provisional", code_match=False,
          chunk_ids=[] if verdict == "none" else ["security-faq#1"])


def _s4_unsure() -> list[dict]:
    """What: S4, three unsure turns in one session (doc 07 s6.5): off-topic (none, decline, no
    ticket), a question the docs do not cover (weak 0.38, clarify), then its follow-up (weak 0.41
    against tau_high 0.45 with weak_streak 1, escalate not_in_docs with a ticket).
    Use when: rendering s4_unsure.jsonl. Inputs: none. Output: event dicts. Side effects: none.
    Role: fixture body for the step trail gate rows (CS-E-04.S5.T1)."""
    sid = "019a7c2e-7000-7000-8000-00000000f001"
    out: list[dict] = []

    t = _Turn(sid, "019a7c2e-7010-7000-8000-0000000000f1", "chat", 300)
    t.add("turn.started", served_by="agent_a", input_kind="text", contract_version=VERSION)
    t.model_step(1200, 2000, 25)
    _s4_search(t, "weather tomorrow", "none", "decline", 0.12)
    line = ("I can only help with Quillstack questions: billing, refunds, your account and how-to "
            "questions. Is there something there I can help with?")
    t.add("turn.completed", final=_final(line, line), usage=_usage(1700, 1, 1, 2000, 25, ttft=1650),
          end_reason="declined_off_topic", replayed=False)
    out += t.events

    t = _Turn(sid, "019a7c2e-7020-7000-8000-0000000000f2", "chat", 330)
    t.add("turn.started", served_by="agent_b", input_kind="text", contract_version=VERSION)
    t.model_step(1300, 2300, 25)
    _s4_search(t, "HIPAA compliance", "weak", "clarify", 0.38)
    line = ("I want to be sure I give you the right answer. Could you tell me a little more about what "
            "you need?")
    t.add("turn.completed", final=_final(line, line, confidence={
        "retrieval_verdict": "weak", "top_cosine": 0.38, "tau_low": 0.30, "tau_high": 0.45,
        "grounding": "not_run"}), usage=_usage(1800, 1, 1, 2300, 25, ttft=1750), end_reason="clarify",
        replayed=False)
    out += t.events

    t = _Turn(sid, "019a7c2e-7030-7000-8000-0000000000f3", "chat", 360)
    t.add("turn.started", served_by="agent_a", input_kind="text", contract_version=VERSION)
    t.model_step(1250, 2600, 25)
    _s4_search(t, "HIPAA business associate agreement", "weak", "escalate", 0.41)
    ticket = ex.TICKET_REF.model_copy(update={
        "ticket_id": "TCK-26118214", "short_id": "8214", "display": "ticket ending 8214",
        "queue": "account", "target_label": "HIPAA question"}).model_dump(mode="json")
    t.step("escalation", "create_ticket", "Opening a ticket", "Opened a ticket", 110,
           detail="ticket ending 8214")
    t.add("escalated", reason="not_in_docs", ticket=ticket, origin="code", offer_ticket_button=False)
    line = "Our help articles don't cover that. I've opened a ticket for our team, ticket ending 8214."
    say = ("Our help articles don't cover that. I've opened a ticket for our team, ticket ending eight "
           "two one four.")
    t.add("turn.completed", final=_final(line, say, escalated=True, ticket=ticket, confidence={
        "retrieval_verdict": "weak", "top_cosine": 0.41, "tau_low": 0.30, "tau_high": 0.45,
        "grounding": "not_run"}), usage=_usage(1900, 1, 2, 2600, 25, ttft=1850), end_reason="escalated",
        replayed=False)
    out += t.events
    return out


def _jsonl(events: list[dict]) -> str:
    """What: events as JSONL text. Use when: rendering. Inputs: event dicts. Output: text.
    Side effects: none. Role: helper."""
    return "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in events)


def render_all() -> dict[str, str]:
    """What: every fixture file's content. Use when: writing or checking fixtures.
    Inputs: none. Output: {file name: JSONL text}. Side effects: none. Role: the builder."""
    return {"s1_chat.jsonl": _jsonl(_s1_chat()), "voice_turn.jsonl": _jsonl(_voice()),
            "edge_turns.jsonl": _jsonl(_edge()), "s4_unsure.jsonl": _jsonl(_s4_unsure())}
