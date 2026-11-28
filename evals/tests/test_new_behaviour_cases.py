"""What: offline tests for golden cases G-16 to G-20 (the owner's 14:03 session, one identity per conversation, a second
refund ask, a rejected prepare then the right charge, a general first question with the card). Each case has a hand-built
run that must grade pass and mutated runs that must grade fail on the named check.
Use when: CI. Inputs: tests/support.py builders and the shipped cases. Output: pass or fail. Side effects: none.
Role: proves each new case can fail before any real run uses it (D-065, D-070, D-071, D-074, D-075, D-079)."""
from __future__ import annotations

from typing import Any

from evals.model import CaseRun
from evals.tests.support import gate, make_run, rule, step, turn
from evals.tests.test_graders import ASK, PA, PRO_RULE, VERIFY_REQ, by, grade

CARD = {"type": "verification.required", "reason": "account_lookup", "prompt_text": "p", "attempts_left": 3, "fields": ["account_id", "pin"]}
GROUND = {**gate("pass", "answer", kind="grounding"), "claims_total": 2, "claims_supported": 2}
GROUND_STATE = {**GROUND, "claims": [{"text": "No ticket was created.", "supported": True, "source_id": "session_state"},
                                      {"text": "A card is on screen.", "supported": True, "source_id": "session_state"}]}
VOICE = {"user_transcript": "t", "transcript_final": True}


def failing(g: Any) -> list[str]:
    """What: names of failed checks. Use when: asserting a mutation fails exactly where meant. Inputs: grade. Output: names.
    Side effects: none. Role: helper."""
    return [c.name for c in g.checks if c.status == "fail"]


def drop_gate2(run: CaseRun, index: int) -> None:
    """What: remove the turn's Gate 2 event from the stored rows (graders read stored events). Use when: a mutation test.
    Inputs: run, turn index. Output: none. Side effects: edits run.data. Role: helper."""
    assert run.data is not None
    tid = run.turns[index].turn_id
    run.data.events = [e for e in run.data.events if not (e["turn_id"] == tid and e.get("type") == "gate")]


def g16_run(**over: Any) -> CaseRun:
    """What: a run that satisfies G-16 (no ticket, card on turn 0, a grounded last answer). Use when: a test mutates one thing.
    Inputs: make_run overrides. Output: CaseRun. Side effects: none. Role: the happy baseline."""
    t = [turn(0, "text", [CARD], text="Hello, how can I help?"),
         turn(1, "voice", [], text="I don't know who you are yet.", input=VOICE),
         turn(2, "voice", [*step("request_verification"), VERIFY_REQ], end_reason="verification_requested", text="Please verify.", input=VOICE),
         turn(3, "voice", [], text="Use the secure card when you are ready.", input=VOICE),
         turn(4, "voice", [GROUND_STATE], text="No, I have not created a ticket for you.", input=VOICE)]
    return make_run("G-16", t, **over)


def test_g16_passes_and_each_mutation_fails_its_own_check() -> None:
    g = grade("G-16", g16_run())
    assert g.verdict == "pass", [(c.name, c.detail) for c in g.checks if c.status != "pass"]
    ticket = {"id": "t", "queue": "technical", "category": "other", "stage": "open", "priority": "P4", "origin": "code"}
    assert {"final_state.tickets", "forbidden:unneeded_ticket"} <= set(failing(grade("G-16", g16_run(tickets=[ticket]))))  # D-070
    run = g16_run()
    run.turns[0].events = [e for e in run.turns[0].events if e["type"] != "verification.required"]
    assert failing(grade("G-16", run)) == ["turn.0"]  # D-074: no card on the first message
    run = g16_run()
    drop_gate2(run, 4)
    assert failing(grade("G-16", run)) == ["turn.4"]  # D-071: the last answer was not a Gate 2 checked claim
    run = g16_run()
    run.turns[4].events[-1]["end_reason"] = "clarify"
    assert "turn.4" in failing(grade("G-16", run))  # the clarify line replaced the answer
    bad = make_run("G-16", [turn(0, "text", [CARD]), turn(1, "voice", [*step("create_ticket")], input=VOICE),
                            turn(2, "voice", [*step("request_verification"), VERIFY_REQ], end_reason="verification_requested", input=VOICE),
                            turn(3, "voice", [], input=VOICE), turn(4, "voice", [GROUND_STATE], text="ticket", input=VOICE)])
    assert {"turn.1", "trajectory"} <= set(failing(grade("G-16", bad)))  # a model ticket call while unverified


