-- Tracks whether memory_mcp.notifier already fired a claude.ai routine for
-- a given message, so a restart or the periodic catch-up sweep never fires
-- the same message twice. NULL means "not yet fired" (or reset after a
-- failed fire attempt, so the next sweep retries it); non-NULL is a claim,
-- taken atomically by notifier.py's CLAIM_SQL UPDATE ... WHERE
-- routine_fired_at IS NULL. See server/memory_mcp/notifier.py.

ALTER TABLE messages ADD COLUMN IF NOT EXISTS routine_fired_at TIMESTAMPTZ;

-- Backfill existing rows so the first deploy doesn't fire the whole
-- pre-existing unread backlog through the new routine.
UPDATE messages SET routine_fired_at = now() WHERE routine_fired_at IS NULL;
