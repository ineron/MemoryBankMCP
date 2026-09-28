"""Fires a Claude Code cloud routine when a cross-project message goes
unread and no live session is around to deliver it.

listener.py is started by Monitor *inside* an open Claude Code session and
dies the moment that session closes — so it only exists exactly when a
session is already delivering the message live in chat (a routine fire
there would be redundant) and never exists when one isn't (the actual gap).
This module is the opposite: a standalone daemon, independent of any
session, that watches every project's message channel and fires a
claude.ai routine's API trigger (POST .../routines/<id>/fire) only when a
message sits unread and no session's listener currently holds that
project's advisory lock.

    <server/.venv/bin/python> -u -m memory_mcp.notifier

Same two rules as listener.py, for the same reasons:

1. Meaningful lines go through status(), which writes to stdout — under
   systemd that lands in `journalctl --user -u mb-notifier`.
2. NOTIFY is the doorbell, Postgres is the mailbox. A missed NOTIFY loses
   nothing: `routine_fired_at IS NULL` on a still-unread row is recovered
   by the startup/reconnect catch-up sweep.

Deliberately does NOT use db.acquire()/db.get_pool() for the singleton
lock + LISTEN connection, for the same reason listener.py doesn't:
asyncpg's per-release reset (`pg_advisory_unlock_all(); ...; UNLISTEN *`)
would silently drop both the lock and every LISTEN the instant the
connection went back to the pool. One dedicated connection holds the lock
and every channel LISTEN for the life of the process. Housekeeping queries
(claiming routine_fired_at, checking pg_locks, the catch-up sweep) go
through the shared pool via `db` instead — they're one-shot statements
that don't need to survive a connection round-trip.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from typing import Any

import asyncpg
import httpx

from .listener import LOCK_CLASS, emit  # also runs load_dotenv() as a side effect
from . import db


def status(line: str) -> None:
    """Same convention as listener.py's status(), but its own prefix — it
    hardcodes "[mb-listener]", which would misidentify every notifier log
    line under journalctl as coming from the listener instead."""
    emit(f"[mb-notifier] {line}")


# Distinct advisory-lock namespace from listener.py's per-project LOCK_CLASS
# (19778): this is a single machine-wide singleton, not one per project.
SINGLETON_LOCK_CLASS = 19779
SINGLETON_LOCK_KEY = 0

HEARTBEAT_SECONDS = 45
RECONNECT_MIN, RECONNECT_MAX = 2.0, 60.0
BACKOFF_RESET_AFTER = RECONNECT_MAX
STANDBY_POLL_SECONDS = 15
PROJECT_REFRESH_SECONDS = 300
CATCHUP_INTERVAL_SECONDS = 300
CATCHUP_MAX_AGE = "24 hours"
MAX_BATCH = 20
ANTHROPIC_VERSION_DEFAULT = "2023-06-01"
ANTHROPIC_BETA_DEFAULT = "experimental-cc-routine-2026-04-01"

# --- config (env vars; listener.py's import above already ran load_dotenv) ---
FIRE_URL = os.environ.get("ROUTINE_FIRE_URL")
FIRE_TOKEN = os.environ.get("ROUTINE_FIRE_TOKEN")
GRACE_SECONDS = float(os.environ.get("ROUTINE_GRACE_SECONDS", "90"))
PROJECTS_ALLOW = {s.strip() for s in os.environ.get("ROUTINE_PROJECTS", "").split(",") if s.strip()}
KINDS_ALLOW = {s.strip() for s in os.environ.get("ROUTINE_KINDS", "ask,fyi,reply").split(",") if s.strip()}
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", ANTHROPIC_VERSION_DEFAULT)
ANTHROPIC_BETA = os.environ.get("ANTHROPIC_BETA", ANTHROPIC_BETA_DEFAULT)
HOSTNAME = socket.gethostname()

_extra_headers_raw = os.environ.get("ROUTINE_FIRE_EXTRA_HEADERS", "")
try:
    EXTRA_HEADERS: dict[str, str] = json.loads(_extra_headers_raw) if _extra_headers_raw else {}
except json.JSONDecodeError:
    status(f"WARN ROUTINE_FIRE_EXTRA_HEADERS is not valid JSON, ignoring: {_extra_headers_raw[:200]}")
    EXTRA_HEADERS = {}

# Atomically claims candidate messages (still unread, not yet fired, and not
# currently owned by a live per-project listener) and returns everything the
# fire payload needs in one round trip. classid/objid/objsubid match the
# rows Postgres creates for the two-arg `pg_advisory_lock(key1, key2)` form
# listener.py uses (LOCK_CLASS, project_id) — verified against listener.py's
# run_once(), which takes that lock on its own connection for the life of
# the session, so it disappears from pg_locks the moment the session closes.
CLAIM_SQL = """
WITH claimed AS (
    UPDATE messages m
       SET routine_fired_at = now()
     WHERE m.id = ANY($1::bigint[])
       AND m.status = 'unread'
       AND m.routine_fired_at IS NULL
       AND NOT EXISTS (
           SELECT 1 FROM pg_locks pl
            WHERE pl.locktype = 'advisory' AND pl.classid = $2
              AND pl.objid = m.to_project_id AND pl.objsubid = 2 AND pl.granted
       )
    RETURNING m.id, m.thread_id, m.reply_depth AS depth, m.kind,
              m.from_session AS session, m.from_project_id, m.created_at,
              left(regexp_replace(m.subject, '\\s+', ' ', 'g'), 120) AS subject,
              left(regexp_replace(m.body,    '\\s+', ' ', 'g'), 200) AS preview
)
SELECT c.*, COALESCE(p.slug, '?') AS from_slug
  FROM claimed c
  LEFT JOIN projects p ON p.id = c.from_project_id
 ORDER BY c.id
