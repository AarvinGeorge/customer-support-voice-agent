-- What: D-070 "every ticket has a known requester": remove tickets written under the old unverified rule, then replace
--   the old "unverified tickets are P4 with no account" CHECK with tickets_requester_known (verified, account_id and
--   user_id all present).
-- Use when: run once by infra/migrate.py as core_owner, in one transaction, after 0001.
-- Inputs: schema core, table tickets (0001).
-- Output: the CHECK tickets_requester_known; no ticket row without a requester.
-- Side effects: DELETEs the unattributed tickets with their ticket_events and their stored POST /v1/tickets idempotent
--   responses (a replay must not hand back a ticket that no longer exists). audit_events keep their rows: the table
--   is append-only by design and has no foreign key to tickets. Nobody could follow up such a ticket, so nothing
--   actionable is lost. Safe to run again (the test suite re-runs it after dropping the constraint).
-- Role: the schema half of the invariant; core.tickets.create_ticket refuses first with REQUESTER_UNKNOWN.

DELETE FROM idempotency
 WHERE endpoint = 'POST /v1/tickets'
   AND response -> 'ticket' ->> 'ticket_id' IN (SELECT id FROM tickets
                                                WHERE NOT verified OR account_id IS NULL OR user_id IS NULL);
DELETE FROM ticket_events
 WHERE ticket_id IN (SELECT id FROM tickets WHERE NOT verified OR account_id IS NULL OR user_id IS NULL);
DELETE FROM tickets WHERE NOT verified OR account_id IS NULL OR user_id IS NULL;

-- 0001 declared the old rule as an unnamed CHECK, so find it by its definition rather than a generated name.
DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conname FROM pg_constraint
            WHERE conrelid = 'tickets'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%verified OR%account_id IS NULL%'
  LOOP
    EXECUTE format('ALTER TABLE tickets DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$;

ALTER TABLE tickets DROP CONSTRAINT IF EXISTS tickets_requester_known;
ALTER TABLE tickets ADD CONSTRAINT tickets_requester_known
  CHECK (verified AND account_id IS NOT NULL AND user_id IS NOT NULL);
