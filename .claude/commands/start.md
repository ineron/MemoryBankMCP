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

### 4. Live delivery readiness (no arming by default)

**`SendMessage`/`ListAgents` (the harness's own cross-session messaging) is
the default live-delivery mechanism — nothing to arm, no expiry, no
re-arming.** This flipped 2026-10-27 from an earlier design where
`listener.py`/`Monitor` was armed by default: that approach is pure
Postgres `LISTEN`/`NOTIFY`, which was the only thing that could cross an
OS-account/machine boundary (confirmed 2026-10-20 — `SendMessage` is
hard-restricted to the *same* OS user by design, no override). Once every
project here moved to running under the same OS user, that advantage
stopped mattering for this machine's day-to-day case, and `Monitor`'s real
cost — capping at ~30 min and needing mechanical re-arming in any
environment without `persistent` support — stopped being worth paying by
default. (See `.claude/reference/live-push.md` for the full history and
the exact send-side mechanics.)

**This is not a claim that `SendMessage` delivers to a closed session** —
it doesn't, and neither did `listener.py`/`Monitor` (its live process also
dies the moment the session/terminal closes — see `server/README.md`'s
"Message listener" section). Delivery to an offline recipient was always
`mb-notifier`'s job (a true independent daemon, unaffected by any of this)
plus the next `/start`'s `message_inbox` poll, not either live mechanism.

The only thing worth checking at `/start` time is whether *this* session
is reachable by `SendMessage` at all — a peer's `SendMessage(to=<name>)`
only finds this session if its address name normalizes (lowercase, strip
everything but letters/digits) to the same string as this project's slug:

1. `ListAgents()` — its result states this session's own name (e.g. "This
   session is MemoryBankMCP").
2. Normalize both that name and `<slug>` the same way and compare.
3. **Match** → nothing to say; live pushes to this project will resolve
   whenever a sender's own session is also open at send time.
4. **Mismatch** → this session was started without `-n <slug>` (or with a
   different name). Pushes to this project's slug won't find it until a
   session is started that way — say so once, briefly, in the output below
   (not a blocker: `message_inbox` polling at the next `/start` and
   `mb-notifier`'s routine fallback still work regardless).

**`listener.py`/`Monitor` is still available, just not armed by default
any more** — arm it manually (same `Monitor({command: "<python> -u -m
memory_mcp.listener --project <slug>", description: "...", persistent:
true})` call as before, pgrep-guarded the same way — see
`server/README.md`'s "Message listener" section for the exact command)
specifically when you know this project's session, or a frequent
correspondent's, runs under a *different* OS account or a *different
machine* sharing this same Postgres DB — that's the one case `SendMessage`
structurally cannot reach, no matter what. If you do arm it manually,
follow the same mechanical re-arm-or-let-mb-notifier-cover-it guidance this
file used to spell out in full here — now condensed into `server/README.md`
since it's no longer the default path, only an opt-in one.

### 5. Handling a 💬 message notification

A live push arrives wrapped as `<cross-session-message from="...">`, its
first line self-contained (id, kind, sender, subject — see
`.claude/reference/live-push.md`'s format). If you opted into arming
`listener.py`/`Monitor` manually (step 4), a notification from that path
instead looks like `💬 msg#412 ❓ ask from ledgyx-core/... [thread 412
depth 0] ... — preview text` — treat either shape the same way below. It
is an event, not a user turn — never interrupt an in-flight tool call or
edit to react to one; handle it once the current step finishes.

Only if `message_inbox` returned something just now, or a notification
arrives later this session (either shape above): read
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

`🔵 no cross-project edits (§6) | 🟣 live delivery: <name matches slug | name mismatch — start with -n <slug>>`

Source the live-delivery state from what step 4 actually found — don't
guess. If you manually armed `listener.py`/`Monitor` per step 4's opt-in
branch, append ` | listener: armed pid <pid>` to the same line. If you ever
catch yourself about to edit another project's files directly instead of
sending a message per §6, that's the checklist failing to do its job —
stop and reread §6 rather than proceeding.

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
