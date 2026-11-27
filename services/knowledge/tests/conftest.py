"""What: shared fixtures for the knowledge service tests.
Use when: pytest collects this package. Inputs: none. Output: fixtures `client` and TOKEN.
Side effects: sets KNOWLEDGE_TOKEN_AGENT and a fake DB url in the environment of the test process.
Role: builds the app against a fake database so unit tests never need Postgres."""
import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-for-knowledge-agent-0123456789"


@pytest.fixture()
def client(monkeypatch):
    """What: TestClient for a fresh app with the agent token set and a fake corpus reader.
    Use when: any HTTP-level test. Inputs: monkeypatch. Output: TestClient.
    Side effects: env vars. Role: isolates tests from the real database."""
    monkeypatch.setenv("KNOWLEDGE_TOKEN_AGENT", TOKEN)
    monkeypatch.setenv("KNOWLEDGE_DB_URL", "postgresql://unused")
    from knowledge import db
    from knowledge.main import create_app

    class FakeDb:
        """What: stand-in for knowledge.db.Database. Use when: unit tests. Inputs: none.
        Output: canned rows. Side effects: none. Role: no Postgres needed."""

        def status(self) -> dict | None:
            """What: canned readiness facts. Use when: readyz. Inputs: none. Output: dict with chunk count and
            corpus version. Side effects: none. Role: fake."""
            return {"chunks": 74, "corpus_version": "abc123"}

        def corpus(self) -> dict:
            """What: canned empty corpus. Use when: GET /v1/corpus. Inputs: none. Output: dict.
            Side effects: none. Role: fake."""
            return {"corpus_version": "none", "articles": [], "last_ingest_at": None}

    monkeypatch.setattr(db, "Database", lambda url: FakeDb())
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c
