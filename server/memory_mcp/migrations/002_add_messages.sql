-- Inter-agent messaging channel: a conversational layer alongside the
-- existing cross-project inbox-task mechanism (memory_upsert(...,
-- filed_from_project=...)). Deliberately NOT a node kind: no embedding is
-- computed for a message (memory_upsert always calls embed_one() before
-- touching Postgres, so a chat message would cost an API call and would
-- fail outright during an embedding-provider outage), and messages carry
-- no graph edges. Idempotent — safe to run against an existing database.

-- Drops a since-removed constraint from an earlier draft of this migration
-- (see messages_set_thread() below for where the same rule now lives).
-- Harmless no-op on a fresh install.
ALTER TABLE IF EXISTS messages DROP CONSTRAINT IF EXISTS messages_reply_has_parent;

CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,

    -- The recipient owns the row: their mailbox dies with the project.
    to_project_id   BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- The sender may go away without destroying a message already
    -- received — same asymmetry as nodes.project_id (CASCADE) vs
    -- nodes.filed_from_project_id (SET NULL).
    from_project_id BIGINT REFERENCES projects(id) ON DELETE SET NULL,
    -- Free-text label for which session sent it. Never resolved, never
    -- validated, purely for humans reading a thread.
    from_session    TEXT NOT NULL DEFAULT '',

    -- Conversation grouping key == the root message's id. Deliberately NOT
    -- a foreign key: replies live in the *other* project's mailbox, so a FK
    -- with CASCADE would delete half a conversation when one participant
    -- project is dropped. Assigned by trg_messages_set_thread, never by
    -- the client.
    thread_id       BIGINT NOT NULL,
    in_reply_to     BIGINT REFERENCES messages(id) ON DELETE SET NULL,
    -- 0 at the thread root, parent+1 for each reply. Assigned by trigger,
    -- so a client cannot reset it to dodge the ping-pong cap. The CHECK is
    -- a hard backstop under the (lower, tunable) cap enforced in
    -- messaging.py via MESSAGE_MAX_REPLY_DEPTH.
    reply_depth     INTEGER NOT NULL DEFAULT 0 CHECK (reply_depth BETWEEN 0 AND 32),

    kind            TEXT NOT NULL DEFAULT 'ask'
                        CHECK (kind IN ('ask', 'reply', 'fyi')),
    subject         TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'unread'
                        CHECK (status IN ('unread', 'read', 'answered')),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at         TIMESTAMPTZ,

    -- NOTE: "kind='reply' implies in_reply_to IS NOT NULL" is enforced in
    -- messages_set_thread() below, an INSERT-time trigger, deliberately NOT
    -- a table CHECK. in_reply_to is ON DELETE SET NULL, so a table CHECK
    -- would also run (and fail) when an unrelated project's deletion
    -- cascades and nulls out a *surviving* reply's in_reply_to — an
    -- insert-time rule is not a standing invariant once the referenced
    -- parent can legitimately disappear later.
    CONSTRAINT messages_body_not_blank
        CHECK (btrim(body) <> ''),
    -- A project messaging itself is always a mistake (that's what
    -- memory_upsert is for), and it would make the reply-direction check
    -- in the trigger vacuous.
    CONSTRAINT messages_no_self_send
        CHECK (from_project_id IS NULL OR from_project_id <> to_project_id)
);

-- "Unread for project X" — the /start query and the listener's startup
-- drain. Partial, because virtually every row is eventually read/answered
-- and never scanned again; the index stays roughly mailbox-sized, not
-- table-sized.
CREATE INDEX IF NOT EXISTS idx_messages_unread
    ON messages (to_project_id, created_at DESC) WHERE status = 'unread';

