# Handling a 💬 message notification — detail for /start

Read this only when there's an actual message to handle: `message_inbox`
returned something during `/start`, or a live notification arrives later
in the session, in either of two shapes (see `.claude/commands/start.md`
§4 and `.claude/reference/live-push.md` for which path produced it):

- **The armed `listener.py`/`Monitor` path** (works regardless of OS
  account):
  `💬 msg#412 ❓ ask from ledgyx-core/... [thread 412 depth 0 ] ... — preview text`.
- **The `SendMessage` bonus fast path** (same-OS-account sender only), a
  `<cross-session-message from="...">`-wrapped line with the same kind of
  content: `💬 new message #412 (ask) from ledgyx-core — memory_tasks inbox
  split. Call message_thread(412) to read it.`

Don't read this file pre-emptively when there's nothing to act on. Either
shape above is an event, not a user turn — never interrupt an in-flight
tool call or edit to react to one; handle it once the current step
finishes. Then:

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
     `message_send(in_reply_to=N, from_project=<this project's slug>,
     body=...)` — `from_project` is required on a reply; omit `to_project`.
     Attempt a live push on the reply too — see
     `.claude/reference/live-push.md`.
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

**A claude.ai routine alert (`📬 Memory-bank inbox: <project>`) is only a
pointer, not a handled message.** `memory_mcp.notifier` fires that routine
when nothing was around to deliver live — the routine itself has no access
to this system and cannot read, claim, or reply. Open a real session in the
named project and follow this file's steps 1–6 as normal; the routine run
did none of them.
