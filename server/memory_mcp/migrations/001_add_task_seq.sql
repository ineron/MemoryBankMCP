-- Adds a stable, per-project task display number, decoupled from `nodes.id`
-- (which is a single sequence shared across every project in the DB and
-- jumps unpredictably whenever *other* projects insert nodes). Safe to run
-- once against an existing database with real data — backfills task_seq
-- for all existing task nodes in creation order, then initializes each
-- project's counter so new tasks continue from the right number.

ALTER TABLE nodes ADD COLUMN IF NOT EXISTS task_seq BIGINT;

CREATE TABLE IF NOT EXISTS project_task_counters (
    project_id BIGINT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    next_seq   BIGINT NOT NULL DEFAULT 1
);

-- Backfill existing task nodes in creation order, per project.
WITH ranked AS (
    SELECT id, project_id,
           ROW_NUMBER() OVER (PARTITION BY project_id ORDER BY created_at, id) AS rn
    FROM nodes
    WHERE kind = 'task' AND task_seq IS NULL
)
UPDATE nodes n
SET task_seq = ranked.rn
FROM ranked
WHERE n.id = ranked.id;

CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_project_task_seq
    ON nodes(project_id, task_seq) WHERE task_seq IS NOT NULL;

-- Initialize (or fix up) each project's counter to continue after the
-- highest backfilled number.
INSERT INTO project_task_counters (project_id, next_seq)
SELECT p.id, COALESCE(MAX(n.task_seq), 0) + 1
FROM projects p
LEFT JOIN nodes n ON n.project_id = p.id AND n.kind = 'task'
GROUP BY p.id
ON CONFLICT (project_id) DO UPDATE SET next_seq = EXCLUDED.next_seq;
