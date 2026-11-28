"""What: content tests for the frozen Realtime prompt and the ask_support_agent tool description: the voice model
relays every customer question or request to the agent and decides nothing about scope, policy or identity
(CS-E-05.F2, same class as F7: the voice model judged scope itself, so Gate 1 and the classifier never ran).
Use when: pytest. Inputs: REALTIME_PROMPT, ASK_SUPPORT_AGENT_TOOL, data/policy.yaml. Output: pass or fail.
Side effects: none. Role: principle 22 guard on the voice front end (meaning goes to the agent, not a second model)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from contracts.tools import ASK_SUPPORT_AGENT_TOOL

from agent.realtime import REALTIME_PROMPT

POLICY = yaml.safe_load((Path(__file__).resolve().parents[3] / "data" / "policy.yaml").read_text())

# Phrases that make the voice model judge scope or topic before relaying. These check a fixed prompt text we
# own (a content snapshot), not customer input, so they decide no meaning at runtime.
SCOPE_RULE_PHRASES = ("anything about", "about quillstack", "off-topic", "off topic", "unrelated", "out of scope",
                      "not related", "decline", "refuse", "only questions", "only quillstack")

# The relay rule before CS-E-05.F2 (positive control: it must be caught).
OLD_RELAY_RULE = ("Call ask_support_agent for anything about Quillstack, the customer's account, billing, charges, "
                  "policies, tickets or actions, and whenever the customer answers a question the agent asked.")


def scope_rules(text: str) -> list[str]:
    """What: the scope-gating phrases found in text. Use when: prompt and description checks. Inputs: text.
    Output: sorted matches. Side effects: none. Role: test helper."""
    low = text.lower()
    return sorted(p for p in SCOPE_RULE_PHRASES if p in low)


def policy_terms(text: str) -> list[str]:
    """What: the policy.yaml role, tier and blocked-status names (and spaced forms) found in text.
    Use when: rule checks. Inputs: text. Output: sorted matches. Side effects: none.
    Role: test helper; vocabulary comes from the policy file, not a hand list."""
    words = [*POLICY["roles"]["all_roles"], *POLICY["tiers"], *POLICY["actions"]["refund"]["blocked_statuses"]]
    forms = {f for w in words for f in (w, w.replace("_", " "))}
    return sorted(f for f in forms if re.search(rf"\b{re.escape(f)}s?\b", text, re.IGNORECASE))


def test_scope_rules_helper_can_fail() -> None:
    assert scope_rules(OLD_RELAY_RULE) == ["about quillstack", "anything about"]
    assert scope_rules("Off-topic requests: decline politely.") == ["decline", "off-topic"]
    assert policy_terms("Only owners can refund a Pro plan") == ["owner", "pro"]


def test_realtime_prompt_relays_every_question_or_request() -> None:
    low = REALTIME_PROMPT.lower()
    assert "every customer question or request" in low
    assert "whatever the topic" in low
    assert "you decide nothing about scope, policy or identity" in low


def test_realtime_prompt_allows_only_conversational_glue_without_a_call() -> None:
    low = REALTIME_PROMPT.lower()
    assert "greets you, says thanks or says goodbye" in low
    assert "without calling the tool" in low


def test_realtime_prompt_states_no_scope_or_policy_rule() -> None:
    assert scope_rules(REALTIME_PROMPT) == []
    assert policy_terms(REALTIME_PROMPT) == []
    assert not re.search(r"\d", REALTIME_PROMPT)


def test_tool_description_relays_every_question_or_request() -> None:
    low = ASK_SUPPORT_AGENT_TOOL["description"].lower()
    assert "every customer question or request" in low
    assert "whatever the topic" in low
    assert "the agent decides scope, policy and identity" in low


def test_tool_description_states_no_scope_or_policy_rule() -> None:
    desc = ASK_SUPPORT_AGENT_TOOL["description"]
    assert scope_rules(desc) == []
    assert policy_terms(desc) == []
    assert not re.search(r"\d", desc)
