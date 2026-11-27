"""What: model registry package (models.yaml loader, chat model factory, usage and price math).
Use when: any agent code needs a model for a role or needs to cost a call.
Inputs: none (see submodules).
Output: re-exports load_models_config, ModelRegistry, Usage (contracts), token_usage, add_usage, normalize_usage, cost_usd_est.
Side effects: none.
Role: single place where provider differences live (D-024).
"""
from agent.models.config import ModelsConfig, Price, RoleSpec, load_models_config
from agent.models.registry import ModelRegistry
from agent.models.usage import add_usage, cost_usd_est, normalize_usage, token_usage
from contracts.events import Usage

__all__ = ["ModelsConfig", "Price", "RoleSpec", "load_models_config", "ModelRegistry",
           "Usage", "add_usage", "cost_usd_est", "normalize_usage", "token_usage"]
