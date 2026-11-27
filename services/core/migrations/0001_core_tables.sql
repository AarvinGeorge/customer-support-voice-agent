-- What: the core schema tables the thin slice touches (doc 07 s12 core row), with the s7.1 unique indexes and
--       the append-only protection of audit_events. Source: doc 07 s7.1, s12, s11; DDL shaped by L2 draft C section 2.2.
-- Use when: the migrate_core job runs as core_owner (search_path = core, set on the role). Runs once.
-- Inputs: none. Output: tables, indexes, triggers. Side effects: DDL.
-- Role: operators and the ah_email_daily index are designed only (doc 09 s1.2) and are not created; billing_email,
--       grace_extension and notification_prefs stay valid action_type values because they are wire literals.
-- Note: default privileges (infra/postgres/init) already give core_app SELECT, INSERT, UPDATE, DELETE on every table
--       core_owner creates; the REVOKEs below narrow that for the append-only tables.

-- Key/value facts about the data set (the seeder writes seed_date; /readyz reads it). Not in doc 07 s12: additive.
CREATE TABLE meta (
  key   text PRIMARY KEY,
  value text NOT NULL
);

CREATE TABLE accounts (
  id               text PRIMARY KEY CHECK (id ~ '^QS-[0-9]{5}$'),
  name             text NOT NULL,
  tier             text NOT NULL CHECK (tier IN ('free','pro','business','enterprise')),
  contract_status  text NOT NULL CHECK (contract_status IN ('trialing','active','past_due','in_notice','cancelled')),
  seats            int  NOT NULL CHECK (seats > 0),
  billing_email    text,
  grace_until      date,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id                   text PRIMARY KEY CHECK (id ~ '^U-[0-9]{4}$'),
  account_id           text NOT NULL REFERENCES accounts(id),
  display_name         text NOT NULL,
  role                 text NOT NULL CHECK (role IN ('owner','billing_admin','member')),
  pin_hmac             bytea NOT NULL,            -- HMAC-SHA256(PIN_PEPPER, account_id:pin); never returned or audited
  notification_prefs   jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (account_id, pin_hmac)
);

CREATE TABLE charges (
  id              text PRIMARY KEY CHECK (id ~ '^CH-[0-9]{8}$'),
  account_id      text NOT NULL REFERENCES accounts(id),
  invoice_number  text UNIQUE,
  charged_at      timestamptz NOT NULL,
  amount_cents    int NOT NULL CHECK (amount_cents > 0),
  currency        text NOT NULL DEFAULT 'USD',
  description     text NOT NULL,
  status          text NOT NULL CHECK (status IN ('paid','failed','refunded')),
  refunded_at     timestamptz,
  CHECK ((status = 'refunded') = (refunded_at IS NOT NULL))
);
CREATE INDEX charges_account_idx ON charges (account_id, charged_at DESC);

CREATE TABLE session_claims (
  session_id        uuid PRIMARY KEY,
  user_id           text REFERENCES users(id),
  account_id        text REFERENCES accounts(id),
  verified_at       timestamptz,
  expires_at        timestamptz,
  failed_verifies   int NOT NULL DEFAULT 0,
  verify_locked     boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CHECK ((user_id IS NULL) = (account_id IS NULL))
);

