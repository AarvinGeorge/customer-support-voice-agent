"""What: the provider adapter for structured output: a JSON schema to the call kwargs that make the provider
constrain its reply to that schema (Anthropic `output_config.format` json_schema; OpenAI `response_format`
json_schema, strict).
Use when: GroundingVerifier.verify, for every call (the schema is built per call: its source_id enum is the
citable sids of that draft's evidence).
Inputs: a chat model, a JSON schema. Output: structured_output_kwargs.
Side effects: none (no request is sent).
Role: provider details live in agent/models (models.yaml note, D-024), never in callers; a model this adapter does
not know (a test fake) gets no kwargs, and the code rules still check every id.
"""
from __future__ import annotations

from typing import Any


def structured_output_kwargs(model: Any, schema: dict[str, Any]) -> dict[str, Any]:
    """What: the ainvoke kwargs that constrain the model's reply to `schema`.
    Use when: GroundingVerifier.verify. Inputs: the chat model, a JSON schema (additionalProperties false, every
    property required). Output: {"output_config": {"format": ...}} for ChatAnthropic, {"response_format": ...} for
    ChatOpenAI, {} otherwise. Side effects: none. Role: provider adapter (review PR #71 I1)."""
    name = type(model).__name__
    if name == "ChatAnthropic":
        from anthropic import transform_schema

        return {"output_config": {"format": {"type": "json_schema", "schema": transform_schema(schema)}}}
    if name in ("ChatOpenAI", "AzureChatOpenAI"):
        return {"response_format": {"type": "json_schema",
                                    "json_schema": {"name": "verdict", "schema": schema, "strict": True}}}
    return {}
