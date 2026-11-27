"""What: ONE real two-turn run on the primary agent model to prove prompt caching (CS-E-03.S8.T2 Done when).
Use when: `uv run pytest -m real -s` only; deselected otherwise. Exactly 2 real model calls.
Inputs: ANTHROPIC_API_KEY and OPENAI_API_KEY from the worktree .env (values never printed).
Output: pass when turn 2 reports cache_read_input_tokens above 0; prints both turns' usage.
Side effects: two paid Anthropic calls (the OpenAI fallback client is built, never called).
Role: integration evidence for the caching middleware and the frozen system prompt.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from agent.assembly import build_agent_from_registry, memory_checkpointer_factory, run_config
from agent.models import ModelRegistry, load_models_config
from agent.prompt import TurnContext, build_turn_message

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.real
async def test_second_turn_reads_the_prompt_cache():
    load_dotenv(ROOT / ".env", override=False)
    if not (os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("OPENAI_API_KEY")):
        pytest.fail("provider keys missing: this test must not pass without making the calls")
    registry = ModelRegistry(load_models_config(ROOT / "data" / "models.yaml"))
    ctx = TurnContext(mode="chat", verified=False)
    async with memory_checkpointer_factory()() as saver:
        agent = build_agent_from_registry(registry, checkpointer=saver)
        cfg = run_config("00000000-0000-7000-8000-0000000000t2")
        usages = []
        for text in ("Hi, in one sentence: what can you help me with?",
                     "Thanks. In one sentence: can you change my billing email?"):
            out = await agent.ainvoke({"messages": [build_turn_message(text, ctx)]}, cfg)
            last = out["messages"][-1]
            meta = last.response_metadata or {}
            raw = meta.get("usage") or {}
            usages.append({"model": meta.get("model_name") or meta.get("model"),
                           "usage_metadata": last.usage_metadata, "raw_usage": raw})
            print(f"TURN {len(usages)} model={usages[-1]['model']} usage_metadata={last.usage_metadata}")
            print(f"TURN {len(usages)} raw cache_creation_input_tokens={raw.get('cache_creation_input_tokens')} "
                  f"cache_read_input_tokens={raw.get('cache_read_input_tokens')} input_tokens={raw.get('input_tokens')}")
        state = await agent.aget_state(cfg)
        print(f"THREAD messages={len(state.values['messages'])}")
    assert usages[0]["model"] and usages[0]["model"].startswith(registry.spec("agent").model)
    read2 = usages[1]["raw_usage"].get("cache_read_input_tokens") or \
        (usages[1]["usage_metadata"] or {}).get("input_token_details", {}).get("cache_read", 0)
    assert read2 > 0
