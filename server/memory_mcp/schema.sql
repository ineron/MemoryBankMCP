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
    'active', 'progress', 'devenv', 'task', 'plan', 'decision'
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
