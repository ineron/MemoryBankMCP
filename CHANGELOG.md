# Changelog

This project doesn't cut tagged releases (`server/pyproject.toml` has
stayed at `0.1.0` since the start) — it's a Claude Code configuration
package that evolves continuously across the projects that use it. So
instead of semver sections, this changelog groups entries **by date**,
reconstructed from `git log` on 2026-10-20. From that point on, each
dated section should be added alongside the commit(s) it describes, newest
date first, in the style of [Keep a Changelog](https://keepachangelog.com/)
(`Added`/`Changed`/`Fixed`/`Removed`).

## 2026-10-20

### Added
- `protocol_audit`: an append-only log of every `protocol_hook.py`
  decision (deny/ask/inform/none/unknown) per classified action —
  migration 006 + `schema.sql`, written best-effort so a logging failure
  never blocks or changes the decision already made.
- `/workflow:execute` now requires any Chrome/browser check to go through
  Playwright (the project's own setup, or a new headless-Chromium test),
  instead of letting the agent improvise a launch method each time.

### Changed
- Replaced `Monitor`-armed `listener.py` with the harness's native
  `SendMessage`/`ListAgents` for live inter-project message delivery (a
  `Monitor` watch caps at ~30 min and needed mechanical re-arming).
- **Reverted the above the same day**: `SendMessage`/`ListAgents` turned
  out to be hard-restricted to sessions under the *same OS user*, by
  design (socket ownership + per-session auth token, no override via env
  var/CLI flag/shared directory) — confirmed via a live cross-account test
  against a different project running under a different OS account.
  `/start` now arms `listener.py`/`Monitor` again as the mechanism that
  actually guarantees delivery (pure Postgres `LISTEN`/`NOTIFY`, works
  across OS accounts and machines); the `SendMessage` fast path
  (`.claude/reference/live-push.md`) stays as a free, zero-cost bonus for
  same-OS-account project pairs only.
- Dropped a redundant `timeout_ms` from `start.md`'s `Monitor(...)` example
  (it's ignored once `persistent: true` is set).

## 2026-09-29

### Added
- Action-protocols layer: a `PreToolUse` hook (`protocol_hook.py`) that
  looks up deterministic "how do we do X" rules keyed on the action a tool
  call is about to take (Bash/Read/Edit/Write) — the kind of rule
  `memory_search`'s topic-based retrieval can't reliably surface. New
  `kind='protocol'` node type, the `_global` project, `protocol_match`/
  `protocol_vector_cache` tables (migration 005), and
  `protocol_add`/`protocol_check`/`protocol_list` MCP tools.
- Offline notifier daemon (`memory_mcp.notifier`): a standalone systemd
  service that fires a claude.ai routine when a message sits unread with
  no session listener around to deliver it live (migration 004 adds
  `routine_fired_at`).

### Fixed
- Cleaned up protocol test residue in `manual_protocols.py` — global-scope
  test rules were landing on the shared `_global` project and never being
  removed, leaving a duplicate `test.priority` rule after every run.

## 2026-09-21

### Changed
- Reversed the previous day's bounded-`Monitor` re-arm guidance back to
  mechanical re-arm (instead of "go dark after the first expiry") — live
  evidence (`pg_stat_activity`) showed listener backends silently dying
  and never coming back under the "go dark" guidance. The actual fix was
  making expiry-handling cheap (pgrep + re-arm, no retrieval calls, no
  narration) rather than avoiding it.

## 2026-09-20

### Fixed
- `message_send` flipped thread direction when a project replied to its
  own prior message instead of the other side's — fixed in both
  `messaging.py` and the DB trigger (migration 003); replies now require
  `from_project` so direction is derived correctly.

### Changed
- Bumped `MESSAGE_MAX_REPLY_DEPTH` default 6 → 10.
- Extracted `/start`'s message-triage and cross-project-request procedures
  into `.claude/reference/{message-handling,cross-project-requests}.md` so
  they're read only when actually needed, not on every `/start`.
- Tightened bounded-`Monitor` re-arm guidance to never re-arm a `Monitor`
  lacking `persistent` support — reversed the very next day (see
  2026-09-21) once it turned out to trade a small bounded cost for an
  unbounded one.
- `/save`: always pass the existing `id` when upserting the `active` node
  (prevents a silent duplicate), and keep that node short since `/start`
  reads its full body every session.

## 2026-08-22

### Added
- Codegraph plugin (`memory_mcp/codegraph/`): a tree-sitter based function/
  class/call map of a project's own source, stored in its own droppable
  Postgres schema. Registered as `codegraph_build`/`map`/`deps`/`search`/
  `issues` MCP tools only when the `codegraph` extra is installed.

### Fixed (found while dogfooding the plugin across 3 connected projects)
- Incremental-rebuild staleness leaving resolved call edges dangling.
- Name-collision false resolutions (module-qualified vs. instance-method
  vs. bare calls).
- Decorator-registered functions wrongly flagged as unused.
- Cycle detection conflating real multi-function cycles with ordinary
  self-recursion.
- Unbounded entity growth from scanning installed dependencies — added
  `.gitignore`-aware file discovery.

### Changed
- Synced the root README with the codegraph plugin and `/scan-code`.
- Ignored the `articles/` directory.

## 2026-08-11

### Fixed
- `pgrep` self-match false positive in `/start`'s listener guard: an
  unanchored pattern also matched the Bash-tool wrapper process running
  the `pgrep` command itself. Anchored with `$`.
- `listener.py`'s reconnect backoff never actually reset after a stable
  connection (dead `run_once()` return path), so it kept escalating toward
  the 60s cap on every disconnect regardless of prior stability.

## 2026-08-05

### Added
- `hours_report.py`: weekly hours + cost per project, parsed from local
  session transcripts and `ccusage session --json`.

### Changed
- Reworked cross-project message autonomy: an idle session now picks up
  requested work immediately (same as a task from its own user) instead
  of always filing to inbox regardless of what else it was doing; a
  mid-task session still files to inbox and replies that it's queued.

## 2026-07-31

### Added
- Inter-agent messaging channel: `messages` table (migration 002,
  trigger-derived `thread_id`/`reply_depth`, `pg_notify` trigger),
  `messaging.py` + 4 MCP tools (`message_send`/`inbox`/`thread`/`mark`),
  `listener.py` (standalone `LISTEN`/`NOTIFY` process armed via `Monitor`
  from `/start`, since the MCP server itself can't push), and the
  `/message` command for manual send/reply/inbox-check.
- Documented inter-agent messaging in the root README.

### Fixed
- `install.py`'s slug fallback fell back to the existing project's
  *display name* instead of its *slug*, silently registering a duplicate
  project on any re-run without `--slug` when the two differed even just
  in case.
- `/start`'s listener arm now checked with `pgrep` first and skipped
  arming a second `Monitor` if one was already running for the project.

## 2026-07-22

### Fixed
- "bugs fix 220726" (no further detail recorded in the original commit).

## 2026-07-17

### Added
- Initial commit — project scaffold.
- Cross-project filing example added to the README
  (`memory_upsert(..., filed_from_project=...)` plus the resulting
  `/start` "Filed from other sessions" inbox block).

### Changed
- Updated `LICENSE`.
