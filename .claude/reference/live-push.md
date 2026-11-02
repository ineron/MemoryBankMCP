# Live push after message_send — detail for /message, cross-project-requests.md, message-handling.md replies

Call this immediately after any `message_send` (a new message or a reply)
whose recipient is a different project's session — it's a free, best-effort
**bonus** fast path on top of the durable Postgres row `message_send`
already wrote, and on top of `/start`'s own `listener.py`/`Monitor` arming
(`.claude/commands/start.md` §4), which remains the path that actually
guarantees live delivery. Skipping this step, or it finding no match, loses
nothing: the recipient still gets it the normal way, via the armed
Postgres listener if one is up, at their next `message_inbox` poll
(`/start`), or via a `memory_mcp.notifier` routine fire if nobody opens a
session in time (see `server/README.md`'s "Offline notifier" section).

**Two confirmed hard limitations — don't try to work around either:**

1. (2026-10-20) `SendMessage`/`ListAgents` cross-session messaging is
   restricted to sessions under the *same OS user*, by design — the
   delivery socket is owned by that user and gated by a per-session auth
   token; Claude Code actively refuses to set one up in a directory owned
   by a different user, regardless of
   `CLAUDE_CODE_TMPDIR`/`--messaging-socket-path`/shared-directory tricks.
   Every project on this machine currently runs as the same OS user
   (`eugene`, since 2026-10-30), so this boundary doesn't bite today — but
   if a project ever moves to, or is created under, a different account or
   a different machine sharing this same Postgres DB, this step will
   reliably find no match for it.
2. (2026-10-27, still live after the 2026-10-30 account move) `ListAgents`
   only enumerates sessions whose top-level process is actually running —
   an interactive session idle for more than ~1h gets suspended and drops
   out entirely until its user attaches again, confirmed live when a
   peer's `SendMessage` to a genuinely-running but long-idle `KafkaConsumer`
   session found zero reachable agents. This has nothing to do with OS
   accounts and affects same-account pairs too. `listener.py`/`Monitor`
   (step 4's default arming) is an independent background process, not the
   suspended session loop, so it's unaffected by this and is why it stays
   the default rather than this fast path.

For either limitation, `listener.py`/`Monitor` is the thing that still
delivers — pure Postgres `LISTEN`/`NOTIFY`, with no OS-level session
discovery and no dependency on the top-level session process staying
scheduled.

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
