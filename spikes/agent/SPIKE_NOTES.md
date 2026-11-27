# Day-1 agent spikes (doc 07 s14), lane B

Versions under test (pinned in `pyproject.toml`, locked in `uv.lock`): langchain 1.5.2, langchain-core 1.8.5, langgraph 1.2.24, langgraph-checkpoint 4.3.2, langchain-anthropic 1.7.13, langchain-openai 1.8.5. Real models: `claude-haiku-5-5`, `gpt-5.4-mini` (SDK `max_retries=0`).

Run: `cd spikes/agent && uv sync && uv run pytest -m "not real"` (fakes only) or `uv run pytest` (adds real provider calls).

| # | Question | Result | Evidence | Decision for lane B |
|---|---|---|---|---|
| 15 | A tool or tool wrapper mutates `TurnControl.route` on the object bound once before `astream`; does the next `before_model` see it, on both providers? | PASS | `spike_15_turn_control.py`: async tool, sync tool (executor thread) and `awrap_tool_call` mutation all give router events `['router_saw:None', 'router_saw:verify']`, last message the fixed verify line, one model call. Negative control `test_negative_control_contextvar_set_inside_tool_is_invisible` proves `ContextVar.set()` inside a tool is NOT seen (route None, model called twice), so the measurement can fail. Real: `test_real_provider_tool_sets_route[anthropic]` and `[openai]` PASSED. | Keep the doc 07 s2.5 design: ingress sets the context variable once, everything else mutates the object. Never call `.set()` below ingress. `Command` fallback not needed. |
