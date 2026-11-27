-- What: knowledge schema tables: articles, chunks (HNSW and GIN indexes), ingest_runs.
-- Use when: the migrate_knowledge job runs as knowledge_owner (search_path = knowledge, public; vector lives in public).
-- Inputs: none. Output: three tables and their indexes. Side effects: DDL; default privileges give knowledge_app DML.
-- Role: doc 07 s12 knowledge row, s4.2; draft A s2.4 plus source_url on articles and chunks (doc 07 s16).
CREATE TABLE articles (
  article_slug    text PRIMARY KEY,
  title           text NOT NULL,
  source_path     text NOT NULL,
  source_url      text NOT NULL,
  tiers           text[] NOT NULL,
  doc_version     integer NOT NULL,
  last_updated    date NOT NULL,
  source_hash     char(64) NOT NULL,
  rendered_hash   char(64) NOT NULL,
  embedding_model text NOT NULL,
  ingested_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE chunks (
  chunk_id          text PRIMARY KEY,
  article_slug      text NOT NULL REFERENCES articles ON DELETE CASCADE,
  ordinal           integer NOT NULL,
  section_path      text[] NOT NULL,
  heading           text NOT NULL,
  header            text NOT NULL,
  body              text NOT NULL,
  source_url        text NOT NULL,
  tiers             text[] NOT NULL
                    CHECK (tiers <@ ARRAY['all','free','pro','business','enterprise']
                           AND cardinality(tiers) > 0),
  error_codes       text[] NOT NULL DEFAULT '{}',
  policy_keys       text[] NOT NULL DEFAULT '{}',
  injection_suspect boolean NOT NULL DEFAULT false,
  token_count       integer NOT NULL,
  content_hash      char(64) NOT NULL,
  embedding_model   text NOT NULL,
  embedding         vector(1536) NOT NULL,
  tsv               tsvector NOT NULL,
  doc_version       integer NOT NULL,
  last_updated      date NOT NULL,
  ingested_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX chunks_tiers_gin ON chunks USING gin (tiers);
CREATE INDEX chunks_hash_idx ON chunks (content_hash, embedding_model);

CREATE TABLE ingest_runs (
  run_id          uuid PRIMARY KEY,
  started_at      timestamptz NOT NULL,
  finished_at     timestamptz,
  status          text NOT NULL CHECK (status IN ('running','ok','failed')),
  corpus_version  char(64),
  embedding_model text NOT NULL,
  articles_seen   integer,
  articles_changed integer,
  chunks_embedded integer,
  chunks_deleted  integer,
  error           text
);
