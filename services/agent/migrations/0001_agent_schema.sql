-- What: the agent schema tables of doc 07 s12 (sessions, voice_sessions, turns, turn_events, transcript_items).
-- Use when: run once by infra/migrate.py as agent_owner, in one transaction, before the 0900 checkpointer hook.
-- Inputs: schema agent (created by infra/postgres/init with default privileges for agent_app).
-- Output: tables, keys, indexes; turn_events restricted to INSERT and SELECT for agent_app.
-- Side effects: DDL in schema agent only (no cross-schema reference).
-- Role: agent service storage; enums mirror packages/contracts (Mode, EndReason, input_kind, transcript role and source).

CREATE TABLE agent.sessions (
  session_id   uuid        PRIMARY KEY,
  visitor_id   text        NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_active_at timestamptz NOT NULL DEFAULT now(),
  ip_hash      text,
  user_turns   integer     NOT NULL DEFAULT 0 CHECK (user_turns >= 0),
  awaiting_verification boolean NOT NULL DEFAULT false,
  verified_user_id text,
  voice_usage  jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX sessions_visitor ON agent.sessions (visitor_id);

CREATE TABLE agent.voice_sessions (
  voice_session_id text      PRIMARY KEY,
  session_id   uuid        NOT NULL REFERENCES agent.sessions(session_id) ON DELETE CASCADE,
  minted_at    timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  ended_at     timestamptz
);
CREATE INDEX voice_sessions_session ON agent.voice_sessions (session_id);

CREATE TABLE agent.turns (
  turn_id      uuid        PRIMARY KEY,
  session_id   uuid        NOT NULL REFERENCES agent.sessions(session_id) ON DELETE CASCADE,
  client_turn_id uuid      NOT NULL,
  mode         text        NOT NULL CHECK (mode IN ('chat', 'voice')),
  input_kind   text        NOT NULL CHECK (input_kind IN ('text', 'confirm', 'verification', 'ticket_offer', 'voice')),
  status       text        NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  end_reason   text        CHECK (end_reason IN ('answered', 'clarify', 'readback', 'verification_requested', 'verified',
                                                 'committed', 'submitted', 'cancelled', 'escalated', 'declined_off_topic',
                                                 'cap_reached', 'limit_reached', 'deadline', 'error', 'swept')),
  deadline_at  timestamptz NOT NULL,
  started_at   timestamptz NOT NULL DEFAULT now(),
  ended_at     timestamptz,
  delivered    boolean     NOT NULL DEFAULT false,
  model_used   text,
  fallback_used boolean    NOT NULL DEFAULT false,
  realtime_call_id text,
  final        jsonb,
  usage        jsonb,
  error        jsonb,
  events_complete boolean  NOT NULL DEFAULT true,
  ls_summary   jsonb,
  eval_run_id  text,
  UNIQUE (session_id, client_turn_id)
);
-- replay lookup for voice (doc 07 s2.4 step 4) and the sweep (step 6)
CREATE INDEX turns_session_call ON agent.turns (session_id, realtime_call_id) WHERE realtime_call_id IS NOT NULL;
CREATE INDEX turns_session_running ON agent.turns (session_id) WHERE status = 'running';

CREATE TABLE agent.turn_events (
  turn_id     uuid        NOT NULL REFERENCES agent.turns(turn_id) ON DELETE CASCADE,
  seq         integer     NOT NULL,
  session_id  uuid        NOT NULL,
  ts          timestamptz NOT NULL,
  type        text        NOT NULL,
  step_id     text,
  payload     jsonb       NOT NULL,
  PRIMARY KEY (turn_id, seq)
);
CREATE INDEX turn_events_session_ts ON agent.turn_events (session_id, ts);
-- doc 07 s12: agent_app INSERT, SELECT only on turn_events (default privileges granted more)
REVOKE UPDATE, DELETE, TRUNCATE ON agent.turn_events FROM agent_app;

CREATE TABLE agent.transcript_items (
  id           uuid        PRIMARY KEY,
  session_id   uuid        NOT NULL REFERENCES agent.sessions(session_id) ON DELETE CASCADE,
  voice_session_id text    REFERENCES agent.voice_sessions(voice_session_id) ON DELETE SET NULL,
  realtime_item_id text,
  turn_id      uuid        REFERENCES agent.turns(turn_id) ON DELETE SET NULL,
  role         text        NOT NULL CHECK (role IN ('user', 'assistant')),
  mode         text        NOT NULL CHECK (mode IN ('chat', 'voice')),
  source       text        NOT NULL CHECK (source IN ('server', 'client')),
  text         text        NOT NULL,
  interrupted  boolean     NOT NULL DEFAULT false,
  merged_into_turn_id uuid REFERENCES agent.turns(turn_id) ON DELETE SET NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, realtime_item_id)
);
CREATE INDEX transcript_items_session_created ON agent.transcript_items (session_id, created_at);
