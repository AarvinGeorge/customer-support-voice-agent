"""What: model registry package (models.yaml loader, chat model factory, usage and price math).
Use when: any agent code needs a model for a role or needs to cost a call.
Inputs: none (see submodules).
Output: re-exports load_models_config, ModelRegistry, Usage, normalize_usage, cost_usd_est.
Side effects: none.
Role: single place where provider differences live (D-024).
"""
from agent.models.config import ModelsConfig, Price, RoleSpec, load_models_config
from agent.models.registry import ModelRegistry
from agent.models.usage import Usage, cost_usd_est, normalize_usage

__all__ = ["ModelsConfig", "Price", "RoleSpec", "load_models_config", "ModelRegistry",
           "Usage", "cost_usd_est", "normalize_usage"]
