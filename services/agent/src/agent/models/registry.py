"""What: chat model factory and cost helper over a ModelsConfig.
Use when: the agent, verifier, classifier or evals need a model client for a role.
Inputs: ModelsConfig; role names.
Output: LangChain chat models, Usage and USD estimates.
Side effects: none until a model is called (building makes no network call).
Role: registry adapter; the only place that knows provider kwargs (D-024).
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage

from agent.models.config import CHAT_ROLES, ModelsConfig, RoleSpec
from agent.models.usage import Usage, cost_usd_est, normalize_usage

log = logging.getLogger("agent.models.registry")


class ModelRegistry:
    """What: builds one chat model per role and costs usage.
    Use when: once per process; keep the instance.
    Inputs: ModelsConfig.
    Output: methods build, cost, role_for_model, cost_for_model, usage_of.
    Side effects: none.
    Role: model registry.
    """

    def __init__(self, config: ModelsConfig) -> None:
        self.config = config
        self._warned_models: set[str] = set()

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
        price = self.spec(role).price
        if price is None:  # the config validator requires a price on every role
            raise ValueError(f"role {role}: price block is required")
        return cost_usd_est(usage, price)

    def role_for_model(self, model_name: str) -> str | None:
        """What: the role whose model id the provider-reported name starts with (longest match; providers may
        append a dated suffix, for example gpt-5.4-mini-2026-03-17). Use when: costing a call by the name in
        response_metadata. Inputs: model name. Output: role or None. Side effects: none. Role: lookup."""
        hits = [(len(s.model), r) for r, s in self.config.roles.items()
                if r in CHAT_ROLES and model_name and (model_name == s.model or model_name.startswith(s.model + "-"))]
        return max(hits)[1] if hits else None

    def cost_for_model(self, model_name: str, usage: Usage) -> float:
        """What: USD estimate for usage of the model a response names. Use when: stream_agent_turn(cost_of=...).
        Inputs: provider model name, Usage. Output: float (0.0 for an unknown model, logged by the caller's
        drawer as a zero cost rather than a crash; one warning per name). Side effects: none. Role: models.yaml price lookup."""
        role = self.role_for_model(model_name)
        if role is None:
            if model_name not in self._warned_models:  # one warning per name: a silent $0 hides an alias change
                self._warned_models.add(model_name)
                log.warning("unknown model cost=0 model=%s", model_name)
            return 0.0
        return self.cost(role, usage)

    def usage_of(self, message: AIMessage) -> Usage:
        """What: normalized usage of a model message. Use when: after each model call.
        Inputs: AIMessage. Output: Usage (zeros when absent). Side effects: none. Role: wrapper.
        """
        return normalize_usage(getattr(message, "usage_metadata", None))
