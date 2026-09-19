---
description: Start session - load active context and prioritized tasks from the memory bank MCP server
---
# Session Start

## Process

### 1. Resolve the project
Read `.claude/settings.json` for `project.slug` (create the project first via
`project_create` if it isn't registered yet — check with `project_list`).
All calls below use this slug.

### 2. Context Loading
Call **only** three tools: `memory_active(project)`, `memory_tasks(project)`,
and `message_inbox(project)`. `message_inbox` is admitted to this short list
because it costs what `memory_tasks` costs — one partial-index lookup with a
bounded `limit` — and computes no embedding, so it doesn't touch the
knowledge graph any more than `memory_tasks` does.

Do not call `memory_search` or `memory_get` here — session start is not the
place to explore the knowledge graph. Those happen later, scoped to a
specific task, via the `memory-scan` subagent from `/workflow:understand`.
Calling them now would spend context budget on material unrelated to
whichever task gets picked.

If `memory_active` returns nothing, or `memory_tasks` returns an empty list,
say so explicitly — that's a sign `/save` or `/workflow:update-memory` fell
behind, not a reason to go call `memory_search` to reconstruct state
yourself.

### 3. Task Discovery
`memory_tasks` returns two lists — present both, clearly separated:
- **`tasks`** — this project's own backlog.
- **`inbox`** — tasks filed here from another project's session (see
  `filed_from_slug` on each row). These are unreviewed by definition; don't
  silently fold them into the main table.

Each task row carries:
- **`#` (`task_seq`)** — the stable, per-project task number to show the
  user and use in conversation ("let's do #12"). **Never show the raw
  `id`** — that's a single sequence shared by every project in the shared
  DB, so it jumps unpredictably (e.g. 219 → 1003) whenever *other* projects
  insert nodes; `task_seq` is scoped to this project alone and never
  reused, even after a task is archived. Keep the `id` from each row in
  mind for this session only, to resolve a "#N" the user mentions back to
  the real node id for `memory_get`/`memory_archive`/`memory_link` calls.
- **Priority** — 9-level urgency scale (🔴9-7 act soon / 🟡6-4 medium-term /
  🟢3-1 backlog)
- **Importance** — 5-star scale (⭐⭐⭐⭐⭐ real risk to data/money/security
  down to ⭐ cosmetic/edge case)
- **Topic** — which area it touches; used both by `/workflow:understand` to
  decide what to search for, and as a secondary signal of importance (e.g.
  `security`/`versioning` topics tend to carry more weight than
  `graph`/`agents` UX polish)
- **Depends/Related** (`depends_note`) — one-line pointer only (e.g.
  `blocked by #57`, `epic w/ #48`); full rationale for a dependency lives in
  the graph edge between the two task nodes (`depends_on`/`blocks`/
  `relates_to`), retrievable via `memory_get(hops=1)` when a task is
  actually picked up — do not fetch that here, it's not needed for the
  session-start summary.

Do not drop the Topic or Depends/Related columns when presenting the table —
report every column exactly as returned.

### 4. Arm live message delivery

Before arming anything, check whether a listener is already running for
this project — do not rely on remembering an earlier `/start` in this
conversation, since that memory doesn't survive `/clear`, context
compaction, or a second terminal running `/start` in the same project:

```
pgrep -f "memory_mcp\.listener --project <slug>$"
```

The trailing `$` is load-bearing, not cosmetic: an unanchored pattern also
matches the Bash-tool wrapper process that is, at that instant, running the
`pgrep` command itself — its command line is `bash -c '... eval
'"'"'pgrep -f "memory_mcp.listener --project <slug>"'"'"' ...'`, which
contains the search string as a literal substring. That wrapper is a real,
momentarily-running process, so it comes back as a false-positive pid
indistinguishable from a genuine listener, then exits immediately after —
producing exactly the "already running, pid `<pid>`" false claim with no
listener actually armed. The real listener's command line ends exactly at
`--project <slug>` with nothing after it, so anchoring with `$` excludes
the wrapper while still matching the real process.

