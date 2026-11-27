"""What: the Quillstack wire contract package (doc 07 r4) shared by agent, core, knowledge and web.
Use when: any service needs a request, response, event, tool schema, limit, trust wrapper or the
contract version. Import submodules directly (contracts.events, contracts.core_api, ...).
Inputs: none.
Output: VERSION, the contract version string every service logs and returns in /readyz.
Side effects: none.
Role: single source of truth for every shape the three lanes share; doc 07 wins on conflict."""

VERSION = "2026-11-26.r4"

__all__ = ["VERSION"]
