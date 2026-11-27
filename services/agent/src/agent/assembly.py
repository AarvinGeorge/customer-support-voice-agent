"""What: the create_agent assembly: middleware stack in doc 07 s2.5 order (as amended by D-046 and
D-048), SupportState, frozen system prompt, checkpointer factories, run config with
thread_id = session_id.
Use when: once per process (or per test) to build the compiled agent graph; per turn for run_config.
Inputs: primary and fallback chat models (from ModelRegistry roles agent and agent_fallback), tools,
a LangGraph checkpointer.
Output: MIDDLEWARE_ORDER, build_middleware, build_agent, build_agent_from_registry, run_config, turn_run_config,
postgres_checkpointer_factory, memory_checkpointer_factory.
Side effects: none at build time (no network); the Postgres factory opens a small connection pool when
entered (the app lifespan enters it once and closes it on shutdown).
Role: composition root of the agent loop. Tool limits live in ToolGuard (D-048), so no
ToolCallLimitMiddleware is added.
"""
from __future__ import annotations

import uuid
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
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent.gates import GroundingVerifier, ScopeClassifier
from agent.middleware.grounding import GroundingMiddleware
from agent.middleware.router import OpenTicket, RouterMiddleware
from agent.middleware.deadline_fault import DeadlineAndFaultMiddleware, fault_run_metadata, model_retry_on
from agent.middleware.tool_guard import ToolGuardMiddleware
from agent.middleware.visibility import ToolVisibilityMiddleware
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


def build_middleware(fallback_model: Any, deadline_kwargs: dict[str, Any] | None = None, *,
                     scope_classifier: ScopeClassifier | None = None,
                     grounding_verifier: GroundingVerifier | None = None,
                     open_ticket: OpenTicket | None = None) -> list[AgentMiddleware[Any, Any]]:
    """What: the middleware list in MIDDLEWARE_ORDER.
    Use when: build_agent; the order test.
    Inputs: the fallback chat model (role agent_fallback); optional DeadlineAndFaultMiddleware overrides (tests);
    the Gate 1 ScopeClassifier (role scope_classifier; None: a `none` verdict counts as on-topic); the Gate 2
    GroundingVerifier (role grounding_verifier; None: Gate 2 off, tests only); the escalation ticket hook used by
    the router and Gate 2 (CS-E-03.S4.T1 supplies it; None: the line gives the turn reference).
    Output: new middleware instances (fresh per agent, no shared state).
    Side effects: none.
    Role: single source of the stack order and the built-in settings (doc 07 s2.5, s8.1).
    """
    return [
        RouterMiddleware(open_ticket=open_ticket, scope_classifier=scope_classifier),
        ModelCallLimitMiddleware(run_limit=limits.MODEL_CALLS_PER_TURN, exit_behavior="end"),
        ToolVisibilityMiddleware(),
        ModelFallbackMiddleware(fallback_model),
        # jitter off: the 0.5 s backoff is exact, as the predicate's budget check assumes (doc 07 s8.2)
        ModelRetryMiddleware(max_retries=limits.MODEL_PRIMARY_RETRIES, retry_on=model_retry_on,
                             initial_delay=limits.MODEL_RETRY_BACKOFF_S, on_failure="error", jitter=False),
        DeadlineAndFaultMiddleware([fallback_model], **(deadline_kwargs or {})),
        AnthropicPromptCachingMiddleware(ttl="5m", unsupported_model_behavior="ignore"),
        GroundingMiddleware(grounding_verifier, open_ticket=open_ticket),
        ToolGuardMiddleware(),
    ]


def build_agent(model: Any, *, fallback_model: Any, checkpointer: BaseCheckpointSaver,
                tools: Sequence[Any] = (), deadline_kwargs: dict[str, Any] | None = None,
                scope_classifier: ScopeClassifier | None = None, grounding_verifier: GroundingVerifier | None = None,
                open_ticket: OpenTicket | None = None) -> Any:
    """What: compile the support agent graph.
    Use when: at startup inside an entered checkpointer factory; tests pass InMemorySaver.
    Inputs: primary model, fallback model, checkpointer, tools (agent.tools.build_tools in the service),
    DeadlineAndFaultMiddleware overrides (tests only), the gate models and the ticket hook (see build_middleware).
    Output: CompiledStateGraph over SupportState.
    Side effects: none.
    Role: create_agent call site.
    """
    return create_agent(model, tools=list(tools), system_prompt=SYSTEM_PROMPT,
                        middleware=build_middleware(fallback_model, deadline_kwargs, scope_classifier=scope_classifier,
                                                    grounding_verifier=grounding_verifier, open_ticket=open_ticket),
                        state_schema=SupportState,
                        checkpointer=checkpointer, name="quillstack_support")