- **A pid comes back** — a listener is already live for `<slug>` (from this
  session or another). Skip arming a new Monitor entirely and say so ("live
  message delivery already running, pid `<pid>`").
- **No pid** — proceed. Read `.mcp.json` at the project root and take
  `mcpServers["memory-bank"].command` — the absolute path to the shared
  server venv's python. Arm a persistent Monitor with:

```
Monitor({
  command:     "<that python> -u -m memory_mcp.listener --project <slug>",
  description: "inter-agent messages for <slug>",
  persistent:  true,
  timeout_ms:  3600000
})
```

Expect a `[mb-listener] ready on mb_msg_<id> for <slug> — N unread
message(s) replayed` line shortly after. If it instead prints `FATAL ...`,
report that line and continue anyway — the 💬 block below still works by
polling `message_inbox` on each future `/start`, it just won't notify live.

The `pgrep` check keeps the *process count* down (one listener per
project, not one per `/start`). It's a belt-and-suspenders layer on top of
the Postgres advisory lock already in `listener.py`, which guarantees
correctness even if two listeners somehow end up running at once — a
second one stands by and never double-delivers. `pgrep` avoids paying for
that second idle process in the first place.

**If the listener ever needs re-arming mid-session** (a `Monitor` finished
event reports the underlying process exited — e.g. it crashed after
repeated DB-connection resets — not just a routine reconnect log line):
re-issue *exactly* the same call as above, `persistent: true` (`timeout_ms`
is then irrelevant — ignored when persistent). If `persistent` is available
in your Monitor tool's schema, do not improvise a bounded `timeout_ms`
instead — that produces an indefinite ~30-minute die/re-arm loop that burns
a full turn (Bash + Monitor call + reply) every cycle purely to report
"still idle," even though nothing is actually wrong.

**But check first — `persistent` is not guaranteed to exist in every
environment's Monitor tool.** If your tool's own schema doesn't offer it
(only `command`/`description`/`timeout_ms`/`ws` accepted, and `timeout_ms`
caps below what a truly indefinite watch would need — e.g. capped at
1800000ms with no way around it), do **not** re-arm a bounded Monitor at
all when it expires — not even once. It is tempting to read "don't loop
*perpetually*" as "one re-arm is fine, just not an endless chain," but
that reading is wrong: the first bounded re-arm costs the same turn
(Bash + Monitor call + reply) as every subsequent one, and there is no
signal available at that point to tell you this is the *last* one you'll
need — so "re-arm once, then stop" degrades to the same repeating cycle
as "re-arm forever," just discovered one cycle later. Treat every expiry
of a bounded Monitor identically, starting with the first: let it lapse
and fall back to polling. `message_inbox` at the next `/start` already
covers correctness without live delivery (the same fallback already
described above for the FATAL-on-startup case) — accept degraded,
non-live delivery for the rest of *this* session rather than spending a
turn on it. A fresh session (new `/start`) gets its own new bounded
window regardless — that's expected, not a bug to chase.

### 5. Handling a 💬 message notification

A live listener notification looks like:
`💬 msg#412 ❓ ask from ledgyx-core/... [thread 412 depth 0 ] ... — preview text`.
It is an event, not a user turn — never interrupt an in-flight tool call or
edit to react to one; handle it once the current step finishes.

Only if `message_inbox` returned something just now, or a listener
notification arrives later this session: read
`.claude/reference/message-handling.md` for the full triage procedure
(idle-vs-mid-task, per-`kind` handling, reply-depth cap) before acting.
Don't read it pre-emptively when there's nothing to handle — most `/start`
runs have an empty inbox and never need it.

### 6. Cross-project requests: send, don't do it yourself

If at any point this session decides something needs doing or checking in
a *different* project — not just while handling an incoming 💬 message —
read `.claude/reference/cross-project-requests.md` for the exact procedure
before acting. Do not switch into that project's repo and do it yourself,
even if you happen to have filesystem access to it; this applies whenever
the moment arises during the session, not only at `/start` time, so don't
read the reference file until it actually does.

### 7. Session compliance checklist

For the rest of this session (every reply, not just this one), end your
message with a one-line checklist confirming the two easy-to-forget rules
from this file are still active, right above the heartbeat marker from
`~/.claude/CLAUDE.md`:

`🔵 no cross-project edits (§6) | 🔴 listener: <armed pid <pid> | already running pid <pid> | not armed>`

Source the listener state from what step 4 actually found — don't guess.
If you ever catch yourself about to edit another project's files directly
instead of sending a message per §6, that's the checklist failing to do its
job — stop and reread §6 rather than proceeding.

## Output

Present exactly in this format:

---
**Project:** [one line summary]
**Stack:** [key technologies]
**Architecture:** [one line — how it's built]

**Last completed:** [one line, from memory_active's body]
**Current blocker:** [one line or "none", from memory_active's body]

**Tasks:**

| # | Task | Priority | Importance | Topic | Depends/Related |
|---|------|----------|------------|-------|------------------|
| 1 | ...  | 🔴9 | ⭐⭐⭐⭐⭐ | auth | — |
| 2 | ...  | 🟡5 | ⭐⭐⭐ | billing | blocked by #4 |
| 3 | ...  | 🟢2 | ⭐⭐ | infra | w/ #7 |

**📥 Filed from other sessions:** *(omit this block entirely if `inbox` is empty)*

| # | Task | Priority | Importance | Topic | Filed from |
|---|------|----------|------------|-------|------------|
| 1 | ...  | 🟡5 | ⭐⭐⭐ | bug | ledgyx-landing |

**💬 Messages:** *(omit this block entirely if `message_inbox` returns nothing)*

| ID | From | Kind | Subject | Age | Thread |
|----|------|------|---------|-----|--------|
| 412 | ledgyx-core | ❓ ask | memory_tasks inbox split | 2h | 412 · 6 replies left |

📥 Filed = work another project wants done here. 💬 Messages = a
conversation another project started. Different tables, different tools,
on purpose.

**Recommended: start with task #[N]** — [one line why]

---
Priority: 🔴9/8/7 срочно (act soon) / 🟡6/5/4 скоро (medium-term) / 🟢3/2/1 когда-нибудь (backlog)
Importance: ⭐⭐⭐⭐⭐ реальный риск данным/деньгам/безопасности / ⭐⭐⭐⭐ ломает заявленную фичу / ⭐⭐⭐ подрывает доверие к системе / ⭐⭐ полезное улучшение / ⭐ косметика
