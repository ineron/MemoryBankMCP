# Live push after message_send — detail for /message, cross-project-requests.md, message-handling.md replies

Call this immediately after any `message_send` (a new message or a reply)
whose recipient is a different project's session — it's a best-effort fast
path on top of the durable Postgres row `message_send` already wrote.
Skipping it, or it finding no match, loses nothing: the recipient still
gets it the normal way, at their next `message_inbox` poll (`/start`), or
via a `memory_mcp.notifier` routine fire if nobody opens a session in time
(see `server/README.md`'s "Offline notifier" section). This replaced a
Monitor-armed `memory_mcp.listener` (2026-10-20) — that approach needed a
30-minutes-or-less watch mechanically re-armed to stay useful, which made
it the wrong foundation for "always on"; `SendMessage`/`ListAgents` is a
harness-native channel between live sessions with no such cap.

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
