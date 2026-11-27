"""What: tests for the knowledge search request rules (doc 07 s4.1).
Use when: CI. Inputs: none. Output: pass or fail. Side effects: none.
Role: guards tier and verified pairing."""
import pytest
from pydantic import ValidationError

from contracts.knowledge_api import SearchRequest


def test_unverified_requires_public_tier():
    """What: verified false needs tier public; verified true needs a real tier.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: s4.1 rule."""
    SearchRequest.model_validate({"query": "refund", "tier": "public", "verified": False})
    SearchRequest.model_validate({"query": "refund", "tier": "pro", "verified": True})
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "refund", "tier": "pro", "verified": False})
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "refund", "tier": "public", "verified": True})


def test_k_bounds_and_error_code_pattern():
    """What: k 1..8, default 5; error_code pattern. Use when: CI. Inputs: none.
    Output: assertion. Side effects: none. Role: s4.1 fields."""
    assert SearchRequest.model_validate({"query": "q", "tier": "public", "verified": False}).k == 5
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "q", "tier": "public", "verified": False, "k": 9})
    with pytest.raises(ValidationError):
        SearchRequest.model_validate({"query": "q", "tier": "public", "verified": False,
                                      "error_code": "SSO-401"})
