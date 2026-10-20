-- Adds protocol_audit: a durable, append-only log of every decision
-- protocol_hook.py made (deny/ask/inform/none/unknown), inspired by
-- cc-monitor's SQLite audit log but kept in the same Postgres DB as
-- everything else. See schema.sql's "Action protocols" section for the
-- field-by-field rationale -- this file only carries the DDL.

CREATE TABLE protocol_audit (
    id           BIGSERIAL PRIMARY KEY,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    project_id   BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id   TEXT,
    tool_name    TEXT,
    action_class TEXT NOT NULL,
    signature    TEXT,
    decision     TEXT NOT NULL CHECK (decision IN ('inform', 'ask', 'deny', 'none', 'unknown')),
    node_id      BIGINT REFERENCES nodes(id) ON DELETE SET NULL
);

CREATE INDEX idx_protocol_audit_project_at ON protocol_audit(project_id, at DESC);
CREATE INDEX idx_protocol_audit_decision ON protocol_audit(decision) WHERE decision IN ('deny', 'ask', 'unknown');