"""

SWEEP_SQL = f"""
SELECT id, to_project_id, kind
  FROM messages
 WHERE status = 'unread' AND routine_fired_at IS NULL
   AND created_at > now() - interval '{CATCHUP_MAX_AGE}'
 ORDER BY id
"""


class Notifier:
    def __init__(self) -> None:
        self.projects: dict[int, str] = {}  # project_id -> slug
        self.channels: set[str] = set()
        self.queue: asyncio.Queue[tuple[int, dict]] = asyncio.Queue(maxsize=2000)
        self.deadlines: dict[int, float] = {}  # project_id -> monotonic fire time
        self.pending_ids: dict[int, set[int]] = {}  # project_id -> buffered message ids
        self.dead = asyncio.Event()
        self.degraded = False
        self.stable_since: float | None = None

    # -- NOTIFY intake (mirrors listener.py's _on_notify/_consume split: no I/O in the callback) --

    def _on_notify(self, conn: Any, pid: int, channel: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            status(f"WARN unparseable payload on {channel}: {payload[:200]}")
            return
        try:
            project_id = int(channel.removeprefix("mb_msg_"))
        except ValueError:
            return
        try:
            self.queue.put_nowait((project_id, data))
        except asyncio.QueueFull:
            # Safe to drop: the row is still 'unread' in Postgres, and the
            # catch-up sweep will pick it up within CATCHUP_INTERVAL_SECONDS.
            status("WARN notifier backlog full — dropped one; still unread in DB")

    async def _buffer_consumer(self) -> None:
        while True:
            project_id, payload = await self.queue.get()
            slug = self.projects.get(project_id)
            if slug is None or (PROJECTS_ALLOW and slug not in PROJECTS_ALLOW):
                continue
            if KINDS_ALLOW and payload.get("kind", "") not in KINDS_ALLOW:
                continue
            mid = payload.get("id")
            if not mid:
                continue
            self.pending_ids.setdefault(project_id, set()).add(int(mid))
            # Grace period starts on the FIRST buffered message for this
            # project, not the most recent one — a steady trickle of
            # messages must not postpone the fire forever.
            self.deadlines.setdefault(project_id, time.monotonic() + GRACE_SECONDS)

    async def _fire_scheduler(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            ready = [pid for pid, deadline in self.deadlines.items() if deadline <= now]
            for pid in ready:
                ids = self.pending_ids.pop(pid, set())
                self.deadlines.pop(pid, None)
                if ids:
                    await self._maybe_fire(pid, sorted(ids))

    # -- catch-up sweep: recovers NOTIFYs missed while this process was down --

    async def _catchup_sweep(self) -> None:
        while True:
            try:
                rows = await db.fetch(SWEEP_SQL)
                by_project: dict[int, list[int]] = {}
                for r in rows:
                    slug = self.projects.get(r["to_project_id"])
                    if slug is None or (PROJECTS_ALLOW and slug not in PROJECTS_ALLOW):
                        continue
                    if KINDS_ALLOW and r["kind"] not in KINDS_ALLOW:
                        continue
                    by_project.setdefault(r["to_project_id"], []).append(r["id"])
                for pid, ids in by_project.items():
                    await self._maybe_fire(pid, ids)
            except Exception as exc:
                status(f"WARN catch-up sweep failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(CATCHUP_INTERVAL_SECONDS)

    # -- claim + fire --

    async def _maybe_fire(self, project_id: int, ids: list[int]) -> None:
        slug = self.projects.get(project_id)
        if slug is None:
            return
        try:
            rows = await db.fetch(CLAIM_SQL, ids, LOCK_CLASS)
        except Exception as exc:
            status(f"WARN claim query failed for {slug}: {type(exc).__name__}: {exc}")
            return
        if not rows:
            # Nothing claimable: already read, already fired by a prior
            # attempt, or a live session's listener now owns the project.
            return
        await self._fire(slug, rows)

    async def _fire(self, slug: str, rows: list[asyncpg.Record]) -> None:
        batch = rows[:MAX_BATCH]
        payload = {
            "source": "memory-bank",
            "v": 1,
            "to": slug,
            "machine": HOSTNAME,
            "messages": [
                {
                    "id": r["id"],
                    "thread_id": r["thread_id"],
                    "depth": r["depth"],
                    "kind": r["kind"],
                    "from": r["from_slug"],
                    "session": r["session"] or None,
                    "subject": r["subject"],
                    "preview": r["preview"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in batch
            ],
            "truncated": max(0, len(rows) - len(batch)),
        }
        ok, detail = await self._post_fire(payload)
        if ok:
            status(f"fired routine for {slug} — {len(rows)} message(s){detail}")
        else:
            status(f"WARN routine fire failed for {slug}: {detail}")
            ids = [r["id"] for r in rows]
            try:
                await db.execute(
                    "UPDATE messages SET routine_fired_at = NULL WHERE id = ANY($1::bigint[])",
                    ids,
                )
            except Exception as exc:
                status(f"WARN could not reset routine_fired_at for {slug}: {exc}")

    async def _post_fire(self, payload: dict) -> tuple[bool, str]:
        headers = {
            "Authorization": f"Bearer {FIRE_TOKEN}",
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": ANTHROPIC_BETA,
            "Content-Type": "application/json",
            **EXTRA_HEADERS,
        }
        body = {"text": json.dumps(payload)}
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                    resp = await client.post(FIRE_URL, headers=headers, json=body)
            except httpx.HTTPError as exc:
                if attempt == 0:
                    await asyncio.sleep(5)
                    continue
                return False, f"network error: {type(exc).__name__}: {exc}"
            if resp.status_code < 300:
                try:
                    session_id = resp.json().get("claude_code_session_id", "?")
                except Exception:
                    session_id = "?"
                return True, f" (session {session_id})"
            if 500 <= resp.status_code < 600 and attempt == 0:
                await asyncio.sleep(5)
                continue
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        return False, "exhausted retries"

    # -- project discovery --

    async def _refresh_projects(self, query_conn: asyncpg.Connection, listen_conn: asyncpg.Connection) -> int:
        rows = await query_conn.fetch("SELECT id, slug FROM projects")
        added = 0
        for r in rows:
            pid, slug = r["id"], r["slug"]
            self.projects[pid] = slug
            channel = f"mb_msg_{pid}"
            if channel not in self.channels:
                await listen_conn.add_listener(channel, self._on_notify)
                self.channels.add(channel)
                added += 1
        return added

    async def _project_refresh_loop(self, conn: asyncpg.Connection) -> None:
        while True:
            await asyncio.sleep(PROJECT_REFRESH_SECONDS)
            added = await self._refresh_projects(conn, conn)
            if added:
                status(f"picked up {added} new project channel(s)")

    async def _heartbeat(self, conn: asyncpg.Connection) -> None:
        # Same reasoning as listener.py: an idle LISTEN connection can sit
        # behind a NAT/firewall timeout that drops packets silently without
        # ever raising, so poke it periodically.
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await conn.fetchval("SELECT 1")

    async def run_once(self) -> None:
        conn = await asyncpg.connect(
            dsn=db.database_url(),
            timeout=10,
            command_timeout=30,
            server_settings={"application_name": "mb-notifier"},
        )
        try:
            waited = False
            while not await conn.fetchval(
                "SELECT pg_try_advisory_lock($1::int, $2::int)",
                SINGLETON_LOCK_CLASS,
                SINGLETON_LOCK_KEY,
            ):
                if not waited:
                    status("another notifier instance is already running; standing by")
                    waited = True
                await asyncio.sleep(STANDBY_POLL_SECONDS)
            if waited:
                status("took over as the active notifier")

            self.dead.clear()
            conn.add_termination_listener(lambda c: self.dead.set())

            await self._refresh_projects(conn, conn)
            status(f"ready — watching {len(self.projects)} project(s) for offline routine delivery")
            self.degraded = False
            self.stable_since = time.monotonic()

            tasks = {
                asyncio.create_task(self._buffer_consumer()),
                asyncio.create_task(self._fire_scheduler()),
                asyncio.create_task(self._catchup_sweep()),
                asyncio.create_task(self._project_refresh_loop(conn)),
                asyncio.create_task(self._heartbeat(conn)),
                asyncio.create_task(self.dead.wait()),
            }
            done, todo = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in todo:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc
            if self.dead.is_set():
                raise ConnectionResetError("notifier connection terminated")
        finally:
            await conn.close(timeout=5)


async def run() -> int:
    notifier = Notifier()
    backoff = RECONNECT_MIN
    while True:
        try:
            await notifier.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if (
                notifier.stable_since is not None
                and time.monotonic() - notifier.stable_since >= BACKOFF_RESET_AFTER
            ):
                backoff = RECONNECT_MIN
            if not notifier.degraded:
                notifier.degraded = True
                status(f"WARN lost DB connection ({type(exc).__name__}: {exc}); reconnecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)


def main() -> None:
    if not FIRE_URL or not FIRE_TOKEN:
        status("routine firing disabled — ROUTINE_FIRE_URL/ROUTINE_FIRE_TOKEN unset")
        raise SystemExit(0)

    try:
        db.database_url()  # fail loud and early, not inside the retry loop
    except RuntimeError as exc:
        status(f"FATAL {exc}")
        raise SystemExit(2)

    try:
        rc = asyncio.run(run())
    except KeyboardInterrupt:
        rc = 0
    except BaseException as exc:
        status(f"FATAL unhandled {type(exc).__name__}: {exc}")
        rc = 1
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