def g17_run(**over: Any) -> CaseRun:
    """What: a run that satisfies G-17 (card-first, read-back, conflict line, commit). Use when: a test mutates one thing.
    Inputs: make_run overrides. Output: CaseRun. Side effects: none. Role: the happy baseline."""
    result = {"type": "action.result", "pending_action_id": PA, "action_type": "refund", "outcome": "committed", "changed": True,
              "reason_codes": ["PRO_WITHIN_LIMITS"], "policy_refs": [{"article": "refund-policy", "section": "pro-plan-refunds", "title": "T"}],
              "result_text": "Refunded $96.00 for the charge ending 0412.", "ticket": None}
    t = [turn(0, "verification", [], end_reason="verified"),
         turn(1, "text", [*step("list_charges"), *step("prepare_refund", rule=PRO_RULE), ASK], end_reason="readback", text="Refund $96.00 for the charge ending 0412?"),
         turn(2, "verification", [{"type": "verification.result", "status": "conflict"}], text="To help a different account, please start a new conversation."),
         turn(3, "confirm", [*step("commit", kind="commit", args={"pending_action_id": PA}), result], end_reason="committed",
              text="Refunded $96.00 for the charge ending 0412.", say="Refunded ninety six dollars for the charge ending 0412.",
              input={"decision": "confirm", "pending_action_id": PA})]
    row = {"id": PA, "action_type": "refund", "target_key": "refund:CH-00000412", "params": {"charge_id": "CH-00000412", "amount_cents": 9600},
           "state": "committed", "reason_codes": ["PRO_WITHIN_LIMITS"]}
    kw: dict[str, Any] = {"pending_rows": [row], "refunds": [{"target_key": "refund:CH-00000412", "amount_cents": 9600}]}
    kw.update(over)
    return make_run("G-17", t, **kw)


def test_g17_passes_and_each_mutation_fails_its_own_check() -> None:
    g = grade("G-17", g17_run())
    assert g.verdict == "pass", [(c.name, c.detail) for c in g.checks if c.status != "pass"]
    run = g17_run()
    run.turns[2].events = [e for e in run.turns[2].events if e["type"] != "verification.result"]
    assert failing(grade("G-17", run)) == ["turn.2"]  # no conflict result on the wire
    run = g17_run()
    run.turns[2].events[-1]["final"]["text"] = "This conversation is verified."
    assert failing(grade("G-17", run)) == ["turn.2"]  # not the fixed line
    run = g17_run()
    run.turns[2].events.insert(1, {**CARD, "reason": "retry", "seq": 99, "turn_id": run.turns[2].turn_id})
    assert failing(grade("G-17", run)) == ["turn.2"]  # a new card after the conflict
    assert "final_state.refunds" in failing(grade("G-17", g17_run(refunds=[], pending_rows=[])))  # the identity did not hold, no commit


def g18_run(**over: Any) -> CaseRun:
    """What: a run that satisfies G-18 (decline, then a grounded second answer naming the role). Use when: a test mutates one thing.
    Inputs: make_run overrides. Output: CaseRun. Side effects: none. Role: the happy baseline."""
    deny = rule("ROLE_NOT_PERMITTED", "declined", "roles-and-permissions", "billing-permissions")
    t = [turn(0, "text", [*step("request_verification"), VERIFY_REQ], end_reason="verification_requested", text="Please verify."),
         turn(1, "verification", [*step("prepare_refund", rule=deny)], text="Only owners and billing admins can request refunds. I can open a ticket."),
         turn(2, "voice", [GROUND], text="As I said, only an owner or a billing admin can request this refund.", input=VOICE)]
    return make_run("G-18", t, **over)


