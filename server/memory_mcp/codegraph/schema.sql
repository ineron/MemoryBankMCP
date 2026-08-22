-- Codegraph plugin schema.
--
-- Lives in its own Postgres schema (not `public`) so the whole plugin can
-- be added or removed without touching the core memory-bank tables:
--   uninstall with `DROP SCHEMA codegraph CASCADE;`
--
-- Idempotent — safe to re-run (used both for fresh installs and to pick up
-- schema changes; there is no migration runner here any more than there is
-- for the core schema, but IF NOT EXISTS covers the common case of "add a
-- new project to an existing install").

CREATE SCHEMA IF NOT EXISTS codegraph;

-- One row per scanned source file. `file_hash` is the whole point of this
-- table: build_project_graph() diffs it against the file's current content
-- hash to decide whether to reparse — this is what actually makes the
-- incremental build incremental (a design the codebase this was borrowed
-- from computed but never wired up).
CREATE TABLE IF NOT EXISTS codegraph.files (
    id          BIGSERIAL PRIMARY KEY,
    project_id  BIGINT NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,       -- relative to the scanned project root
    language    TEXT NOT NULL,
    file_hash   TEXT NOT NULL,       -- sha256 of the file's raw bytes
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, path)
);

CREATE INDEX IF NOT EXISTS idx_codegraph_files_project ON codegraph.files(project_id);

-- module / class / function / method entries. A module entity represents
-- the file itself (parent_id NULL); everything else nests under it via
-- parent_id, mirroring the source's lexical nesting.
CREATE TABLE IF NOT EXISTS codegraph.entities (
    id          BIGSERIAL PRIMARY KEY,
    project_id  BIGINT NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    file_id     BIGINT NOT NULL REFERENCES codegraph.files(id) ON DELETE CASCADE,
    parent_id   BIGINT REFERENCES codegraph.entities(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('module', 'class', 'function', 'method')),
    name        TEXT NOT NULL,
    full_path   TEXT NOT NULL,       -- dotted, e.g. "pkg.module.ClassName.method"
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    is_public   BOOLEAN NOT NULL DEFAULT true,
    -- @decorator-wrapped (Flask/FastAPI routes, @pytest.fixture, etc.) —
    -- these are commonly invoked by a framework at runtime, never by a
    -- name lookup this graph can see, so codegraph_issues' unused_entities
    -- excludes them rather than reporting a large, low-signal false-positive
    -- share on any web-framework project.
    has_decorator BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (project_id, full_path)
);
-- Idempotent add for databases where this schema was applied before
-- has_decorator existed.
ALTER TABLE codegraph.entities ADD COLUMN IF NOT EXISTS has_decorator BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_codegraph_entities_project ON codegraph.entities(project_id);
CREATE INDEX IF NOT EXISTS idx_codegraph_entities_file ON codegraph.entities(file_id);
CREATE INDEX IF NOT EXISTS idx_codegraph_entities_parent ON codegraph.entities(parent_id);
CREATE INDEX IF NOT EXISTS idx_codegraph_entities_name ON codegraph.entities(project_id, name);

-- calls/imports between entities. target_entity_id is nullable ON PURPOSE:
-- call-site resolution here is best-effort name matching (same limitation
-- the borrowed-from project had), so a call that doesn't resolve to exactly
-- one candidate is still kept — with target_name preserved and
-- target_entity_id NULL — rather than silently dropped. codegraph_issues
-- surfaces the unresolved count so this stays visible instead of being a
-- silent gap.
--
-- target_entity_id uses ON DELETE SET NULL rather than CASCADE: if the
-- target file gets reparsed and that particular entity goes away, the
-- dependency row (and the still-valid source_entity_id / call site) should
-- survive as "now unresolved", not vanish along with the old target row.
-- source_entity_id uses CASCADE — a dependency without its source call site
-- is meaningless once that entity's own file is reparsed.
CREATE TABLE IF NOT EXISTS codegraph.dependencies (
    id                BIGSERIAL PRIMARY KEY,
    project_id        BIGINT NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    source_entity_id  BIGINT NOT NULL REFERENCES codegraph.entities(id) ON DELETE CASCADE,
    target_entity_id  BIGINT REFERENCES codegraph.entities(id) ON DELETE SET NULL,
    target_name       TEXT NOT NULL,   -- raw referenced name/path, always kept
    dependency_type   TEXT NOT NULL CHECK (dependency_type IN ('calls', 'imports')),
    line_number       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codegraph_deps_source ON codegraph.dependencies(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_codegraph_deps_target ON codegraph.dependencies(target_entity_id) WHERE target_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_codegraph_deps_project ON codegraph.dependencies(project_id);
