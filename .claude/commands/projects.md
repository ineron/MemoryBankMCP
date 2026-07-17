---
description: List all projects connected to the memory bank with a stats overview
---
# Projects Overview

## Process

Call `project_overview()` (no args for all projects, or `group="<slug>"` to
scope to one product family). This is a single aggregate query — no need
to call `memory_search`/`memory_get` or loop per-project.

## Output

Present exactly in this format, one row per project, sorted by slug:

---
**Connected projects:**

| Project | Group | Nodes | Tasks (active/inbox) | Active context last updated |
|---------|-------|-------|----------------------|------------------------------|
| ledgyx-admin-ui | ledgyx | 640 | 16 / 0 | 2026-07-16 |
| ledgyx-landing  | ledgyx | 51  | 2 / 0  | 2026-07-16 |
| pg_ilib         | —      | 37  | 0 / 0  | 2026-07-09 |

**Node breakdown by kind** (only if the user asks for detail on a specific
project — don't dump this for every project by default, it's noisy):

| Kind | Count |
|------|-------|
| brief | ... |
| product | ... |
| ... | ... |

---

## Guidelines

- If `tasks_inbox` is non-zero for any project, call it out explicitly —
  that means something was filed cross-project and hasn't been triaged yet
  (the target project's own `/start` would show it under "📥 Filed from
  other sessions", but this overview is a good place to notice it exists
  across *all* projects at once).
- If `last_active_updated` is missing or looks stale (weeks old) for a
  project, note it — likely means that project hasn't been touched through
  the memory bank in a while, not necessarily a problem, just worth flagging.
- Format `last_active_updated` as a plain date (not a full timestamp) unless
  the user is debugging same-day activity.