def build_agent_from_registry(registry: Any, *, checkpointer: BaseCheckpointSaver,
                              tools: Sequence[Any] = ()) -> Any:
    """What: build_agent with the agent, agent_fallback, scope_classifier and grounding_verifier roles from
    models.yaml.
    Use when: the service and the real two-turn cache check.
    Inputs: ModelRegistry, checkpointer, tools.
    Output: compiled agent.
    Side effects: none (clients make no request until called).
    Role: wiring helper; the one place the gate models are attached in the service.
    """
    return build_agent(registry.build("agent"), fallback_model=registry.build("agent_fallback"),
                       checkpointer=checkpointer, tools=tools,
                       scope_classifier=ScopeClassifier(registry.build("scope_classifier")),
                       grounding_verifier=GroundingVerifier(registry.build("grounding_verifier")))


def run_config(session_id: str, **extra: Any) -> dict[str, Any]:
    """What: the RunnableConfig for one turn.
    Use when: every ainvoke or astream of the agent.
    Inputs: session_id (uuid string); extra top-level config keys (for example tags, run_id).
    Output: {"configurable": {"thread_id": session_id}, **extra}.
    Side effects: none.
    Role: doc 07 s2.6 thread_id = session_id.
    """
    return {"configurable": {"thread_id": str(session_id)}, **extra}


def turn_run_config(session_id: str, turn_id: str, demo_fault: str | None = None, **extra: Any) -> dict[str, Any]:
    """What: the RunnableConfig for one agent turn: thread_id = session_id and LangSmith root run id = turn_id;
    a faulted turn also gets tag demo_fault and metadata fault_injected=true.
    Use when: every agent turn (stream_agent_turn). Inputs: session_id, turn_id (uuid7 string), the gated demo
    fault (TurnControl.demo_fault), extra keys.
    Output: run_config(...) plus run_id (uuid.UUID of turn_id) and the fault tags. Side effects: none.
    Role: doc 07 s1.2 X-Trace-Id = turn_id = LangSmith root run id (spike 7 PASS); s10 fault tag."""
    return run_config(session_id, run_id=uuid.UUID(str(turn_id)), **fault_run_metadata(demo_fault), **extra)


CHECKPOINTER_POOL_MAX = 4  # concurrent checkpoint reads and writes per replica (turns run in parallel)


def postgres_checkpointer_factory(dsn: str) -> CheckpointerFactory:
    """What: factory for an AsyncPostgresSaver over a small psycopg pool on the agent schema's app role.
    Use when: the app lifespan (`async with factory() as saver`), once per process. Tables are created
    by the migrate runner (D-047), so setup() is not called here.
    Inputs: libpq DSN for agent_app.
    Output: zero-arg callable returning an async context manager that yields the saver.
    Side effects: opens the pool on enter without waiting (startup never blocks on Postgres) and closes
    it on exit. Each checkout waits at most 2 s; each connect and each statement is capped at 2 s
    (doc 07 s8.1 "Postgres checkpointer 2 s, 0 retries"); a broken connection is checked and replaced on
    checkout, so the saver recovers after a Postgres restart (review PR #17 I1).
    Role: production checkpointer seam.
    """
    timeout_s = limits.POSTGRES_CHECKPOINTER_TIMEOUT_S

    @asynccontextmanager
    async def factory() -> AsyncIterator[BaseCheckpointSaver]:
        """What: open the pool and yield the saver. Use when: entered once. Inputs: none. Output: saver.
        Side effects: pool open and close. Role: factory closure."""
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            dsn, min_size=1, max_size=CHECKPOINTER_POOL_MAX, open=False, timeout=timeout_s,
            check=AsyncConnectionPool.check_connection, name="checkpointer",
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
                    "connect_timeout": int(timeout_s),
                    "options": f"-c statement_timeout={int(timeout_s * 1000)}"})
        await pool.open(wait=False)
        try:
            yield AsyncPostgresSaver(conn=pool)
        finally:
            await pool.close()
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
