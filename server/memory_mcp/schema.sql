-- Memory Bank MCP schema
--
-- Embedding dimension is fixed at table-creation time by pgvector. Default
-- here is 1024, matching Voyage AI's voyage-3.5 natively; embeddings.py asks
-- OpenAI's text-embedding-3-small for 1024 dims too (via Matryoshka
-- truncation), so both providers work against this column unmodified. If
-- you pick a model that can't be truncated to 1024, change `vector(1024)`
-- below *before* first init and re-embed everything — pgvector does not
-- up/down-cast between dimensions after the fact.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------
-- Projects & groups
-- ---------------------------------------------------------------------

CREATE TABLE project_groups (
    id         BIGSERIAL PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id         BIGSERIAL PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    group_id   BIGINT REFERENCES project_groups(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_projects_group ON projects(group_id);

-- ---------------------------------------------------------------------
-- Nodes: atomic memory units (replaces memory-bank/*.md sections)
-- ---------------------------------------------------------------------

CREATE TYPE node_kind AS ENUM (
    'brief', 'product', 'pattern', 'tech',
    'active', 'progress', 'devenv', 'task', 'plan', 'decision', 'protocol'
);

CREATE TYPE node_status AS ENUM ('active', 'archived', 'inbox');

CREATE TABLE nodes (
    id                     BIGSERIAL PRIMARY KEY,
    -- project this node belongs to (the write's target project)
    project_id             BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- project whose session actually created the node, if different from
    -- project_id (NULL when filed from within the same project — the
    -- common case). Non-null is what marks a node as a cross-project write.
    filed_from_project_id  BIGINT REFERENCES projects(id) ON DELETE SET NULL,

    kind       node_kind   NOT NULL,
    title      TEXT        NOT NULL,
    body       TEXT        NOT NULL DEFAULT '',
    topic      TEXT[]      NOT NULL DEFAULT '{}',
    status     node_status NOT NULL DEFAULT 'active',

    -- task-only fields
    priority   SMALLINT CHECK (priority BETWEEN 1 AND 9),
    importance SMALLINT CHECK (importance BETWEEN 1 AND 5),
    depends_note TEXT,  -- one-line free-text pointer, e.g. "blocked by #57"
    -- Human-facing task number, stable per project (never reused, never
    -- shifted by archiving) — NOT the same as `id`, which is a single
    -- sequence shared by every project in the DB and jumps unpredictably
    -- whenever *other* projects insert nodes. Only set for kind='task'.
    -- Assigned atomically via project_task_counters (see below).
    task_seq   BIGINT,

    embedding  vector(1024),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_nodes_project_kind_status ON nodes(project_id, kind, status);
CREATE INDEX idx_nodes_filed_from ON nodes(filed_from_project_id) WHERE filed_from_project_id IS NOT NULL;
CREATE INDEX idx_nodes_topic ON nodes USING GIN(topic);
CREATE INDEX idx_nodes_embedding ON nodes USING hnsw (embedding vector_cosine_ops);
CREATE UNIQUE INDEX idx_nodes_project_task_seq ON nodes(project_id, task_seq) WHERE task_seq IS NOT NULL;

-- Per-project counter for task_seq. One row per project; next_seq only
-- ever increments, so a task's number is permanent even after the task
-- itself is archived.
CREATE TABLE project_task_counters (
    project_id BIGINT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    next_seq   BIGINT NOT NULL DEFAULT 1
);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_nodes_updated_at
    BEFORE UPDATE ON nodes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------
-- Edges: typed graph relations (may cross project boundaries)
-- ---------------------------------------------------------------------

CREATE TYPE edge_rel AS ENUM (
    'depends_on', 'blocks', 'relates_to', 'supersedes',
    'part_of', 'refines', 'cross_ref'
);

CREATE TABLE edges (
    id         BIGSERIAL PRIMARY KEY,
    src_id     BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst_id     BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    rel        edge_rel NOT NULL,
    weight     REAL NOT NULL DEFAULT 1.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (src_id, dst_id, rel)
);

CREATE INDEX idx_edges_src ON edges(src_id);
CREATE INDEX idx_edges_dst ON edges(dst_id);

-- ---------------------------------------------------------------------
-- Messages: inter-agent conversational channel (separate from the
-- inbox-task mechanism above). No embedding, no graph edges — see
-- migrations/002_add_messages.sql for the full rationale.
-- ---------------------------------------------------------------------

CREATE TABLE messages (
    id              BIGSERIAL PRIMARY KEY,

    to_project_id   BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_project_id BIGINT REFERENCES projects(id) ON DELETE SET NULL,
    from_session    TEXT NOT NULL DEFAULT '',

    -- == root message's id. Deliberately NOT a FK: replies live in the
    -- other project's mailbox, so ON DELETE CASCADE from the root would
    -- delete half a conversation when one participant project is dropped.
    -- Assigned by trg_messages_set_thread, never by the client.
    thread_id       BIGINT NOT NULL,
    in_reply_to     BIGINT REFERENCES messages(id) ON DELETE SET NULL,
    -- 0 at the thread root, parent+1 per reply. Trigger-derived, so a
    -- client cannot reset it to dodge the ping-pong cap.
    reply_depth     INTEGER NOT NULL DEFAULT 0 CHECK (reply_depth BETWEEN 0 AND 32),

    kind            TEXT NOT NULL DEFAULT 'ask'
                        CHECK (kind IN ('ask', 'reply', 'fyi')),
    subject         TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'unread'
                        CHECK (status IN ('unread', 'read', 'answered')),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at         TIMESTAMPTZ,

    -- Set by memory_mcp.notifier once it fires a claude.ai routine's API
    -- trigger for this message (offline delivery when no session's
    -- listener is around). NULL = not yet fired, or reset after a failed
    -- fire attempt so the next catch-up sweep retries. See
    -- server/memory_mcp/notifier.py and migrations/004_add_routine_fired_at.sql.
    routine_fired_at TIMESTAMPTZ,

    -- NOTE: "kind='reply' implies in_reply_to IS NOT NULL" is enforced in
    -- messages_set_thread() below, an INSERT-time trigger, deliberately NOT
    -- a table CHECK — in_reply_to is ON DELETE SET NULL, so a table CHECK
    -- would also fire when an unrelated project's deletion cascades and
    -- nulls out a *surviving* reply's in_reply_to.
    CONSTRAINT messages_body_not_blank
        CHECK (btrim(body) <> ''),
    CONSTRAINT messages_no_self_send
        CHECK (from_project_id IS NULL OR from_project_id <> to_project_id)
);

CREATE INDEX idx_messages_unread
    ON messages (to_project_id, created_at DESC) WHERE status = 'unread';
CREATE INDEX idx_messages_to_created ON messages (to_project_id, created_at DESC);
CREATE INDEX idx_messages_from_created ON messages (from_project_id, created_at DESC);
CREATE INDEX idx_messages_thread ON messages (thread_id, id);
CREATE INDEX idx_messages_in_reply_to
    ON messages (in_reply_to) WHERE in_reply_to IS NOT NULL;

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
        -- A reply must run between the same two projects already in this
        -- conversation, in either direction: the other side replying back
        -- (from/to flip relative to the parent), or a project continuing
        -- its OWN prior message in the thread (from/to unchanged from the
        -- parent). An earlier version of this check only allowed the first
        -- case -- forcing every reply's to_project back to the parent's
        -- sender unconditionally -- which broke the second case: it can't
        -- tell "the other side is replying" from "I'm adding on to what I
        -- just said", and flipped direction wrongly on the latter.
        IF parent.from_project_id IS NOT NULL THEN
            IF NOT (
                (NEW.from_project_id = parent.to_project_id
                 AND NEW.to_project_id = parent.from_project_id)
                OR
                (NEW.from_project_id = parent.from_project_id
                 AND NEW.to_project_id = parent.to_project_id)
            ) THEN
                RAISE EXCEPTION
                    'reply to message % must be between projects % and % (its participants), got from=% to=%',
                    parent.id, parent.from_project_id, parent.to_project_id,
                    NEW.from_project_id, NEW.to_project_id;
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_messages_set_thread
    BEFORE INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION messages_set_thread();

-- NOTIFY channel is keyed by project id, not slug: LISTEN identifiers
-- truncate at NAMEDATALEN-1 = 63 bytes, risking silent cross-project
-- channel collisions on long slugs. Payload carries truncated,
-- whitespace-collapsed teasers only, never the full body (NOTIFY payloads
-- are hard-capped at 8000 bytes).
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

CREATE TRIGGER trg_messages_notify
    AFTER INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION messages_notify();

-- ---------------------------------------------------------------------
-- Scan verdicts: durable "reviewed and rejected/accepted" marks
-- ---------------------------------------------------------------------

CREATE TABLE scan_verdicts (
    id         BIGSERIAL PRIMARY KEY,
    query_hash TEXT NOT NULL,
    node_id    BIGINT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    verdict    TEXT NOT NULL CHECK (verdict IN ('irrelevant', 'relevant')),
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_scan_verdicts_lookup ON scan_verdicts(query_hash, node_id);

-- ---------------------------------------------------------------------
-- Action protocols: deterministic "how do we do X" rules, looked up by
-- protocol_hook.py (a PreToolUse hook) BEFORE a tool call, keyed on the
-- ACTION being taken (e.g. "psql as role X", "read this file") rather than
-- the task's topic -- the thing memory_search's semantic matching can't
-- reach, since a rule like this rarely reads as similar to the task that
-- triggers it. See protocols.py and protocol_hook.py.
--
-- One protocol_match row per kind='protocol' node: the node's title/body
-- hold the human-readable rule text (what memory_search/memory_get return),
-- this table holds the structured fields the hook matches on. Exact-field
-- matching (tools/action_class/path_glob/db_role) is tried first, in
-- protocols.py, before falling back to vector search over kind='protocol'
-- nodes -- so most lookups never touch the embedding provider.
-- ---------------------------------------------------------------------

-- Reserved project owning global (all-project) protocols. Group-scoped
-- protocols instead live on any real project in a project_group and are
-- looked up via that group's id.
INSERT INTO projects (slug, name)
VALUES ('_global', 'Global (cross-project action protocols)')
ON CONFLICT (slug) DO NOTHING;

CREATE TABLE protocol_match (
    -- 1:1 with the kind='protocol' node carrying the rule text.
    node_id      BIGINT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    scope        TEXT NOT NULL CHECK (scope IN ('project', 'group', 'global')),
    group_id     BIGINT REFERENCES project_groups(id) ON DELETE CASCADE,
    -- NULL = applies to any tool. Otherwise one or more of Bash/Read/Edit/Write.
    tools        TEXT[],
    -- e.g. 'db.query', 'db.ddl', 'db.grant', 'api.call', 'git.write',
    -- 'file.read', 'file.edit', 'shell.readonly'. A trailing '.*' segment
    -- (e.g. 'db.*') matches any action_class sharing that prefix -- see
    -- protocols.py's exact-match query.
    action_class TEXT NOT NULL,
    -- fnmatch-style glob against the action's path, matched in Python
    -- (protocols.py), not in SQL.
    path_glob    TEXT,
    db_role      TEXT,
    -- 'inform': inject the rule as context, tool call proceeds normally.
    -- 'ask': same, but also force a permission prompt even in auto mode.
    -- 'deny': block the call, rule text becomes the denial reason.
    -- 'none': explicitly "no special rule for this" -- lets protocols.py
    -- treat this action_class/scope as already reviewed, so the hook
    -- doesn't keep asking the user about it.
    effect       TEXT NOT NULL CHECK (effect IN ('inform', 'ask', 'deny', 'none'))
);

CREATE INDEX idx_protocol_match_action ON protocol_match(action_class);
CREATE INDEX idx_protocol_match_scope ON protocol_match(scope, group_id);

-- Caches the outcome of the (only-when-exact-match-misses) vector search
-- step, keyed by a normalized action signature, so repeating the same kind
-- of action doesn't re-embed it every time. node_id NULL means "vector
-- search ran and found nothing" (also cached, to skip a wasted call next
-- time). Fully invalidated (DELETE ... WHERE project_id = ...) whenever a
-- protocol_add/archive touches that project's protocols -- see protocols.py.
CREATE TABLE protocol_vector_cache (
    project_id     BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    signature_hash TEXT NOT NULL,
    node_id        BIGINT REFERENCES nodes(id) ON DELETE CASCADE,
    at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, signature_hash)
);
