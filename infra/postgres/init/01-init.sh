#!/bin/sh
# What: first-start Postgres setup: vector extension, schemas agent/core/knowledge, six roles, grants.
# Use when: the postgres container starts on an EMPTY data volume (docker-entrypoint-initdb.d runs it once).
# Inputs: env POSTGRES_USER, POSTGRES_DB, and {AGENT,CORE,KNOWLEDGE}_{OWNER,APP}_PASSWORD.
# Output: database ready for the migrate jobs. Side effects: creates extension, schemas, roles, privileges.
# Role: doc 07 s12 and s15.1 row 2. Owner roles own their schema and run migrations; app roles get DML only on
#       their own schema. Per-table restrictions (turn_events, audit_events) are applied in each service's migration.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v agent_owner_pw="$AGENT_OWNER_PASSWORD" -v agent_app_pw="$AGENT_APP_PASSWORD" \
  -v core_owner_pw="$CORE_OWNER_PASSWORD" -v core_app_pw="$CORE_APP_PASSWORD" \
  -v knowledge_owner_pw="$KNOWLEDGE_OWNER_PASSWORD" -v knowledge_app_pw="$KNOWLEDGE_APP_PASSWORD" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;

REVOKE ALL ON DATABASE quillstack FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE ROLE agent_owner LOGIN PASSWORD :'agent_owner_pw';
CREATE ROLE agent_app LOGIN PASSWORD :'agent_app_pw';
CREATE ROLE core_owner LOGIN PASSWORD :'core_owner_pw';
CREATE ROLE core_app LOGIN PASSWORD :'core_app_pw';
CREATE ROLE knowledge_owner LOGIN PASSWORD :'knowledge_owner_pw';
CREATE ROLE knowledge_app LOGIN PASSWORD :'knowledge_app_pw';

GRANT CONNECT ON DATABASE quillstack TO agent_owner, agent_app, core_owner, core_app, knowledge_owner, knowledge_app;

CREATE SCHEMA agent AUTHORIZATION agent_owner;
CREATE SCHEMA core AUTHORIZATION core_owner;
CREATE SCHEMA knowledge AUTHORIZATION knowledge_owner;

GRANT USAGE ON SCHEMA agent TO agent_app;
GRANT USAGE ON SCHEMA core TO core_app;
GRANT USAGE ON SCHEMA knowledge TO knowledge_app;

-- tables and sequences a service owner creates later are usable by its own app role only
ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA agent GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agent_app;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA agent GRANT USAGE, SELECT ON SEQUENCES TO agent_app;
ALTER DEFAULT PRIVILEGES FOR ROLE core_owner IN SCHEMA core GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO core_app;
ALTER DEFAULT PRIVILEGES FOR ROLE core_owner IN SCHEMA core GRANT USAGE, SELECT ON SEQUENCES TO core_app;
ALTER DEFAULT PRIVILEGES FOR ROLE knowledge_owner IN SCHEMA knowledge GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO knowledge_app;
ALTER DEFAULT PRIVILEGES FOR ROLE knowledge_owner IN SCHEMA knowledge GRANT USAGE, SELECT ON SEQUENCES TO knowledge_app;

-- unqualified names resolve to the role's own schema; knowledge also needs public for the vector type
ALTER ROLE agent_owner SET search_path = agent;
ALTER ROLE agent_app SET search_path = agent;
ALTER ROLE core_owner SET search_path = core;
ALTER ROLE core_app SET search_path = core;
ALTER ROLE knowledge_owner SET search_path = knowledge, public;
ALTER ROLE knowledge_app SET search_path = knowledge, public;
SQL
