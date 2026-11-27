"""Shared fakes and helpers for the day-1 agent spikes.

What: fake chat models (scripted replies, scripted failures) and real-model factories.
Use when: a spike needs a model inside `create_agent` without, or with, a provider call.
Inputs: scripted AIMessages or exceptions; env keys from the worktree `.env` for real models.
Output: BaseChatModel instances usable by `create_agent`.
Side effects: `real_model` reads `.env` into the process environment.
Role: test support for spikes/agent only; not product code.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
ANTHROPIC_SPIKE_MODEL = "claude-haiku-5-5"
OPENAI_SPIKE_MODEL = "gpt-5.4-mini"


class ScriptedModel(GenericFakeChatModel):
    """Fake chat model that returns scripted AIMessages and accepts tool binding.

    What: GenericFakeChatModel plus a no-op `bind_tools` and a call log.
    Use when: `create_agent` needs a model and the behaviour under test is framework-side.
    Inputs: `messages`, an iterator of AIMessage (tool calls allowed); `label` names the model.
    Output: the next scripted AIMessage per call (streamed in chunks when streaming).
    Side effects: appends one entry to `calls` per generation.
    Role: stand-in for the agent and fallback models in spikes.
    """

    label: str = "scripted"
    calls: Any = None

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        """Return self, ignoring tools.

        What: makes the fake acceptable to `create_agent`, which binds tools.
        Use when: always, called by the framework.
        Inputs: tools and binding kwargs (ignored).
        Output: this model.
        Side effects: none.
        Role: fake plumbing.
        """
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        """Log the call, then delegate to GenericFakeChatModel.

        What: records one call per generation.
        Use when: framework invokes the model without streaming.
        Inputs: messages and generation kwargs.
        Output: ChatResult with the next scripted message.
        Side effects: appends `label` to `calls`.
        Role: fake plumbing.
        """
        self.calls.append(self.label)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class FailingModel(ScriptedModel):
    """Fake chat model that raises a scripted exception on every call.

    What: a model whose every attempt fails with `error`.
    Use when: testing retry and fallback middleware.
    Inputs: `error`, the exception instance to raise; `label`.
    Output: never returns.
    Side effects: appends `label` to `calls`.
    Role: stand-in for a failing provider.
    """

    error: Any = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        """Log the call and raise the scripted error.

        What: simulates a failed model attempt.
        Use when: framework invokes the model.
        Inputs: messages (ignored).
        Output: none, raises.
        Side effects: appends `label` to `calls`.
        Role: fake plumbing.
        """
        self.calls.append(self.label)
        raise self.error


def scripted(label: str, *replies: AIMessage, calls: list[str] | None = None) -> ScriptedModel:
    """Build a ScriptedModel from replies.

    What: convenience constructor.
    Use when: a spike needs a fake model with fixed replies.
    Inputs: label, replies, optional shared call log.
    Output: ScriptedModel.
    Side effects: none.
    Role: test helper.
    """
    return ScriptedModel(messages=iter(replies), label=label, calls=calls if calls is not None else [])


def tool_call(name: str, args: dict | None = None, call_id: str = "call_1", text: str = "") -> AIMessage:
    """Build an AIMessage carrying one tool call.

    What: shorthand for a model reply that calls a tool.
    Use when: scripting a fake model.
    Inputs: tool name, args, call id, optional text.
    Output: AIMessage with `tool_calls`.
    Side effects: none.
    Role: test helper.
    """
    return AIMessage(content=text, tool_calls=[{"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}])


@dataclass
class TurnControl:
    """Mutable per-turn control object (doc 07 s2.5 item 1, s8.2).

    What: holds `route` and `attempt_role` for one turn.
    Use when: ingress creates one per turn and binds it to `TURN_CONTROL` once.
    Inputs: none at construction.
    Output: an object that tool wrappers and middleware mutate in place.
    Side effects: none.
    Role: spike copy of the design under test (spikes 15 and 16).
    """

    route: str | None = None
    attempt_role: str = "primary"
    events: list[str] = field(default_factory=list)


TURN_CONTROL: ContextVar[TurnControl] = ContextVar("turn_control")


def load_env() -> bool:
    """Load the worktree `.env` without printing it.

    What: reads provider keys into os.environ.
    Use when: a test needs a real provider.
    Inputs: none.
    Output: True when both provider keys are present.
    Side effects: mutates os.environ.
    Role: real-call gate for provider spikes.
    """
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH)
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and bool(os.environ.get("OPENAI_API_KEY"))


def real_model(provider: str):
    """Build the cheapest real chat model for a provider, SDK retries off.

    What: ChatAnthropic `claude-haiku-5-5` or ChatOpenAI `gpt-5.4-mini`, max_retries=0.
    Use when: a spike is about provider behaviour.
    Inputs: provider, "anthropic" or "openai".
    Output: a BaseChatModel.
    Side effects: loads `.env`.
    Role: real-call helper.
    """
    load_env()
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=ANTHROPIC_SPIKE_MODEL, max_retries=0, max_tokens=200, timeout=30)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=OPENAI_SPIKE_MODEL, max_retries=0, timeout=30)
