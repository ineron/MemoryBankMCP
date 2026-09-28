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

If `server/memory_mcp/notifier.py` is deployed and configured (see
`server/deploy/mb-notifier.service`, `server/.env`'s `ROUTINE_*` vars), the
Monitor re-arm burden below is no longer the *only* path to getting
notified: that daemon runs independently of any session and fires a
claude.ai routine whenever a message stays unread with no listener around,
so a closed terminal no longer means zero delivery until the next manual
`/start`. It's a pointer only, not a substitute for steps 1–6 in
`.claude/reference/message-handling.md` — a session still has to actually
open and handle the message. Optionally check whether it's running with
`pgrep -f "memory_mcp\.notifier$"` (reported only — never re-armed by this
command; that daemon is not session-scoped) and mention its state in the
checklist line in §7.

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
1800000ms with no way around it):

1. `pgrep -f "memory_mcp\.notifier$"` — is the offline notifier daemon
   deployed on this machine (see `server/README.md`'s "Offline notifier"
   section)?
   - **Pid found** → **do not re-arm.** Let this Monitor window lapse and
     stop there for the rest of the session. `mb-notifier` now covers
     delivery for however long the session stays open past this point: it
     fires a claude.ai routine (~`ROUTINE_GRACE_SECONDS`, default 90s,
     after a message arrives) whenever no listener is currently armed for
     this project — which, once this window lapses, is exactly this
     session's own state. The routine only *notifies*; a real session
     still has to open and handle the message per
     `.claude/reference/message-handling.md`, so nothing is silently
     dropped, it just stops being instant-in-this-chat past the first
     ~30 minutes. One short status line noting the handoff (or nothing
     user-visible, if your harness permits a turn with no reply) is the
     entire budget for this event — do not keep re-checking `pgrep` for
     the notifier on every future expiry either, since there won't be any:
     nothing re-arms this Monitor again this session.
   - **No pid** → `mb-notifier` isn't deployed here yet. Falling dark for
     the rest of the session would mean zero delivery of any kind until
     the next `/start`, which is worse than the cost of re-arming — so
     **do re-arm it, every time it expires, for as long as the session
     runs**, same as before. (This whole branch existed, unconditionally,
     before `mb-notifier` shipped — see `notifier.py`'s own module
     docstring and `server/README.md` for what it does and how to deploy
     it. If it's missing on a machine you maintain, deploying it once
     ends the mechanical-re-arm cost for every session on that machine,
     not just this one.)

**When re-arming (the "no pid" branch above), make it mechanical, not
conversational.** Handling this event is a health check, nothing more:

1. `pgrep -f "memory_mcp\.listener --project <slug>$"` (same anchored
   pattern as step 4's initial check).
2. Pid found → the listener is already back (another path re-armed it,
   or this notification is stale). Do nothing.
3. No pid → re-issue *exactly* the same `Monitor(...)` call used to arm
   it originally (same bounded `timeout_ms`, since `persistent` isn't
   available here).

Do not call `message_inbox`, `message_thread`, or any other retrieval
tool as part of handling this event — that is a completely different
event type (an actual 💬 message notification) with its own procedure in
`.claude/reference/message-handling.md`; a Monitor expiring on its own
carries no information about whether anything is unread. Do not write a
narrative reply either — no "still waiting," no summary of what's
outstanding, no re-deriving conversation state. One short status line
confirming the re-arm (or nothing user-visible at all, if your harness
permits a turn with no reply) is the entire budget for this event. A
fresh session (new `/start`) still gets its own new bounded window
regardless of any of this — expected, not a bug.

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