def test_g18_passes_and_each_mutation_fails_its_own_check() -> None:
    g = grade("G-18", g18_run())
    assert g.verdict == "pass", [(c.name, c.detail) for c in g.checks if c.status != "pass"]
    run = g18_run()
    run.turns[2].events[-1]["end_reason"] = "clarify"
    assert {"turn.2", "outcome"} & set(failing(grade("G-18", run)))  # D-065 regression: the clarify line replaced the answer
    run = g18_run()
    drop_gate2(run, 2)
    assert failing(grade("G-18", run)) == ["turn.2"]  # no Gate 2 verified claim
    run = g18_run()
    run.turns[2].events[-1]["final"]["text"] = "I will look into it."
    assert "turn.2" in failing(grade("G-18", run))  # the decline is not repeated


def g19_run(**over: Any) -> CaseRun:
    """What: a run that satisfies G-19 (a rejected prepare, then the right one, one read-back, one submitted row). Use when:
    a test mutates one thing. Inputs: make_run overrides. Output: CaseRun. Side effects: none. Role: the happy baseline."""
    biz = rule("BUSINESS_APPROVAL_REQUIRED", "needs_approval", "refund-policy", "business-plan-refunds")
    result = {"type": "action.result", "pending_action_id": PA, "action_type": "refund", "outcome": "submitted", "changed": True,
              "reason_codes": ["BUSINESS_APPROVAL_REQUIRED"], "policy_refs": [{"article": "refund-policy", "section": "business-plan-refunds", "title": "T"}],
              "result_text": "Sent for approval, invoice ending 0901.", "ticket": None}
    t = [turn(0, "verification", [], end_reason="verified"),
         turn(1, "text", [*step("prepare_refund", status="error"), *step("list_charges"), *step("prepare_refund", rule=biz), ASK], end_reason="readback",
              text="Refund the invoice ending 0901?"),
         turn(2, "confirm", [*step("commit", kind="commit", args={"pending_action_id": PA}), result], end_reason="submitted",
              text="Sent for approval, invoice ending 0901.", say="Sent for approval, invoice ending 0901.", input={"decision": "confirm", "pending_action_id": PA})]
    row = {"id": PA, "action_type": "refund", "target_key": "refund:CH-00000901", "params": {"charge_id": "CH-00000901", "amount_cents": 1152000},
           "state": "awaiting_approval", "reason_codes": ["BUSINESS_APPROVAL_REQUIRED"]}
    tk = {"id": "t", "queue": "approvals", "category": "refund", "stage": "pending_approval", "priority": "P2", "origin": "core"}
    kw: dict[str, Any] = {"pending_rows": [row], "tickets": [tk]}
    kw.update(over)
    return make_run("G-19", t, **kw)


def test_g19_passes_and_each_mutation_fails_its_own_check() -> None:
    g = grade("G-19", g19_run())
    assert g.verdict == "pass", [(c.name, c.detail) for c in g.checks if c.status != "pass"]
    run = g19_run()
    run.turns[1].events[-1]["end_reason"] = "clarify"
    assert "turn.1" in failing(grade("G-19", run))  # D-079 regression: the retry was blocked and the clarify line followed
    assert "final_state.pending" in failing(grade("G-19", g19_run(pending_rows=[*g19_run().data.pending, *g19_run().data.pending])))  # type: ignore[union-attr]
    four = [turn(0, "verification", [], end_reason="verified"),
            turn(1, "text", [*step("prepare_refund"), *step("prepare_refund"), *step("prepare_refund"), *step("prepare_refund"), ASK], end_reason="readback")]
    assert "trajectory" in failing(grade("G-19", make_run("G-19", four)))  # more than 3 prepares


