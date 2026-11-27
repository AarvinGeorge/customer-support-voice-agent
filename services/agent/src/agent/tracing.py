"""What: the LangSmith client with hide_inputs and hide_outputs set to contracts hide_for_langsmith (doc 07 s11.1:
"LangSmith client is created with hide_inputs and hide_outputs set to redact"), installed as the global client so
every LangChain tracer uses it.
Use when: the production lifespan calls configure_tracing once; tests build a client with a captured session.
Inputs: env LANGSMITH_API_KEY (or LANGCHAIN_API_KEY) and the usual LANGSMITH_* variables read by the SDK.
Output: build_langsmith_client, configure_tracing.
Side effects: configure_tracing replaces langsmith's cached global client; nothing connects until a run is traced.
Role: second redaction net for traces (ingress already redacted the text with the session's context).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from contracts.redact import hide_for_langsmith
from langsmith import Client
from langsmith.run_trees import configure

log = logging.getLogger("agent.tracing")


def build_langsmith_client(**kwargs: Any) -> Client:
    """What: a LangSmith Client whose run inputs and outputs pass through hide_for_langsmith.
    Use when: configure_tracing; tests. Inputs: extra Client keyword arguments (api_key, api_url, session...).
    Output: Client. Side effects: none (no request until a run is posted). Role: the one way traces leave."""
    return Client(hide_inputs=hide_for_langsmith, hide_outputs=hide_for_langsmith, **kwargs)


def configure_tracing() -> Client | None:
    """What: install the hiding client as langsmith's global client when an API key is set.
    Use when: the production lifespan. Inputs: env. Output: the client, or None when no key is set (tracing
    cannot post anything then). Side effects: langsmith global client. Role: wiring."""
    if not (os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")):
        log.info("no LangSmith API key: traces are not sent")
        return None
    client = build_langsmith_client()
    configure(client=client)
    return client
