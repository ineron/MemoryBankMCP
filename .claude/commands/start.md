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

### 5. Handling a 💬 message notification

A live listener notification looks like:
`💬 msg#412 ❓ ask from ledgyx-core/... [thread 412 depth 0 ] ... — preview text`.
It is an event, not a user turn — never interrupt an in-flight tool call or
edit to react to one; handle it once the current step finishes. Then:

1. `message_thread(N)` — always read the whole thread, never just the
   notification preview (it's truncated at 200 characters).
2. `message_mark(N, "read")` before acting. If it returns `claimed: False`,
   another session already took it — stop, do nothing further.
3. First decide: is this session **idle** (nothing else in flight this
   turn) or **mid-task** (already partway through implementing something
   else this session)? "Mid-task" means actual work underway, not "the
   user hasn't typed in a while" — if unsure, treat it as idle.
4. By `kind`:
   - **`fyi`/`ask` naming actual work (a fix, a change, an
     implementation), session idle** → pick it up now, in this session,
     the same way it would pick up a task its own user handed it —
     through the normal Claude Code permission prompts, with the normal
     judgment about risky/destructive/hard-to-reverse steps. Don't just
     acknowledge and file it for later when nothing is stopping you from
     starting. Reply when done (or when you hit something that needs this
     session's user) summarizing what happened.
   - **`fyi`/`ask` naming actual work, session mid-task** → don't context
     switch away from what's already underway. File it the normal
     cross-project way (`memory_upsert(project=<this>, kind="task",
     filed_from_project=<sender>)`) so it survives, then reply that it's
     queued and, briefly, what this session is currently doing instead.
   - **`ask` that's purely informational, `replies_left > 0`** → answer
     autonomously regardless of idle/mid-task: pull the answer from this
     repo and, if real retrieval is needed, dispatch `memory-scan`. Then
     `message_send(in_reply_to=N, body=...)` with no routing arguments.
   - **`ask`, `replies_left == 0`** → do **not** reply. Surface it instead:
     "thread T hit the reply-depth cap; it needs you."
5. **The boundary is idle-vs-mid-task, not read-vs-write.** An idle
   session may edit files and commit because of a cross-project request,
   same as it would for its own user — Claude Code's permission mode is
   the actual gate on that, not an extra memory-bank rule on top of it.
   What stays off-limits regardless of idle/mid-task: dropping in-flight
   work to go handle someone else's request, and anything that pushes,
   deploys, or force-touches shared state as a side effect of an incoming
   message.
6. Every reply must be self-contained (full paths, slugs, task numbers) —
   the receiving agent shares none of this session's context.

### 6. Cross-project requests: send, don't do it yourself

If at any point this session — not just while handling an incoming 💬
message — decides something needs doing or checking in a *different*
project (the root cause is actually upstream, a question only that
project's session can answer, a change belongs in its code), do **not**
switch into that project's repo and do it yourself, even if you happen to
have filesystem access to it. This isn't the same rule as step 5's
idle-session autonomy — that's about the *target* project's own session
choosing to act on a request addressed to it. Here there is no session for
the target project in this context, only this session reaching outside its
own project on its own initiative, which is exactly what the channel
exists to prevent. Send the request through and let that project's own
session — idle or not — decide, the same way this session gets to decide
for itself.

1. Check `project_list()` for the target project's slug.
2. **Found** — send it through the channel instead of acting on it
   yourself:
   - a question, notice, or anything conversational → `message_send(
     to_project=<slug>, from_project=<this project's slug>, kind="ask"
     or "fyi", ...)`.
   - an actual work item for them to do → `memory_upsert(project=<slug>,
     kind="task", filed_from_project=<this project's slug>, ...)` — lands
     in their 📥 inbox.
3. **Not found** (unlikely — means the project was never registered in
   this memory bank). Do not guess, proceed anyway, or silently skip it.
   Tell the user directly and let them pick:
   - do it yourself right now, in this session, or
   - wait — leave it, and try again once the project is registered, or
   - skip it entirely.

### 7. Session compliance checklist

For the rest of this session (every reply, not just this one), end your
message with a one-line checklist confirming the two easy-to-forget rules
from this file are still active, right above the heartbeat marker from
`~/.claude/CLAUDE.md`:

`🔒 no cross-project edits (§6) | 📡 listener: <armed pid <pid> | already running pid <pid> | not armed>`

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