-- Whole-mailbox browsing and "what have I sent that's unanswered".
CREATE INDEX IF NOT EXISTS idx_messages_to_created
    ON messages (to_project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_from_created
    ON messages (from_project_id, created_at DESC);

-- Thread retrieval, already in display order.
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages (thread_id, id);

-- Postgres does not index FK *source* columns. Without this, the self-FK's
-- ON DELETE SET NULL seq-scans the whole table for every deleted message.
CREATE INDEX IF NOT EXISTS idx_messages_in_reply_to
    ON messages (in_reply_to) WHERE in_reply_to IS NOT NULL;

-- ---------------------------------------------------------------------
-- thread_id / reply_depth assignment
--
-- Relies on two documented Postgres orderings: column DEFAULTs (the
-- BIGSERIAL's nextval) are applied to the tuple *before* BEFORE INSERT
-- FOR EACH ROW triggers fire, so NEW.id already holds its final value
-- inside the trigger; and NOT NULL / CHECK constraints are validated
-- *after* BEFORE triggers, so the INSERT can omit thread_id entirely
-- despite the column's NOT NULL. Together these mean no INSERT-then-
-- UPDATE, no currval(), and no client-supplied placeholder — and no
-- client-supplied reply_depth either, which is what makes the ping-pong
-- cap actually a cap.
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION messages_set_thread() RETURNS TRIGGER AS $$
DECLARE
    parent messages%ROWTYPE;
BEGIN
    IF NEW.in_reply_to IS NULL THEN
        IF NEW.kind = 'reply' THEN
            RAISE EXCEPTION 'kind=reply requires in_reply_to to be set';
        END IF;
        NEW.thread_id   := NEW.id;
        NEW.reply_depth := 0;
    ELSE
        SELECT * INTO parent FROM messages WHERE id = NEW.in_reply_to;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'in_reply_to=% does not exist', NEW.in_reply_to;
        END IF;
        NEW.thread_id   := parent.thread_id;
        NEW.reply_depth := parent.reply_depth + 1;
        -- A reply goes back down the wire it came in on.
        IF parent.from_project_id IS NOT NULL
           AND NEW.to_project_id <> parent.from_project_id THEN
            RAISE EXCEPTION
                'reply to message % must be addressed to project % (its sender), got %',
                parent.id, parent.from_project_id, NEW.to_project_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_messages_set_thread ON messages;
CREATE TRIGGER trg_messages_set_thread
    BEFORE INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION messages_set_thread();

-- ---------------------------------------------------------------------
-- Live delivery: NOTIFY on a channel keyed by project id, not slug.
-- LISTEN takes an identifier truncated at NAMEDATALEN-1 = 63 bytes, so a
-- slug-based channel name risks silent cross-project leakage if two long
-- slugs share a 56-char prefix. The listener resolves slug -> id once at
-- startup and reports the channel in its ready line, so a human still
-- only ever types the slug.
--
-- Payload carries identifiers plus truncated, whitespace-collapsed
-- teasers only -- never the full body: NOTIFY payloads are hard-capped
-- at 8000 bytes, and collapsing whitespace guarantees one message == one
-- stdout line == one chat notification for the listener.
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION messages_notify() RETURNS TRIGGER AS $$
DECLARE
    from_slug TEXT;
    to_slug   TEXT;
BEGIN
    SELECT slug INTO from_slug FROM projects WHERE id = NEW.from_project_id;
    SELECT slug INTO to_slug   FROM projects WHERE id = NEW.to_project_id;

    PERFORM pg_notify(
        'mb_msg_' || NEW.to_project_id,
        json_build_object(
            'id',        NEW.id,
            'thread_id', NEW.thread_id,
            'depth',     NEW.reply_depth,
            'kind',      NEW.kind,
            'from',      COALESCE(from_slug, '?'),
            'to',        COALESCE(to_slug, '?'),
            'session',   left(NEW.from_session, 40),
            'subject',   left(regexp_replace(NEW.subject, '\s+', ' ', 'g'), 120),
            'preview',   left(regexp_replace(NEW.body,    '\s+', ' ', 'g'), 200)
        )::text
    );
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_messages_notify ON messages;
CREATE TRIGGER trg_messages_notify
    AFTER INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION messages_notify();
