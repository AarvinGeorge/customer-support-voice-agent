"""What: tests for the create_agent assembly (doc 07 s2.5, s2.6, s8.1, s12; D-046, D-048).
Use when: pytest (fakes only; no provider calls).
Inputs: none. Output: pass/fail. Side effects: none. Role: test."""
from __future__ import annotations

import warnings
from typing import Any

import pytest
from langchain.agents.middleware import ModelCallLimitMiddleware, ModelFallbackMiddleware, ModelRetryMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from contracts import limits
from agent import assembly
from agent.assembly import MIDDLEWARE_ORDER, build_agent, build_middleware, run_config
from agent.middleware import stubs
from agent.prompt import SYSTEM_PROMPT
from agent.state import SupportState


class FakeModel(GenericFakeChatModel):
    """What: scripted fake chat model that records the messages it receives.
    Use when: assembly tests. Inputs: messages iterator. Output: scripted AIMessages.
    Side effects: appends each input message list to `seen`. Role: test double."""

    seen: Any = None

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeModel":
        """What: no-op tool binding. Use when: create_agent binds tools. Inputs: tools.
        Output: self. Side effects: none. Role: fake plumbing."""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        """What: record input, then reply. Use when: framework calls the model. Inputs: messages.
        Output: ChatResult. Side effects: appends to seen. Role: fake plumbing."""
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def fake(*replies: str) -> FakeModel:
    """What: build a FakeModel. Use when: tests. Inputs: reply texts. Output: FakeModel.
    Side effects: none. Role: helper."""
    return FakeModel(messages=iter([AIMessage(content=r) for r in replies]), seen=[])


EXPECTED_ORDER = [
    "RouterMiddleware",
    "ModelCallLimitMiddleware",
    "ToolVisibilityMiddleware",
    "ModelFallbackMiddleware",
    "ModelRetryMiddleware",
    "DeadlineAndFaultMiddleware",
    "AnthropicPromptCachingMiddleware",
    "GroundingMiddleware",
    "ToolGuardMiddleware",
]


def test_middleware_order_matches_doc07_s2_5_with_d048():
    mws = build_middleware(fallback_model=fake("fb"))
    assert [m.name for m in mws] == EXPECTED_ORDER
    assert list(MIDDLEWARE_ORDER) == EXPECTED_ORDER


def test_no_builtin_tool_call_limiters_d046_d048():
    names = [type(m).__name__ for m in build_middleware(fallback_model=fake("fb"))]
    assert "ToolCallLimitMiddleware" not in names


def test_builtin_middleware_settings():
    mws = {m.name: m for m in build_middleware(fallback_model=fake("fb"))}
    mcl = mws["ModelCallLimitMiddleware"]
    assert isinstance(mcl, ModelCallLimitMiddleware)
    assert mcl.run_limit == limits.MODEL_CALLS_PER_TURN == 6 and mcl.exit_behavior == "end"
    assert mcl.thread_limit is None
    retry = mws["ModelRetryMiddleware"]
    assert isinstance(retry, ModelRetryMiddleware)
    assert retry.max_retries == limits.MODEL_PRIMARY_RETRIES == 1
    assert retry.initial_delay == limits.MODEL_RETRY_BACKOFF_S and retry.on_failure == "error"
    assert retry.retry_on is stubs.retry_on_placeholder
    assert isinstance(mws["ModelFallbackMiddleware"], ModelFallbackMiddleware)
    cache = mws["AnthropicPromptCachingMiddleware"]
    assert isinstance(cache, AnthropicPromptCachingMiddleware)
    assert cache.ttl == "5m" and cache.unsupported_model_behavior == "ignore"