def g20_run() -> CaseRun:
    """What: a run that satisfies G-20 (answered with a citation and the card). Use when: a test mutates one thing.
    Inputs: none. Output: CaseRun. Side effects: none. Role: the happy baseline."""
    return make_run("G-20", [turn(0, "text", [*step("search_kb"), gate("pass", "answer"), GROUND, CARD], text="Open Settings, then Export.",
                                  citations=["import-export-retention#exporting-content"])])


def test_g20_passes_and_each_mutation_fails_its_own_check() -> None:
    g = grade("G-20", g20_run())
    assert g.verdict == "pass", [(c.name, c.detail) for c in g.checks if c.status != "pass"]
    run = g20_run()
    run.turns[0].events = [e for e in run.turns[0].events if e["type"] != "verification.required"]
    assert failing(grade("G-20", run)) == ["turn.0"]  # no card offered
    run = g20_run()
    run.turns[0].events[-1]["end_reason"] = "verification_requested"
    assert "turn.0" in failing(grade("G-20", run))  # the card blocked the answer
    blocked = make_run("G-20", [turn(0, "text", [*step("search_kb"), *step("request_verification"), VERIFY_REQ], end_reason="verification_requested")])
    assert {"trajectory", "turn.0"} <= set(failing(grade("G-20", blocked)))
    assert by(grade("G-20", g20_run()))["citations"] == "pass"


def probe(grade_: Any, name: str) -> bool | None:
    """What: the exercised flag of one path probe check. Use when: asserting which path a run took. Inputs: grade, probe id.
    Output: True, False or None when absent. Side effects: none. Role: helper."""
    return next((c.exercised for c in grade_.checks if c.name == f"path:{name}"), None)


def test_g18_records_whether_the_second_answer_rested_on_session_evidence() -> None:
    g = grade("G-18", g18_run())
    assert g.verdict == "pass" and probe(g, "answer_from_session_evidence") is True  # no tool call in turn 2
    deny = rule("ROLE_NOT_PERMITTED", "declined", "roles-and-permissions", "billing-permissions")
    t = [turn(0, "text", [*step("request_verification"), VERIFY_REQ], end_reason="verification_requested"),
         turn(1, "verification", [*step("prepare_refund", rule=deny)], text="Only owners and billing admins can request refunds. ticket"),
         turn(2, "voice", [*step("prepare_refund", rule=deny), GROUND], text="Only an owner or a billing admin can request this.", input=VOICE)]
    g2 = grade("G-18", make_run("G-18", t))
    assert probe(g2, "answer_from_session_evidence") is False  # a fresh prepare in turn 2: the trivial path (I3)


def test_g19_records_whether_a_rejected_prepare_was_retried() -> None:
    assert probe(grade("G-19", g19_run()), "retry_after_rejected_prepare") is True
    biz = rule("BUSINESS_APPROVAL_REQUIRED", "needs_approval", "refund-policy", "business-plan-refunds")
    t = [turn(0, "verification", [], end_reason="verified"), turn(1, "text", [*step("list_charges"), *step("prepare_refund", rule=biz), ASK], end_reason="readback")]
    g = grade("G-19", make_run("G-19", t))
    assert probe(g, "retry_after_rejected_prepare") is False  # listed charges first: the invariant held, D-079 not exercised


def g21_run(verdict: str = "weak") -> CaseRun:
    """What: a run that satisfies G-21 (weak Gate 1, cited Gate 2 pass, ends answered). Use when: a test mutates one thing.
    Inputs: Gate 1 verdict. Output: CaseRun. Side effects: none. Role: the happy baseline."""
    t = [turn(0, "verification", [], end_reason="verified"),
         turn(1, "text", [*step("search_kb"), gate(verdict, "clarify" if verdict == "weak" else "answer", ["refund-policy#how-refunds-work"]), GROUND],
              text="Refunds reach your card in 5 to 10 business days.", citations=["refund-policy#how-refunds-work"])]
    return make_run("G-21", t)


