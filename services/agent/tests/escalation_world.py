"""What: the shared test world for the escalation tests: Ingress plus TurnRunner plus the full step 13 handler on the
contracts core stub, with a transport that can override any core path (fail, delay, record) and scripted fake models.
Use when: tests/test_escalation.py and tests/test_escalation_timing.py.
Inputs: model replies (or failing models), core overrides, optional tool replacements and deadlines.
Output: World, Fake, Boom, CoreTransport, json_response, error_response, OWNER.
Side effects: none outside the objects it builds.
Role: test wiring (no network, no real model calls)."""
from __future__ import annotations

import asyncio
import inspect
import uuid
from typing import Any, Callable

import httpx
from contracts.agent_api import TurnRequest
from contracts.stubs import examples as ex
from contracts.stubs.core_stub import create_app
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.assembly import build_agent, run_config
from agent.clients.core_agent import CoreAgentClient
from agent.clients.knowledge import KnowledgeClient
from agent.escalation import Escalator
from agent.ingress import Ingress, IngressInput, IngressStream, SessionRow, input_kind_of
from agent.structured.confirm import Confirmer, CoreCommitClient, agent_ticket_opener
from agent.structured.handler import StructuredInput
from agent.tools import build_tools
from agent.turn_body import TurnRunner
from ingress_fakes import MemoryTurnLock, MemoryTurnStore

SID, VISITOR = ex.SESSION_ID, "visitor-1"
OWNER = {"verified": True, "claims_snapshot": ex.CLAIMS.model_dump(mode="json"),
         "context_snapshot": ex.ME_CONTEXT.model_dump(mode="json")}
Handler = Callable[[httpx.Request], Any]


def json_response(body: Any, status: int = 200) -> httpx.Response:
    """What: a JSON response. Use when: overrides. Inputs: body (model or dict), status. Output: response.
    Side effects: none. Role: helper."""
    data = body.model_dump(mode="json", by_alias=True) if hasattr(body, "model_dump") else body
    return httpx.Response(status, json=data)


def error_response(code: str = "CORE_UNAVAILABLE", status: int = 503, changed: bool | None = None) -> httpx.Response:
    """What: a doc 07 s1.3 error envelope. Use when: a failing core path. Inputs: code, status, changed.
    Output: response. Side effects: none. Role: helper."""
    return httpx.Response(status, json={"error": {"class": "core_unavailable", "code": code, "message": "down",
                                                  "retryable": status >= 500, "changed": changed,
                                                  "fault_injected": False, "details": {}}})


class CoreTransport(httpx.AsyncBaseTransport):
    """What: routes each request to an override (by method and path suffix) or to the ASGI stub; records every
    ticket POST. Use when: World. Inputs: stub app, overrides {(METHOD, suffix): handler}. Output: responses.
    Side effects: records `tickets`. Role: fake core with failure knobs."""

    def __init__(self, stub: Any, overrides: dict[tuple[str, str], Handler] | None = None) -> None:
        """What: keep the stub and overrides. Use when: World. Inputs: see class. Output: none. Side effects: none.
        Role: constructor."""
        self.asgi = httpx.ASGITransport(app=stub) if stub is not None else None
        self.overrides = overrides or {}
        self.tickets: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """What: dispatch one request. Use when: httpx. Inputs: request. Output: response. Side effects: records.
        Role: transport."""
        if request.method == "POST" and request.url.path == "/v1/tickets":
            self.tickets.append(request)
        for (method, suffix), handler in self.overrides.items():
            if request.method == method and request.url.path.endswith(suffix):
                res = handler(request)
                if inspect.isawaitable(res):
                    res = await res
                if res is not None:
                    return res
        if self.asgi is None:
            return httpx.Response(404, json={"error": {"class": "not_found", "code": "NOT_FOUND", "message": "",
                                                       "retryable": False, "changed": False,
                                                       "fault_injected": False, "details": {}}})
        return await self.asgi.handle_async_request(request)


class Fake(GenericFakeChatModel):
    """What: scripted fake model; `hang_s` makes every call sleep first (virtual seconds in timing tests).
    Use when: tests. Inputs: messages, hang_s. Output: replies. Side effects: none. Role: test double."""

    hang_s: float = 0.0
    disable_streaming: Any = True  # every call goes through _agenerate

    def bind_tools(self, tools: Any, **kw: Any) -> "Fake":
        """What: no-op. Use when: binding. Inputs: tools. Output: self. Side effects: none. Role: plumbing."""
        return self

    async def _agenerate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        """What: optionally hang, then answer from the script. Use when: each call. Inputs: messages.
        Output: ChatResult. Side effects: none. Role: double."""
        if self.hang_s:
            await asyncio.sleep(self.hang_s)
        return await super()._agenerate(messages, *args, **kwargs)