def test_every_stub_names_the_task_that_fills_it():
    fills = {m.name: getattr(m, "FILLED_BY", None) for m in build_middleware(fallback_model=fake("fb"))
             if m.name in stubs.STUB_NAMES}
    assert fills == {
        "DeadlineAndFaultMiddleware": "CS-E-07.S1.T2",
        "GroundingMiddleware": "CS-E-02.S4.T2",
        "ToolGuardMiddleware": "CS-E-03.S8.T6",
    }
    assert stubs.retry_on_placeholder.FILLED_BY == "CS-E-07.S1.T2"  # type: ignore[attr-defined]


def test_retry_placeholder_never_retries_so_the_fallback_is_never_retried():
    # Spike 16: without the attempt_role check the fallback would be retried. Until CS-E-07.S1.T2
    # adds the deadline-aware predicate, no attempt is retried.
    from langchain_core.exceptions import ModelAPIError
    assert stubs.retry_on_placeholder(ModelAPIError("5xx")) is False
    assert stubs.retry_on_placeholder(TimeoutError()) is False


def test_jump_capable_hooks_can_end_the_run():
    mws = {m.name: m for m in build_middleware(fallback_model=fake("fb"))}
    router = type(mws["RouterMiddleware"]).abefore_model
    grounding = type(mws["GroundingMiddleware"]).aafter_model
    assert getattr(router, "__can_jump_to__", None) == ["end"]
    assert getattr(grounding, "__can_jump_to__", None) == ["end"]


def test_compiled_graph_has_hook_nodes_and_state_fields():
    agent = build_agent(fake("hi"), fallback_model=fake("fb"), checkpointer=InMemorySaver())
    nodes = set(agent.get_graph().nodes)
    assert {"model", "RouterMiddleware.before_model", "ModelCallLimitMiddleware.before_model",
            "GroundingMiddleware.after_model"} <= nodes
    channels = set(agent.channels)
    for f in ("messages", "verified", "claims_snapshot", "context_snapshot", "pending",
              "weak_streak", "grounding_failures", "last_turn_delivered"):
        assert f in channels, f


def test_state_schema_fields_from_doc07_s12():
    keys = set(SupportState.__annotations__)
    assert {"verified", "claims_snapshot", "context_snapshot", "pending", "weak_streak",
            "grounding_failures", "last_turn_delivered"} <= keys
    # one authority per fact: these live only in agent.sessions
    assert "awaiting_verification" not in keys and "user_turns" not in keys


async def test_frozen_system_prompt_reaches_the_model_first():
    model = fake("hello")
    agent = build_agent(model, fallback_model=fake("fb"), checkpointer=InMemorySaver())
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # caching middleware must be a silent no-op on non-Anthropic
        await agent.ainvoke({"messages": [HumanMessage("hi")]}, run_config("s-1"))
    first = model.seen[0][0]
    assert isinstance(first, SystemMessage) and first.content == SYSTEM_PROMPT


async def test_thread_id_is_session_id_turns_share_history_and_sessions_are_isolated():
    model = fake("a1", "a2", "b1")
    agent = build_agent(model, fallback_model=fake("fb"), checkpointer=InMemorySaver())
    await agent.ainvoke({"messages": [HumanMessage("turn one")]}, run_config("session-A"))
    await agent.ainvoke({"messages": [HumanMessage("turn two")]}, run_config("session-A"))
    await agent.ainvoke({"messages": [HumanMessage("other")]}, run_config("session-B"))
    second = [m.content for m in model.seen[1][1:]]
    assert second == ["turn one", "a1", "turn two"]
    other = [m.content for m in model.seen[2][1:]]
    assert other == ["other"]
    assert run_config("session-A")["configurable"] == {"thread_id": "session-A"}


async def test_memory_checkpointer_factory_yields_an_in_memory_saver():
    async with assembly.memory_checkpointer_factory()() as saver:
        assert isinstance(saver, InMemorySaver)


