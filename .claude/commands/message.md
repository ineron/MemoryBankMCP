---
description: Message another project's Claude session, or read this project's message inbox
---
# Inter-agent Message

A conversational channel to another project's session — separate from
`memory_upsert(..., filed_from_project=...)`, which is still the right tool
for handing another project *work* (it lands in their 📥 inbox and survives
being ignored for weeks). Use `/message` for "I need to know X" or "heads up,
Y changed" — a live exchange, not a backlog item.

## Modes

**`/message`** (no arguments) — show this project's inbox and what it's
still waiting on:
1. Resolve `project.slug` from `.claude/settings.json`.
2. `message_inbox(project, include_sent=True)`.
3. Render the 💬 inbox as a table (ID, From, Kind, Subject, Age, Thread) and,
   if `awaiting_reply` is non-empty, a second table of this project's own
   unanswered `ask`s.

**`/message <slug> <text>`** — send a new message:
1. Validate `<slug>` against `project_list()` first — fail with the list of
   valid slugs rather than a raw error if it doesn't match.
2. Pick `kind`: a question → `"ask"`; a statement/notice → `"fyi"`.
3. Draft a short `subject` from `<text>`.
4. `message_send(to_project=<slug>, from_project=<this project's slug>,
   subject=..., body=<text>, kind=..., from_session="/message")`.
5. Show the user what was sent and to whom.

**`/message reply <id> <text>`** — reply within an existing thread:
1. `message_thread(<id>)` first, to confirm the thread and check
   `replies_left`. If `0`, refuse and say the thread has hit its
   reply-depth cap.
2. `message_send(in_reply_to=<id>, body=<text>, from_session="/message")` —
   **no routing arguments**; they're derived from the parent message.