CREATE TABLE verify_attempts (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id          uuid NOT NULL,
  claimed_account_id  text NOT NULL,              -- as spoken, may not exist
  kind                text NOT NULL CHECK (kind IN ('verify','step_up')),
  ok                  boolean NOT NULL,
  at                  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX verify_attempts_acct_idx ON verify_attempts (claimed_account_id, at DESC);

-- doc 07 s7.1 states, s3.3 pending_kind and turn ids
CREATE TABLE pending_actions (
  id                text PRIMARY KEY CHECK (id ~ '^PA-[0-9A-F]{8}$'),
  session_id        uuid NOT NULL,
  account_id        text NOT NULL REFERENCES accounts(id),
  user_id           text NOT NULL REFERENCES users(id),
  action_type       text NOT NULL CHECK (action_type IN ('refund','billing_email','grace_extension')),
  pending_kind      text NOT NULL CHECK (pending_kind IN ('commit','submit_for_approval')),
  target_key        text NOT NULL,                -- 'refund:CH-00000412'
  params            jsonb NOT NULL,               -- frozen at prepare; re-checked at commit
  decision          text NOT NULL CHECK (decision IN ('allowed','needs_approval')),
  reason_codes      text[] NOT NULL,
  policy_version    text NOT NULL,
  state             text NOT NULL CHECK (state IN ('prepared','awaiting_customer','awaiting_approval','committed',
                      'expired','superseded','cancelled','rejected','rules_changed')),
  prepare_turn_id   text NOT NULL,
  submit_turn_id    text,
  requires_step_up  boolean NOT NULL DEFAULT false,
  step_up_at        timestamptz,
  ticket_id         text,                         -- set on the approval path (the ticket is created at submit)
  result            jsonb,                        -- stored commit or submit result (replay)
  created_at        timestamptz NOT NULL DEFAULT now(),
  expires_at        timestamptz,                  -- NULL for awaiting_approval
  decided_at        timestamptz
);
-- doc 07 s7.1: one open row per session (a "yes" can only mean one thing)
CREATE UNIQUE INDEX pa_one_open_per_session ON pending_actions (session_id)
  WHERE state IN ('prepared','awaiting_customer');
-- doc 07 s7.1: one live row per target across all sessions
CREATE UNIQUE INDEX pa_one_live_per_target ON pending_actions (target_key)
  WHERE state IN ('prepared','awaiting_customer','awaiting_approval');

CREATE TABLE action_history (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pending_action_id  text UNIQUE REFERENCES pending_actions(id),
  account_id         text NOT NULL REFERENCES accounts(id),
  user_id            text REFERENCES users(id),
  action_type        text NOT NULL CHECK (action_type IN ('refund','billing_email','grace_extension','notification_prefs')),
  target_key         text NOT NULL,
  amount_cents       int,
  before_value       jsonb,
  after_value        jsonb,
  effective_date     date NOT NULL,               -- business date in the policy timezone
  committed_at       timestamptz NOT NULL DEFAULT now(),
  committed_by       text NOT NULL CHECK (committed_by IN ('customer','operator')),
  operator_id        text                         -- no FK: the operators table is designed only
);
-- a charge can be refunded once, whatever the code path (doc 07 s3.1 memory contract)
CREATE UNIQUE INDEX ah_refund_once ON action_history (target_key) WHERE action_type = 'refund';
CREATE INDEX ah_account_type_idx ON action_history (account_id, action_type, effective_date DESC);

CREATE TABLE tickets (
  id                 text PRIMARY KEY CHECK (id ~ '^TCK-[0-9]{8}$'),
  session_id         uuid NOT NULL,
  verified           boolean NOT NULL,
  account_id         text REFERENCES accounts(id),
  user_id            text REFERENCES users(id),
  stage              text NOT NULL CHECK (stage IN ('open','pending_approval','resolved','closed')),
  queue              text NOT NULL CHECK (queue IN ('billing','technical','account','approvals')),
  category           text NOT NULL,
  priority           text NOT NULL CHECK (priority IN ('P1','P2','P3','P4')),
  target_label       text NOT NULL,
  summary            text NOT NULL CHECK (length(summary) <= 2000),
  steps_tried        text[] NOT NULL DEFAULT '{}',
  confidence         numeric(4,3),
  transcript_ref     text,
  origin             text CHECK (origin IN ('model','code','core')),
  escalation_reason  text,
  pending_action_id  text UNIQUE REFERENCES pending_actions(id),
  outcome            text CHECK (outcome IN ('answered','approved_executed','rejected','rules_changed','resolved_by_operator')),
  outcome_note       text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  created_date       date NOT NULL,               -- business date, for the daily limit
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (verified OR (account_id IS NULL AND user_id IS NULL AND priority = 'P4')),
  CHECK (stage <> 'pending_approval' OR pending_action_id IS NOT NULL)
);
CREATE INDEX tickets_account_idx ON tickets (account_id, category, stage);
CREATE INDEX tickets_queue_idx ON tickets (queue, stage, created_at);

CREATE TABLE ticket_events (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id   text NOT NULL REFERENCES tickets(id),
  kind        text NOT NULL CHECK (kind IN ('created','note','merged','stage_changed','approved','rejected')),
  from_stage  text,
  to_stage    text,
  actor       text NOT NULL CHECK (actor IN ('customer','agent','operator','system')),
  actor_id    text,
  body        text,
  at          timestamptz NOT NULL DEFAULT now()
);

-- Idempotency for mutations (doc 07 s1.2 Idempotency-Key): one stored response per (caller, key)
CREATE TABLE idempotency (
  key             text NOT NULL,
  caller          text NOT NULL,
  endpoint        text NOT NULL,
  request_sha256  bytea NOT NULL,
  status_code     int NOT NULL,
  response        jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (caller, key)
);

-- Audit (append-only). args_redacted holds redact() output only; args_sha256 is over that redacted JSON.
CREATE TABLE audit_events (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ts                 timestamptz NOT NULL DEFAULT now(),   -- the writer always passes the injected clock
  trace_id           text,
  session_id         uuid,
  mode               text CHECK (mode IN ('chat','voice','operator','system')),
  actor              text NOT NULL CHECK (actor IN ('customer','agent','operator','system')),
  actor_id           text,
  account_id         text,
  event              text NOT NULL,
  action_type        text,
  pending_action_id  text,
  ticket_id          text,
  decision           text CHECK (decision IN ('allowed','needs_approval','declined','error')),
  reason_codes       text[],
  policy_version     text,
  args_redacted      jsonb,
  args_sha256        bytea,
  result_status      int,
  idempotency_key    text
);
CREATE INDEX audit_session_idx ON audit_events (session_id, ts);

CREATE FUNCTION audit_events_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'audit_events is append-only'; END $$;
-- the trigger also stops the table owner (privileges alone do not: an owner holds all of them)
CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_events
  FOR EACH STATEMENT EXECUTE FUNCTION audit_events_immutable();

REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM core_app;
REVOKE UPDATE, DELETE, TRUNCATE ON action_history FROM core_app;
REVOKE DELETE, TRUNCATE ON ticket_events FROM core_app;
