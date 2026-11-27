"""What: the reviewers' adversarial Gate 2 drafts (PR #52 re-reviews r2 and r3), shared by the unit tests (trigger
recall) and the real-model eval (claim extraction).
Use when: tests/test_gate2.py checks that every policy draft triggers the verifier; tests/test_real_gate2_eval.py
checks that the real small model flags every policy draft and passes the clarifying replies.
Inputs: none. Output: POLICY_IN_QUESTION_R2, POLICY_IN_QUESTION_R3, CLARIFYING_R3, CLARIFYING_R2.
Side effects: none.
Role: D-059 fixture data: meaning is judged by the model, so these lists are an eval, not regexes.
"""
from __future__ import annotations

# r2 N2, list A: policy stated inside or next to a question (10). Every one must be flagged.
POLICY_IN_QUESTION_R2 = [
    "Did you know Pro refunds are capped at $150?",
    "Did you know you can get a full refund within 90 days?",
    "You know refunds are always approved on Business, right?",
    "Pro refunds are capped at $150, is that okay?",
    "Your refund of $500 is approved, shall I go ahead?",
    "You are eligible for a 60-day refund - want me to start it?",
    "Isn't it great that Enterprise includes unlimited refunds?",
    "Pro costs $5 per seat\nBusiness costs $9 per seat\nWhich do you want?",
    "Refunds take 3 days; credits are instant; which would you like?",
    "Would it help to know that the refund window is 45 days?",
]

# r3 N3, list C: commitments and rules in words inside a question (12). Every one must be flagged.
POLICY_IN_QUESTION_R3 = [
    "Did you realize you qualify for a full refund?",
    "Would you like the thirty-day refund you are eligible for?",
    "Have you heard that refunds never expire?",
    "Shall I go ahead with the full refund, since owners can always get one?",
    "Can I interest you in the two free months you are entitled to?",
    "Why not take the full refund you qualify for?",
    "How about the ninety day money back guarantee, do you want it?",
    "Is it helpful that annual plans get a full refund at any time?",
    "Should I apply the half off discount every customer is allowed?",
    "Is it true refunds take 5 to 10 days?",
    "Are you aware refunds are always approved on Business?",
    "- Which plan are you on?\n- Refunds are always approved",
]

# r3 N4, list D: clarifying replies (12). Each should pass; the "$49" echo is the one the reviewer called arguable.
CLARIFYING_R3 = [
    "Just to confirm, are you on Pro or Business?",
    "Thanks. And which plan are you on, Pro or Business?",
    "Which plan are you on,\nPro or Business?",
    "Are you asking about the Pro plan only?",
    "Was that 3 seats or 5 seats you wanted to remove?",
    "Did the charge of $49 appear on your card?",
    "Could you confirm you are on Pro?",
    "Would you like me to open a ticket?",
    "Is this about the charge on 30 November?",
    "Do you want to cancel now or at the end of the billing cycle?",
    "Got it. Is the workspace on the Enterprise tier?",
    "I can help with that.\n- Which plan are you on?\n- Which charge is it about?",
]

# r2 clarifying examples (a sample of list B), passed in every earlier round.
CLARIFYING_R2 = [
    "Are you on the Pro plan or the Business plan?",
    "Do you mean the 30 November renewal?",
    "Do you want a refund or a credit?",
    "Just to check, are you the admin or a member?",
]
