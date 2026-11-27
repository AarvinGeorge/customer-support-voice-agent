"""What: a Gate 2 verifier model fake that cites honestly: each scripted claim is supported only when its source_id
appears in the evidence it was given (a <source sid=".."> block that is also in the code-written <citable_ids> list).
Use when: tests that check WHICH sources reach the verifier (verification turns, tool results, fixed lines).
Inputs: claims as (text, source_id) pairs. Output: CitingVerifierModel. Side effects: records each call.
Role: fake for the grounding_verifier role; reports usage and a model name like a real chat model reply.
"""
from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import AIMessage

VERIFIER_MODEL = "fake-verifier"


def evidence_ids(human: str) -> set[str]:
    """What: the sids the verifier may cite: present as a <source sid=".."> block in <evidence> and listed in
    <citable_ids> (review PR #71 I1). Use when: the fake decides support.
    Inputs: the verifier's user message. Output: set of ids. Side effects: none. Role: helper."""
    evidence = human.split("</evidence>")[0]
    blocks = set(re.findall(r'<source sid="([^"]+)"', evidence))
    listed = human.split("<citable_ids>")[1].split("</citable_ids>")[0] if "<citable_ids>" in human else ""
    return blocks & {line.strip() for line in listed.splitlines() if line.strip()}


class CitingVerifierModel:
    """What: ainvoke returns the scripted claims, each supported iff its source_id is in the evidence; with no
    scripted claims, an empty list with no_claims_reason "conversation" (D-069).
    Use when: GroundingVerifier(CitingVerifierModel(...)). Inputs: claims, commitment.
    Output: AIMessage JSON with usage_metadata and model_name. Side effects: appends to `seen` and `ids`.
    Role: fake."""

    def __init__(self, *claims: tuple[str, str], commitment: bool = False,
                 no_claims_reason: str | None = None) -> None:
        """What: keep the script. Use when: tests. Inputs: (text, source_id) pairs, commitment, the no-claims
        reason for an empty script. Output: none. Side effects: none. Role: constructor."""
        self.claims, self.commitment, self.no_claims_reason = list(claims), commitment, no_claims_reason
        self.seen: list[str] = []
        self.ids: list[set[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    async def ainvoke(self, messages: list[Any], config: Any = None, **kwargs: Any) -> AIMessage:
        """What: one verifier reply. Use when: GroundingVerifier.verify. Inputs: messages, config, call kwargs
        (structured output). Output: AIMessage. Side effects: records the user message, the ids seen and the
        kwargs. Role: fake call."""
        self.kwargs.append(kwargs)
        human = str(messages[-1].content)
        ids = evidence_ids(human)
        self.seen.append(human)
        self.ids.append(ids)
        body = {"claims": [{"text": t, "supported": s in ids, "source_id": s} for t, s in self.claims],
                "commitment": self.commitment,
                "no_claims_reason": None if self.claims else (self.no_claims_reason or "conversation")}  # D-069
        return AIMessage(json.dumps(body), response_metadata={"model_name": VERIFIER_MODEL},
                         usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
