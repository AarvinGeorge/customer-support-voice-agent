"""What: a Gate 2 verifier model fake that cites honestly: each scripted claim is supported only when its source_id
appears in the evidence it was given (as a <doc id>, a <tool_result name> or a <source id>).
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
    """What: the source ids present in the verifier's <evidence> block. Use when: the fake decides support.
    Inputs: the verifier's user message. Output: set of ids. Side effects: none. Role: helper."""
    evidence = human.split("<draft>")[0]
    return set(re.findall(r'<(?:doc\s+id|tool_result\s+name|source\s+id)="([^"]+)"', evidence))


class CitingVerifierModel:
    """What: ainvoke returns the scripted claims, each supported iff its source_id is in the evidence.
    Use when: GroundingVerifier(CitingVerifierModel(...)). Inputs: claims, commitment.
    Output: AIMessage JSON with usage_metadata and model_name. Side effects: appends to `seen` and `ids`.
    Role: fake."""

    def __init__(self, *claims: tuple[str, str], commitment: bool = False) -> None:
        """What: keep the script. Use when: tests. Inputs: (text, source_id) pairs, commitment. Output: none.
        Side effects: none. Role: constructor."""
        self.claims, self.commitment = list(claims), commitment
        self.seen: list[str] = []
        self.ids: list[set[str]] = []

    async def ainvoke(self, messages: list[Any], config: Any = None) -> AIMessage:
        """What: one verifier reply. Use when: GroundingVerifier.verify. Inputs: messages, config.
        Output: AIMessage. Side effects: records the user message and the ids seen. Role: fake call."""
        human = str(messages[-1].content)
        ids = evidence_ids(human)
        self.seen.append(human)
        self.ids.append(ids)
        body = {"claims": [{"text": t, "supported": s in ids, "source_id": s} for t, s in self.claims],
                "commitment": self.commitment}
        return AIMessage(json.dumps(body), response_metadata={"model_name": VERIFIER_MODEL},
                         usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