class Boom(Fake):
    """What: a model whose provider is down. Use when: both-models-unavailable tests. Inputs: none.
    Output: raises. Side effects: none. Role: double."""

    async def _agenerate(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        """What: raise like a provider 5xx. Use when: each call. Inputs: ignored. Output: none.
        Side effects: raises. Role: double."""
        raise RuntimeError("provider unavailable")


class World:
    """What: one session wired end to end. Use when: each test. Inputs: replies or models, core overrides, tool
    replacements {name: tool}, deadlines, whether the stub session starts verified, whether to use the ASGI stub,
    an optional EscalationHolds store (agent.sessions.held_escalation, D-070).
    Output: send(), values(), seed(), transport. Side effects: none. Role: wiring."""

    def __init__(self, replies: list[AIMessage] | None = None, *, model: Any = None, fallback: Any = None,
                 overrides: dict[tuple[str, str], Handler] | None = None, tools: dict[str, Any] | None = None,
                 deadlines: dict[str, float] | None = None, verified: bool = True, use_stub: bool = True,
                 holds: Any = None) -> None:
        """What: build it. Use when: tests. Inputs: see class. Output: none. Side effects: none. Role: constructor."""
        self.stub = create_app() if use_stub else None
        if self.stub is not None and verified:
            self.stub.state.verified.add(SID)
        self.transport = CoreTransport(self.stub, overrides)
        http = httpx.AsyncClient(transport=self.transport)
        core = CoreAgentClient("http://core:8000", "agent-tok", http=http)
        knowledge = KnowledgeClient("http://knowledge:8000", "kn",
                                    http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
        built = {t.name: t for t in build_tools(core, knowledge)}
        built.update(tools or {})
        self.agent = build_agent(model or Fake(messages=iter(replies or [])),
                                 fallback_model=fallback or Fake(messages=iter([])), checkpointer=InMemorySaver(),
                                 tools=list(built.values()))
        confirmer = Confirmer(CoreCommitClient("http://core:8000", "commit-tok", http=http), agent_ticket_opener(core))
        escalator = Escalator(core, holds=holds)
        handler = StructuredInput(core, confirmer, last_turn=None, escalator=escalator)
        self.ingress = Ingress(MemoryTurnStore(SessionRow(session_id=SID, visitor_id=VISITOR)), MemoryTurnLock(),
                               served_by="agent_a", run_agent=TurnRunner(self.agent, core, structured=handler,
                                                                        escalator=escalator),
                               turn_deadline_s=deadlines)

    async def seed(self, values: dict[str, Any]) -> None:
        """What: initial thread state. Use when: arranging. Inputs: values. Output: none. Side effects: checkpoint.
        Role: helper."""
        await self.agent.aupdate_state(run_config(SID), values)

    async def send(self, mode: str = "chat", **body: Any) -> tuple[Any, list[Any]]:
        """What: one turn with a TurnRequest body. Use when: tests. Inputs: mode, text / confirm / verification /
        ticket_offer. Output: (outcome, events). Side effects: as a turn. Role: helper."""
        req = TurnRequest.model_validate({"client_turn_id": str(uuid.uuid4()), **body})
        res = await self.ingress.handle(IngressInput(session_id=SID, visitor_id=VISITOR,
                                                     client_turn_id=str(req.client_turn_id), mode=mode,
                                                     input_kind=input_kind_of(req), text=req.text, body=req))
        assert isinstance(res, IngressStream) and res.task is not None
        _ = [f async for f in res.stream.frames()]
        return await res.task, res.stream.events

    async def values(self) -> dict[str, Any]:
        """What: thread values. Use when: asserting. Inputs: none. Output: dict. Side effects: none. Role: helper."""
        return (await self.agent.aget_state(run_config(SID))).values


def of(events: list[Any], type_: str) -> list[Any]:
    """What: events of one type. Use when: asserting. Inputs: events, type. Output: list. Side effects: none.
    Role: helper."""
    return [e for e in events if e.type == type_]

