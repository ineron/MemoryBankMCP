---
description: Save current session state to the memory bank MCP server
---
# Save Session State

Update the memory bank to preserve current session state. Resolve the
project slug from `.claude/settings.json` first; all calls below use it.

1. **Active context** — call `memory_active(project)` to get the existing
   current-focus node (if any), then `memory_upsert(project, kind="active",
   title="Current focus", body=<focus/blockers/next-step>, id=<existing id
   or omit>)`. This updates the *same* node in place rather than piling up a
   new one each session — there should only ever be one live `active` node
   per project.
2. **Progress log** — `memory_upsert(project, kind="progress", title="Progress
   <YYYY-MM-DD>", body=<what was completed today>)`. Each session gets its
   own progress node; unlike a growing `progress.md`, these never need
   trimming or archiving — they simply aren't read at session start
   (`/start` only reads `memory_active` + `memory_tasks`), and surface later
   only if `memory_search` finds them relevant to something.

Be brief and factual. Focus on what the next session needs to know to
continue immediately.

## Task Upkeep

Task nodes (`kind="task"`) are what `/start`'s `memory_tasks` call reads —
keep them current every time this command runs, not just during
`/workflow:update-memory`:

- **Task closed this session** — `memory_archive(node_id, status="archived")`.
  Never delete it: archived tasks are excluded from `/start` and default
  `memory_search`, but stay fully retrievable (`memory_get`, or
  `memory_search(include_archived=True)`) — nothing gets silently lost the
  way a trimmed markdown row used to.
- **New task surfaced** — `memory_upsert(project, kind="task", title=...,
  priority=<1-9>, importance=<1-5>, topic=[...], depends_note=<optional
  one-line pointer>)`. Grade it right away: Priority 🔴9-7 (act soon) / 🟡6-4
  (medium-term) / 🟢3-1 (backlog); Importance ⭐ (cosmetic) through ⭐⭐⭐⭐⭐ (real
  risk to data/money/security). Don't leave it ungraded for a later pass.
  The returned `task_seq` is its permanent, per-project task number —
  Postgres never reuses it, so there's nothing to track manually the way a
  hand-maintained `#` column required. **Show `task_seq` to the user, not
  `id`** — `id` is a single sequence shared by every project in the DB and
  jumps unpredictably whenever other projects insert nodes.
- **New dependency found** (blocks, blocked by, bundle-with, soft
  prerequisite) — set a short pointer in `depends_note` (e.g. `blocked by
  #57`) AND create the real relationship with `memory_link(src_id, dst_id,
  rel="depends_on"|"blocks"|"relates_to")`. The edge *is* the durable
  record of the dependency and its direction — there is no separate
  `taskDependencies.md` to maintain; `/workflow:understand` (or anyone) can
  pull the full dependency graph for a task via `memory_get(node_id,
  hops=1)` when it's actually picked up.

If none of the above changed this session, don't touch task nodes just to
touch them.