async def test_caching_middleware_marks_the_system_prompt_on_anthropic_models():
    mw = {m.name: m for m in build_middleware(fallback_model=fake("fb"))}["AnthropicPromptCachingMiddleware"]
    model = ChatAnthropic(model="claude-sonnet-5-5", api_key="sk-test-not-real", max_retries=0)  # pyright: ignore[reportCallIssue]  # pydantic alias
    req = ModelRequest(model=model, messages=[HumanMessage("hi")], system_message=SystemMessage(SYSTEM_PROMPT),
                       tools=[], state={"messages": []}, runtime=None)  # type: ignore[arg-type]
    captured: list[ModelRequest] = []

    async def handler(r: ModelRequest) -> Any:
        """What: capture the request. Use when: this test. Inputs: request. Output: AIMessage.
        Side effects: appends. Role: fake model handler."""
        captured.append(r)
        return AIMessage("ok")

    await mw.awrap_model_call(req, handler)
    sent = captured[0]
    assert sent.model_settings.get("cache_control") == {"type": "ephemeral", "ttl": "5m"}
    assert sent.system_message is not None
    blocks = sent.system_message.content
    assert isinstance(blocks, list) and isinstance(blocks[-1], dict) and blocks[-1].get("cache_control") == {"type": "ephemeral", "ttl": "5m"}


@pytest.mark.parametrize("name", sorted(stubs.STUB_NAMES))
async def test_stubs_are_pass_through(name: str):
    mw = {m.name: m for m in build_middleware(fallback_model=fake("fb"))}[name]
    if name == "GroundingMiddleware":
        assert await mw.aafter_model({"messages": []}, None) is None  # pyright: ignore[reportArgumentType]  # stub ignores runtime
    if name == "DeadlineAndFaultMiddleware":
        out = await mw.awrap_model_call("REQ", _echo)  # pyright: ignore[reportArgumentType]  # pass-through probe
        assert out == ("handled", "REQ")
    if name == "ToolGuardMiddleware":
        out = await mw.awrap_tool_call("CALL", _echo)  # pyright: ignore[reportArgumentType]  # pass-through probe
        assert out == ("handled", "CALL")


async def _echo(x: Any) -> tuple[str, Any]:
    """What: async handler that echoes. Use when: stub tests. Inputs: x. Output: tuple.
    Side effects: none. Role: helper."""
    return ("handled", x)


def _closed_port_dsn() -> str:
    """What: a DSN on a localhost port nothing listens on. Use when: checkpointer timeout tests.
    Inputs: none. Output: DSN. Side effects: binds and closes one socket. Role: helper."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"postgresql://agent_app:x@127.0.0.1:{port}/qs"


async def test_postgres_checkpointer_is_a_pool_with_2s_connect_and_statement_timeouts():
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    async with assembly.postgres_checkpointer_factory(_closed_port_dsn())() as saver:
        pool = saver.conn
        assert isinstance(saver, assembly.AsyncPostgresSaver) and isinstance(pool, AsyncConnectionPool)
        assert pool.timeout == limits.POSTGRES_CHECKPOINTER_TIMEOUT_S == 2
        kw: Any = pool.kwargs
        assert kw["connect_timeout"] == 2 and "statement_timeout=2000" in kw["options"]
        assert kw["autocommit"] is True and kw["prepare_threshold"] == 0
        assert kw["row_factory"] is dict_row
        assert pool._check is AsyncConnectionPool.check_connection  # a dead connection is replaced on checkout
    assert pool.closed


async def test_postgres_checkpointer_on_a_closed_port_fails_in_about_2s_and_keeps_trying():
    import time

    async with assembly.postgres_checkpointer_factory(_closed_port_dsn())() as saver:
        for _ in range(2):  # the second call proves the saver is not dead after the first failure
            t = time.monotonic()
            with pytest.raises(Exception) as exc:
                await saver.aget_tuple(run_config("019a7c2e-5b10-7000-8000-00000000a001"))
            took = time.monotonic() - t
            assert 1.5 <= took < 3.5, took
            assert type(exc.value).__name__ in ("PoolTimeout", "OperationalError")