def test_g21_d066_weak_gate_one_still_ends_answered_and_records_the_path() -> None:
    g = grade("G-21", g21_run())
    assert g.verdict == "pass", [(c.name, c.detail) for c in g.checks if c.status != "pass"]
    assert probe(g, "weak_retrieval_answered") is True
    assert probe(grade("G-21", g21_run("pass")), "weak_retrieval_answered") is False  # Gate 1 passed: D-066 not exercised
    run = g21_run()
    run.turns[1].events[-1]["end_reason"] = "clarify"
    assert "turn.1" in failing(grade("G-21", run))  # the pre-D-066 label


def test_g16_a_ticket_claim_without_session_state_support_fails() -> None:
    """Review PR #84 I2: the last answer is graded on its claim, not on the word ticket."""
    assert grade("G-16", g16_run()).verdict == "pass"  # a supported claim resting on session_state
    wrong = {**GROUND, "claims": [{"text": "Yes, I created a ticket for you.", "supported": True, "source_id": "account_context"}]}
    t = [turn(0, "text", [CARD]), turn(1, "voice", [], input=VOICE),
         turn(2, "voice", [*step("request_verification"), VERIFY_REQ], end_reason="verification_requested", input=VOICE),
         turn(3, "voice", [], input=VOICE), turn(4, "voice", [wrong], text="Yes, I created a ticket for you.", input=VOICE)]
    assert "turn.4" in failing(grade("G-16", make_run("G-16", t)))  # a ticket claim not tied to session_state (the false Yes)
    t[4] = turn(4, "voice", [GROUND], text="Yes, I created a ticket for you.", input=VOICE)  # no claim list at all
    assert "turn.4" in failing(grade("G-16", make_run("G-16", t)))


def g16_with_turn2(end_reason: str, text: str, parts: list[dict[str, Any]] | None = None) -> CaseRun:
    """What: G-16's happy run with turn 2 replaced. Use when: the D-082 turn 2 tests. Inputs: end_reason, final text,
    partial events. Output: CaseRun. Side effects: none. Role: helper."""
    t = [turn(0, "text", [CARD], text="Hello, how can I help?"),
         turn(1, "voice", [], text="I don't know who you are yet.", input=VOICE),
         turn(2, "voice", parts or [], end_reason=end_reason, text=text, input=VOICE),
         turn(3, "voice", [], text="Use the secure card when you are ready.", input=VOICE),
         turn(4, "voice", [GROUND_STATE], text="No, I have not created a ticket for you.", input=VOICE)]
    return make_run("G-16", t)


def test_g16_turn2_accepts_a_card_pointer_answer_but_not_a_ticket_or_a_bare_answer() -> None:
    """D-082: after the code-offered card, turn 2 passes on verification_requested OR on answered with a card pointer;
    still no ticket and no refund prepare. A bare answer that does not point to the card, or any other end, fails."""
    ask = g16_with_turn2("verification_requested", "Please verify.", [*step("request_verification"), VERIFY_REQ])
    assert "turn.2" not in failing(grade("G-16", ask))  # the original path still passes
    point = g16_with_turn2("answered", "Please use the verification card above to continue.")
    assert "turn.2" not in failing(grade("G-16", point))  # D-082: answered, pointing to the card on screen
    assert "turn.2" in failing(grade("G-16", g16_with_turn2("answered", "Sorry, I cannot help with that.")))  # no pointer
    assert "turn.2" in failing(grade("G-16", g16_with_turn2("clarify", "Please use the verification card above.")))  # other end
    tick = g16_with_turn2("answered", "Please use the verification card above.", step("create_ticket"))
    assert "turn.2" in failing(grade("G-16", tick))  # a ticket call is still forbidden
