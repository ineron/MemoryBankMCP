# Live push after message_send — detail for /message, cross-project-requests.md, message-handling.md replies

Call this immediately after any `message_send` (a new message or a reply)
whose recipient is a different project's session — as of 2026-10-27 this is
the **default** live-delivery path, not a bonus on top of something else:
`/start` no longer arms `listener.py`/`Monitor` by default (see
`.claude/commands/start.md` §4). Skipping this step, or it finding no
match, loses nothing: the recipient still gets it the normal way, at their
next `message_inbox` poll (`/start`), via a manually-armed Postgres
listener if that project opted into one, or via a `memory_mcp.notifier`
routine fire if nobody opens a session in time (see `server/README.md`'s
"Offline notifier" section).

**Hard limitation, confirmed 2026-10-20 (doesn't apply to this machine's
current setup, but keep it in mind):** `SendMessage`/`ListAgents`
cross-session messaging is restricted to sessions under the *same OS
user*, by design — the delivery socket is owned by that user and gated by
a per-session auth token; Claude Code actively refuses to set one up in a
directory owned by a different user, regardless of
`CLAUDE_CODE_TMPDIR`/`--messaging-socket-path`/shared-directory tricks. So
this step can only ever help when the target project's session happens to
run under the same OS account as this one. Every project on this machine
currently runs as the same OS user (`eugene`), which is exactly why this
became the default — but if a project ever moves to, or is created under,
a different account or a different machine sharing this same Postgres DB,
this step will reliably find no match for it, and that project's `/start`
should arm `listener.py`/`Monitor` manually (step 4's opt-in branch) to get
live delivery back — pure Postgres `LISTEN`/`NOTIFY` has no OS-level
session-discovery boundary to hit.

1. `ListAgents()`.
2. Normalize the target project's slug and every listed peer's name the
   same way — lowercase, strip everything but letters/digits (e.g.
   `memory-bank-mcp` and `MemoryBankMCP` both normalize to
   `memorybankmcp`). Compare.
3. **Exactly one peer normalizes to the same string** →
   `SendMessage(to=<that peer's exact listed name>, message="💬 new message
   #<id> (<kind>) from <this project's slug> — <subject>. Call
   message_thread(<id>) to read it.")`. The first line must be
   self-contained (id, kind, sender project, subject) — `SendMessage`'s own
   rule is that only the first line previews to the recipient's user.
4. **Zero matches, or more than one** → skip silently. Do not guess a name
   and do not ask the user to disambiguate mid-send — the message is
   already safely in Postgres either way, and guessing wrong would deliver
   a confusing push to an unrelated session.
5. A `[Cross-session delivery notice]` in the tool result, or no
   confirmation at all (a remote-control, cloud, or Claude Desktop peer),
   does not mean the message was missed — it only means this specific fast
   path didn't get a confirmation back. Nothing further to do either way.

This is entirely the sender's responsibility — the MCP server itself
(`messaging.py`/`message_send`) has no access to `SendMessage`/`ListAgents`
(those are harness tools, available only to the calling session, not to
the stdio MCP server process), so it cannot do this step on its own.

**For this to find a session at all**, that session needs to have been
started with an address name that normalizes to its project slug — in
practice, `claude -n <slug>` (or close enough after normalization, e.g.
`KafkaConsumer` for slug `kafka-consumer`). There's no schema field backing
this mapping; it's a naming convention the user maintains when starting
sessions, not something this package can verify except by trying.
