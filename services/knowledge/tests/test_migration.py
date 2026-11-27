"""What: static checks of the knowledge migration file (the live check is `\\d knowledge.chunks` in the stack).
Use when: CI without a database. Inputs: migrations/0001_knowledge_schema.sql. Output: pass or fail.
Side effects: none. Role: keeps the HNSW, GIN and source_url requirements from being edited away."""
from pathlib import Path

SQL = (Path(__file__).resolve().parents[1] / "migrations" / "0001_knowledge_schema.sql").read_text()


def test_indexes_and_tables_present():
    """What: both required indexes, source_url and the three tables exist in the SQL.
    Use when: CI. Inputs: none. Output: assertion. Side effects: none. Role: doc 07 s12 knowledge row."""
    for needle in ("USING hnsw (embedding vector_cosine_ops)", "USING gin (tsv)", "vector(1536)",
                   "CREATE TABLE articles", "CREATE TABLE chunks", "CREATE TABLE ingest_runs"):
        assert needle in SQL, needle
    assert SQL.count("source_url") >= 2


def test_no_em_dash():
    """What: no em dash in the SQL. Use when: CI. Inputs: none. Output: assertion. Side effects: none.
    Role: project style rule."""
    assert chr(0x2014) not in SQL
