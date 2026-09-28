-- Adds the action-protocols layer: a new node_kind='protocol', the
-- reserved '_global' project for cross-project rules, and the
-- protocol_match / protocol_vector_cache tables protocols.py and
-- protocol_hook.py read. See schema.sql's "Action protocols" section for
-- the full field-by-field rationale -- this file only carries the DDL.

ALTER TYPE node_kind ADD VALUE IF NOT EXISTS 'protocol';

INSERT INTO projects (slug, name)
VALUES ('_global', 'Global (cross-project action protocols)')
ON CONFLICT (slug) DO NOTHING;

CREATE TABLE protocol_match (
    node_id      BIGINT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    scope        TEXT NOT NULL CHECK (scope IN ('project', 'group', 'global')),
    group_id     BIGINT REFERENCES project_groups(id) ON DELETE CASCADE,
    tools        TEXT[],
    action_class TEXT NOT NULL,
    path_glob    TEXT,
    db_role      TEXT,
    effect       TEXT NOT NULL CHECK (effect IN ('inform', 'ask', 'deny', 'none'))
);

CREATE INDEX idx_protocol_match_action ON protocol_match(action_class);
CREATE INDEX idx_protocol_match_scope ON protocol_match(scope, group_id);

CREATE TABLE protocol_vector_cache (
    project_id     BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    signature_hash TEXT NOT NULL,
    node_id        BIGINT REFERENCES nodes(id) ON DELETE CASCADE,
    at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, signature_hash)
);
