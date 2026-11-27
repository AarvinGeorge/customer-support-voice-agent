"""What: the agent's model tools (doc 07 s5.1, thin slice). Use when: build_agent(tools=build_tools(...)).
Inputs: none. Output: build_tools, TOOL_NAMES. Side effects: none.
Role: package in the CI import guard's scope: nothing here may import the commit or ops client or name their tokens."""
from agent.tools.catalog import TOOL_NAMES, build_tools

__all__ = ["TOOL_NAMES", "build_tools"]
