"""What: chat model factory and cost helper over a ModelsConfig.
Use when: the agent, verifier, classifier or evals need a model client for a role.
Inputs: ModelsConfig; role names.
Output: LangChain chat models, Usage and USD estimates.
Side effects: none until a model is called (building makes no network call).
Role: registry adapter; the only place that knows provider kwargs (D-024).
"""
from __future__ import annotations

from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage

from agent.models.config import CHAT_ROLES, ModelsConfig, RoleSpec
from agent.models.usage import Usage, cost_usd_est, normalize_usage


class ModelRegistry:
    """What: builds one chat model per role and costs usage.
    Use when: once per process; keep the instance.
    Inputs: ModelsConfig.
    Output: methods build, cost, usage_of.
    Side effects: none.
    Role: model registry.
    """

    def __init__(self, config: ModelsConfig) -> None:
        self.config = config

    def spec(self, role: str) -> RoleSpec:
        """What: look up a role. Use when: reading timeouts or ids. Inputs: role name.
        Output: RoleSpec; KeyError when unknown. Side effects: none. Role: lookup.
        """
        return self.config.roles[role]

    def build(self, role: str) -> Any:
        """What: init_chat_model for a role with SDK retries 0 and the role's timeout.
        Use when: assembling the agent, Gate 2 and the scope classifier.
        Inputs: one of agent, agent_fallback, grounding_verifier, scope_classifier, judge.
        Output: a LangChain chat model.
        Side effects: none (no request is sent).
        Role: factory; effort maps to `effort` (anthropic) or `reasoning_effort` (openai).
        """
        s = self.spec(role)
        if role not in CHAT_ROLES:
            raise ValueError(f"role {role} is not a chat role")
        kw: dict[str, Any] = {"model_provider": s.provider, "max_retries": 0,
                              "timeout": s.attempt_timeout_s}
        if s.max_tokens is not None:
            kw["max_tokens"] = s.max_tokens
        if s.effort is not None:
            kw["effort" if s.provider == "anthropic" else "reasoning_effort"] = s.effort
        return init_chat_model(s.model, **kw)

    def cost(self, role: str, usage: Usage) -> float:
        """What: USD estimate for usage under a role's price. Use when: filling cost_usd_est.
        Inputs: role, Usage. Output: float. Side effects: none. Role: price math wrapper.
        """
        return cost_usd_est(usage, self.spec(role).price)

    def usage_of(self, message: AIMessage) -> Usage:
        """What: normalized usage of a model message. Use when: after each model call.
        Inputs: AIMessage. Output: Usage (zeros when absent). Side effects: none. Role: wrapper.
        """
        return normalize_usage(getattr(message, "usage_metadata", None))
