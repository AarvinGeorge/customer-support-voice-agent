-- What: D-074 card offered: one nullable column on agent.sessions for when code offered the verification card.
-- Use when: run once by infra/migrate.py as agent_owner, after 0002 (the 0900 checkpointer hook runs after all SQL).
-- Inputs: agent.sessions (0001).
-- Output: agent.sessions.card_offered_at timestamptz (null: the card was never offered, or was closed by "I don't have
--   my PIN").
-- Side effects: DDL only; existing rows get null.
-- Role: its own fact, separate from awaiting_verification (credentials expected: the PIN scanner and the D-070 hold
--   read that), so a non-blocking first-message card never makes later numbers PIN candidates (review PR #82 I1, I2).
ALTER TABLE agent.sessions ADD COLUMN IF NOT EXISTS card_offered_at timestamptz;
