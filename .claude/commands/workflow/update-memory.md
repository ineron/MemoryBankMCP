---
description: Update the memory bank MCP server with significant learnings
---
# Memory Bank Documentation Update

## Task
Review nodes relevant to this session's work and write back significant
learnings, decisions, and environment discoveries. Resolve the project slug
from `.claude/settings.json` first; all calls below use it.

Everything is a Postgres node (kinds: `brief`, `product`, `pattern`, `tech`,
`active`, `progress`, `devenv`, `task`, `plan`, `decision`) reached via
`memory_search`, `memory_get`, `memory_upsert`, `memory_link`,
`memory_archive` — there are no memory-bank files.

## Process

### 1. Targeted review
Don't re-read everything — that's exactly what the lazy-loading design
eliminated. Use `memory_search(project, query=<summary of this session's
work>, hops=1, limit=15)` plus `memory_active(project)`. Only broaden to
per-kind sweeps (`memory_search(project, query="", kinds=["pattern"], ...)`)
if the targeted search misses something you know changed.

### 2. Update when
A new pattern/decision was established, a feature completed, a discovery
made, direction changed, a problem resolved, or an environment fact learned.

### 3. Per-kind guidance

| Kind | Update when |
|---|---|
| `brief` | Core requirements, scope, or goals changed |
| `product` | UX insight, problem definition, solution approach, or value prop changed |
| `active` | Always, if focus/blockers/next-step changed — **single live node**: fetch via `memory_active` first and pass its `id` to `memory_upsert` so you update in place, never duplicate |
| `progress` | One new node per session (`title="Progress <YYYY-MM-DD>"`), never appended to an existing one |
| `pattern` | New architecture/design pattern, security/perf approach adopted |
| `tech` | New technology, config, or dev-workflow change |
| `devenv` | New connection/auth/one-liner/gotcha — one node per fact, `topic`-tagged (see `/scan-env` for the category breakdown) |

### 4. Tasks
Same rules as `/save`'s Task Upkeep: `memory_archive(node_id,
status="archived")` for closed tasks (never delete — stays retrievable via
`memory_get` / `memory_search(include_archived=True)`); `memory_upsert(kind
="task", priority=1-9, importance=1-5, topic=[...])` for new ones, graded
immediately. New dependency found → set `depends_note` to a short pointer
(e.g. `blocked by #57`) **and** create the real edge — `memory_link(src_id,
dst_id, rel="depends_on"|"blocks"|"relates_to")` — there is no separate
dependencies file; `memory_get(node_id, hops=1)` pulls the full picture when
a task is picked up.

### 5. Quality
Factual and consistent across nodes (e.g. a `pattern` node shouldn't
contradict what `active` says is currently happening); `topic` tags
accurate — that's what `memory_search`/`memory-scan` filter on, so a wrong
or missing topic makes a node effectively unfindable later; date
`progress` titles.

## Output

**Nodes updated:** [kind / title / id]: brief summary of the change
**Key additions:** patterns/decisions/learnings worth flagging
**Gaps:** anything still missing, for a future session

## Next
`/workflow:understand` for more work, or end the session.
