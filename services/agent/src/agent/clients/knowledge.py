"""What: the knowledge client for search_kb, holding only KNOWLEDGE_TOKEN_AGENT (doc 07 s1.4, s4.1).
Use when: the search_kb tool searches help articles.
Inputs: base URL, the knowledge token, an httpx.AsyncClient.
Output: KnowledgeClient.search.
Side effects: HTTP requests to knowledge.
Role: tool route to knowledge. X-Demo-Fault is never sent here (doc 07 s1.2: the agent forwards it to core only).
"""
from __future__ import annotations

from typing import Any

import httpx

from agent.clients.http import base_headers, call_json
from agent.turn_control import TurnControl


class KnowledgeClient:
    """What: knowledge calls on the agent's knowledge token. Use when: search_kb. Inputs: see module.
    Output: JSON bodies. Side effects: HTTP. Role: client."""

    def __init__(self, base_url: str, token: str, *, http: httpx.AsyncClient) -> None:
        """What: keep the settings. Use when: app start or tests. Inputs: base URL, token, client.
        Output: none. Side effects: none. Role: constructor."""
        self._base, self._token, self._http = base_url.rstrip("/"), token, http

    async def search(self, control: TurnControl, body: dict[str, Any]) -> Any:
        """What: POST /v1/search. Use when: search_kb. Inputs: control, SearchRequest JSON (tier and verified
        from claims, never from the model). Output: SearchResponse JSON. Side effects: HTTP. Role: s4.1."""
        return await call_json(self._http, "POST", f"{self._base}/v1/search", base_headers(self._token, control),
                               body=body)
