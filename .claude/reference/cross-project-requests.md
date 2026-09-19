# Cross-project requests: send, don't do it yourself — detail for /start §6

Read this only at the moment this session actually decides something needs
doing or checking in a *different* project — not pre-emptively at session
start. This applies whenever that moment arises, not only while handling an
incoming 💬 message (that's `message-handling.md`'s idle-session-autonomy
rule, a different thing: this file is about the *target* project having no
session in this context at all, only this session reaching outside its own
project on its own initiative).

If the root cause is actually upstream, a question only that project's
session can answer, or a change belongs in its code: do **not** switch into
that project's repo and do it yourself, even if you happen to have
filesystem access to it. Send the request through and let that project's
own session — idle or not — decide, the same way this session gets to
decide for itself.

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

If you ever catch yourself about to edit another project's files directly
instead of sending a message per this file, stop and reread it rather than
proceeding — that's the `/start` §7 checklist failing to do its job.
