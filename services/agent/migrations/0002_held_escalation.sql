-- What: D-070 held escalation: one nullable column on agent.sessions for the escalation an unverified session needed.
-- Use when: run once by infra/migrate.py as agent_owner, after 0001 (the 0900 checkpointer hook runs after all SQL).
-- Inputs: agent.sessions (0001).
-- Output: agent.sessions.held_escalation jsonb ({reason, category, summary, turn_id}, or null when nothing is held).
-- Side effects: DDL only; existing rows get null (nothing held).
-- Role: core refuses a ticket without a verified requester, so the agent holds the escalation here, asks for the
--   verification card, and opens the ticket for that person when the card verifies them (agent.escalation).
ALTER TABLE agent.sessions ADD COLUMN IF NOT EXISTS held_escalation jsonb;
