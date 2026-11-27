"""What: the create_agent assembly: middleware stack in doc 07 s2.5 order (as amended by D-046 and
D-048), SupportState, frozen system prompt, checkpointer factories, run config with
thread_id = session_id.
Use when: once per process (or per test) to build the compiled agent graph; per turn for run_config.
Inputs: primary and fallback chat models (from ModelRegistry roles agent and agent_fallback), tools,
a LangGraph checkpointer.
Output: MIDDLEWARE_ORDER, build_middleware, build_agent, build_agent_from_registry, run_config,
postgres_checkpointer_factory, memory_checkpointer_factory.
Side effects: none at build time (no network); the Postgres factory opens a connection when entered.
Role: composition root of the agent loop. Tool limits live in ToolGuard (D-048), so no
ToolCallLimitMiddleware is added.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Sequence

from contracts import limits
from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ModelFallbackMiddleware,
    ModelRetryMiddleware,
)
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent.middleware.stubs import (
    DeadlineAndFaultMiddleware,
    GroundingMiddleware,
    RouterMiddleware,
    ToolGuardMiddleware,
    ToolVisibilityMiddleware,
    retry_on_placeholder,
)
from agent.prompt import SYSTEM_PROMPT
from agent.state import SupportState

# doc 07 s2.5 order; item 3 (three ToolCallLimitMiddleware) removed by D-048, its limits moved
# into ToolGuardMiddleware (item 10).
MIDDLEWARE_ORDER: tuple[str, ...] = (
    "RouterMiddleware",                  # 1  before_model, can_jump_to end (CS-E-03.S8.T4)
    "ModelCallLimitMiddleware",          # 2  run_limit 6, exit_behavior end
    "ToolVisibilityMiddleware",          # 4  wrap_model_call, V0/V1/V2 (CS-E-03.S8.T4)
    "ModelFallbackMiddleware",           # 5  outer model wrapper
    "ModelRetryMiddleware",              # 6  1 retry, 0.5 s, on_failure error
    "DeadlineAndFaultMiddleware",        # 7  innermost model wrapper (CS-E-07.S1.T2)
    "AnthropicPromptCachingMiddleware",  # 8  ttl 5m, no-op for non-Anthropic
    "GroundingMiddleware",               # 9  after_model, Gate 2 (CS-E-02.S4.T2)
    "ToolGuardMiddleware",               # 10 wrap_tool_call plus combined tool limiter (CS-E-03.S8.T6)
)

CheckpointerFactory = Callable[[], Any]  # returns an async context manager yielding a saver


def build_middleware(fallback_model: Any) -> list[AgentMiddleware]:
    """What: the middleware list in MIDDLEWARE_ORDER.
    Use when: build_agent; the order test.
    Inputs: the fallback chat model (role agent_fallback).
    Output: new middleware instances (fresh per agent, no shared state).
    Side effects: none.
    Role: single source of the stack order and the built-in settings (doc 07 s2.5, s8.1).
    """
    return [
        RouterMiddleware(),
        ModelCallLimitMiddleware(run_limit=limits.MODEL_CALLS_PER_TURN, exit_behavior="end"),
        ToolVisibilityMiddleware(),
        ModelFallbackMiddleware(fallback_model),
        ModelRetryMiddleware(max_retries=limits.MODEL_PRIMARY_RETRIES, retry_on=retry_on_placeholder,
                             initial_delay=limits.MODEL_RETRY_BACKOFF_S, on_failure="error"),
        DeadlineAndFaultMiddleware(),
        AnthropicPromptCachingMiddleware(ttl="5m", unsupported_model_behavior="ignore"),
        GroundingMiddleware(),
        ToolGuardMiddleware(),
    ]


def build_agent(model: Any, *, fallback_model: Any, checkpointer: BaseCheckpointSaver,
                tools: Sequence[Any] = ()) -> Any:
    """What: compile the support agent graph.
    Use when: at startup inside an entered checkpointer factory; tests pass InMemorySaver.
    Inputs: primary model, fallback model, checkpointer, tools (filled by CS-E-03.S8.T6).
    Output: CompiledStateGraph over SupportState.
    Side effects: none.
    Role: create_agent call site.
    """
    return create_agent(model, tools=list(tools), system_prompt=SYSTEM_PROMPT,
                        middleware=build_middleware(fallback_model), state_schema=SupportState,
                        checkpointer=checkpointer, name="quillstack_support")


def build_agent_from_registry(registry: Any, *, checkpointer: BaseCheckpointSaver,
                              tools: Sequence[Any] = ()) -> Any:
    """What: build_agent with the agent and agent_fallback roles from models.yaml.
    Use when: the service and the real two-turn cache check.
    Inputs: ModelRegistry, checkpointer, tools.
    Output: compiled agent.
    Side effects: none (clients make no request until called).
    Role: wiring helper.
    """
    return build_agent(registry.build("agent"), fallback_model=registry.build("agent_fallback"),
                       checkpointer=checkpointer, tools=tools)


def run_config(session_id: str, **extra: Any) -> dict[str, Any]:
    """What: the RunnableConfig for one turn.
    Use when: every ainvoke or astream of the agent.
    Inputs: session_id (uuid string); extra top-level config keys (for example tags, run_id).
    Output: {"configurable": {"thread_id": session_id}, **extra}.
    Side effects: none.
    Role: doc 07 s2.6 thread_id = session_id.
    """
    return {"configurable": {"thread_id": str(session_id)}, **extra}


def postgres_checkpointer_factory(dsn: str) -> CheckpointerFactory:
    """What: factory for an AsyncPostgresSaver on the agent schema's app role.
    Use when: service startup (`async with factory() as saver`). Tables are created by the
    migrate runner (D-047), so setup() is not called here; timeouts and the Postgres read-back are
    verified in the T7 spikes.
    Inputs: libpq DSN for agent_app.
    Output: zero-arg callable returning an async context manager that yields the saver.
    Side effects: opens and closes one connection when entered and exited.
    Role: production checkpointer seam.
    """
    def factory() -> Any:
        """What: open the saver. Use when: entered once. Inputs: none. Output: async cm.
        Side effects: connects on enter. Role: factory closure."""
        return AsyncPostgresSaver.from_conn_string(dsn)
    return factory


def memory_checkpointer_factory() -> CheckpointerFactory:
    """What: factory for an in-memory saver with the same interface as the Postgres one.
    Use when: unit tests and the real two-turn cache check (no database).
    Inputs: none.
    Output: zero-arg callable returning an async context manager that yields an InMemorySaver.
    Side effects: none.
    Role: test checkpointer seam.
    """
    @asynccontextmanager
    async def factory() -> AsyncIterator[BaseCheckpointSaver]:
        """What: yield a fresh InMemorySaver. Use when: entered. Inputs: none. Output: saver.
        Side effects: none. Role: factory."""
        yield InMemorySaver()
    return factory
