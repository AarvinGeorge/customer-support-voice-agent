"""What: ONE real traced turn on the cheapest configured model (claude-haiku-5-5) proving usage.model_calls equals
the LangSmith llm run count for the trace whose root run id is the turn id (CS-E-03.S8.T8 Done when, part 2).
Use when: `uv run pytest -m real -s tests/test_real_trajectory.py` only; deselected otherwise. One model call.
Inputs: ANTHROPIC_API_KEY, OPENAI_API_KEY (fallback client built, never called), LANGSMITH_API_KEY from the
worktree .env (values never printed).
Output: pass when the root run id equals the turn id and llm runs == usage.model_calls; prints both.
Side effects: one paid Anthropic call; LangSmith trace upload and reads.
Role: integration evidence for run_id = turn_id and the model_calls count.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_core.utils.uuid import uuid7

from agent.assembly import build_agent, memory_checkpointer_factory, turn_run_config
from agent.models import ModelRegistry, load_models_config
from agent.sse import TurnMeta, TurnStream, stream_agent_turn

ROOT = Path(__file__).resolve().parents[3]
CHEAPEST = "scope_classifier"  # claude-haiku-5-5, the lowest input and output price in models.yaml


@pytest.mark.real
async def test_model_calls_match_langsmith_llm_runs_for_the_turn_trace():
    load_dotenv(ROOT / ".env", override=False)
    if not all(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LANGSMITH_API_KEY")):
        pytest.fail("keys missing: this test must not pass without making the call")
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ.setdefault("LANGSMITH_PROJECT", "cs-voice-agent")
    from langchain_core.tracers.langchain import wait_for_all_tracers
    from langsmith import Client

    reg = ModelRegistry(load_models_config(ROOT / "data" / "models.yaml"))
    spec = reg.spec(CHEAPEST)
    reg.config.roles["agent"] = spec.model_copy(update={"attempt_timeout_s": 10, "first_token_timeout_s": 6,
                                                         "max_tokens": 64})
    sid, tid = str(uuid7()), str(uuid7())
    async with memory_checkpointer_factory()() as saver:
        agent = build_agent(reg.build("agent"), fallback_model=reg.build("agent_fallback"), checkpointer=saver)
        stream = TurnStream(TurnMeta(sid, tid, "chat", "agent_a", "text"))
        out = await stream_agent_turn(stream, agent, {"messages": [HumanMessage("Reply with the single word: OK")]},
                                      turn_run_config(sid, tid), cost_of=reg.cost_for_model)
    wait_for_all_tracers()
    client = Client()
    llm_runs: list = []
    root = None
    for _ in range(15):  # LangSmith ingests asynchronously
        try:
            root = client.read_run(tid)
            llm_runs = list(client.list_runs(trace_id=tid, run_type="llm"))
            if llm_runs and root.end_time is not None:
                break
        except Exception:  # noqa: BLE001 - not ingested yet
            pass
        await asyncio.sleep(2)
    u = out.usage
    print(f"TURN turn_id={tid} model_calls={u.model_calls} models_used={u.models_used} "
          f"input={u.input_tokens} output={u.output_tokens} cost_usd_est={u.cost_usd_est:.8f}")
    print(f"LANGSMITH root.id={getattr(root, 'id', None)} root.parent={getattr(root, 'parent_run_id', None)} "
          f"llm_runs={len(llm_runs)} names={[r.name for r in llm_runs]}")
    assert root is not None and str(root.id) == tid and root.parent_run_id is None
    assert u.model_calls >= 1 and len(llm_runs) == u.model_calls
    assert u.cost_usd_est > 0 and u.models_used and u.models_used[0].startswith(spec.model)
