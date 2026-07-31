"""Live delivery of inter-agent messages into a Claude Code session.

server.py is a request/response stdio process: it can only answer calls,
never push. Live delivery therefore has to come from Claude Code's Monitor
tool, which turns every stdout line of a background command into a chat
notification. This module is that command:

    <server/.venv/bin/python> -u -m memory_mcp.listener --project <slug>

Two rules govern everything below.

1. EVERYTHING THE SESSION MUST SEE GOES TO STDOUT, FLUSHED. Monitor
   notifies on stdout only; stderr lands in an output file nothing reads.
   A listener that dies into a stderr traceback is indistinguishable from a
   healthy quiet one — so fatal errors are printed to stdout *first*, and
   only then does the process exit.

2. NOTIFY IS THE DOORBELL, POSTGRES IS THE MAILBOX. A missed notification
   loses nothing: the row stays status='unread', so the next startup drain
   (or any message_inbox call) recovers it. This is why the reconnect loop
   can afford to be simple and why a full notification queue may drop.

Deliberately does NOT use db.acquire()/db.get_pool(): asyncpg's per-release
reset query is `pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL`,
so a pooled connection silently drops any LISTEN and any advisory lock the
instant it's released back to the pool — and permanently parking a pooled
connection here would burn 1 of the pool's 10 slots on a process that isn't
even the MCP server. This module opens and holds exactly one connection of
its own via asyncpg.connect(), reusing only db.database_url() so DSN
resolution stays in one place.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import asyncpg
from dotenv import load_dotenv

# Absolute, not cwd-relative — Monitor launches this from whichever project
# repo the session is in, never from server/ (same reasoning as
# server.py:28-31, more so here since there's no MCP client resolving cwd).
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from . import db  # noqa: E402 — must follow load_dotenv

HEARTBEAT_SECONDS = 45
RECONNECT_MIN, RECONNECT_MAX = 2.0, 60.0
STANDBY_POLL_SECONDS = 15
SEEN_CAP = 4000
LOCK_CLASS = 19778  # 'M','B' on a phone keypad — advisory-lock namespace
KIND_ICON = {"ask": "❓", "reply": "↩", "fyi": "ℹ"}


class FatalError(Exception):
    """Misconfiguration, not a transient blip: do not retry, print and exit."""


def emit(line: str) -> None:
    """One stdout line == one chat notification. Explicit flush because
    stdout to a pipe is block-buffered by default, which would hold every
    notification hostage until the buffer fills."""
    sys.stdout.write(line.rstrip("\n") + "\n")
    sys.stdout.flush()


def status(line: str) -> None:
    """Plumbing line (ready / WARN / FATAL) — report, never act on."""
    emit(f"[mb-listener] {line}")


def format_message(p: dict[str, Any]) -> str:
    icon = KIND_ICON.get(p.get("kind", ""), "•")
    who = p["from"] + (f"/{p['session']}" if p.get("session") else "")
    subject = f"{p['subject']} — " if p.get("subject") else ""
    return (
        f"💬 msg#{p['id']} {icon} {p['kind']} from {who} "
        f"[thread {p['thread_id']} depth {p['depth']}] {subject}{p['preview']}"
    )


class Listener:
    def __init__(self, slug: str, drain: bool) -> None:
        self.slug, self.drain = slug, drain
        self.project_id: int | None = None
        self.channel: str | None = None
        self.seen: set[int] = set()
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self.dead = asyncio.Event()
        self.degraded = False

    def _first_time(self, mid: int) -> bool:
        """False if already emitted. Needed because the startup drain and
        the live LISTEN deliberately overlap (see run_once), and because a
        reconnect re-drains everything still unread."""
        if mid in self.seen:
            return False
        self.seen.add(mid)
        if len(self.seen) > SEEN_CAP:  # ids are monotonic: keep the tail
            for old in sorted(self.seen)[: SEEN_CAP // 2]:
                self.seen.discard(old)
        return True

    def _on_notify(self, conn: Any, pid: int, channel: str, payload: str) -> None:
        """Runs inside asyncpg's protocol data handler. Do no I/O here: hand
        off to the queue so ordering against the startup drain is decided in
        one place (run_once) rather than by callback timing."""
        try:
            self.queue.put_nowait(json.loads(payload))
        except asyncio.QueueFull:
            # Safe to drop: the row is still 'unread' in Postgres.
            status("WARN notification backlog full — dropped one; still unread in DB")
        except json.JSONDecodeError:
            status(f"WARN unparseable payload on {channel}: {payload[:200]}")

    async def _drain_unread(self, conn: asyncpg.Connection) -> int:
        rows = await conn.fetch(
            r"""
            SELECT m.id, m.thread_id, m.reply_depth AS depth, m.kind,
                   m.from_session AS session,
                   left(regexp_replace(m.subject, '\s+', ' ', 'g'), 120) AS subject,
                   left(regexp_replace(m.body,    '\s+', ' ', 'g'), 200) AS preview,
                   COALESCE(fp.slug, '?') AS "from"
              FROM messages m
              LEFT JOIN projects fp ON fp.id = m.from_project_id
             WHERE m.to_project_id = $1 AND m.status = 'unread'
             ORDER BY m.id
            """,
            self.project_id,
        )
        n = 0
        for r in rows:
            if self._first_time(r["id"]):
                emit(format_message(dict(r)))
                n += 1
        return n

    async def run_once(self) -> None:
        conn = await asyncpg.connect(
            dsn=db.database_url(),
            timeout=10,
            command_timeout=30,
            # Makes `SELECT * FROM pg_stat_activity` immediately legible
            # when someone wonders what all these idle connections are.
            server_settings={"application_name": f"mb-listener:{self.slug}"},
        )
        try:
            if self.project_id is None:
                row = await conn.fetchrow("SELECT id FROM projects WHERE slug = $1", self.slug)
                if row is None:
                    raise FatalError(
                        f"unknown project slug '{self.slug}' — register it with "
                        "project_create, or fix project.slug in .claude/settings.json"
                    )
                self.project_id = row["id"]
                self.channel = f"mb_msg_{self.project_id}"

            # One live listener per project. The lock lives on THIS
            # connection, so Postgres releases it the moment the process
            # (or the machine) goes away — no stale lockfile, correct
            # across machines sharing the DB. Standing by rather than
            # exiting gives automatic failover when the holding session ends.
            waited = False
            while not await conn.fetchval(
                "SELECT pg_try_advisory_lock($1::int, $2::int)", LOCK_CLASS, self.project_id
            ):
                if not waited:
                    status(
                        f"another session is already listening for {self.slug}; "
                        "standing by to take over"
                    )
                    waited = True
                await asyncio.sleep(STANDBY_POLL_SECONDS)
            if waited:
                status(f"took over listening for {self.slug}")

            self.dead.clear()
            conn.add_termination_listener(lambda c: self.dead.set())

            # LISTEN *before* the drain. Reversed, a message committed
            # between the SELECT and the LISTEN would be invisible to both
            # until the next restart. The overlap this ordering creates
            # (one message seen by both paths) is absorbed by _first_time().
            await conn.add_listener(self.channel, self._on_notify)

            pending = await self._drain_unread(conn) if self.drain else 0
            status(f"ready on {self.channel} for {self.slug} — {pending} unread message(s) replayed")
            self.degraded = False

            tasks = {
                asyncio.create_task(self._consume()),
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
                # add_termination_listener fires on ANY connection loss —
                # including e.g. pg_terminate_backend — without the wrapped
                # dead.wait() task itself raising. Left unraised, run()'s
                # except-based WARN/backoff reporting would never fire and
                # this would reconnect completely silently.
                raise ConnectionResetError("listener connection terminated")
        finally:
            await conn.close(timeout=5)

    async def _consume(self) -> None:
        while True:
            payload = await self.queue.get()
            mid = int(payload.get("id") or 0)
            if mid and self._first_time(mid):
                emit(format_message(payload))

    async def _heartbeat(self, conn: asyncpg.Connection) -> None:
        """A LISTEN connection is otherwise perfectly idle, and asyncpg does
        not speak libpq's keepalives=* DSN options (it's a from-scratch
        protocol implementation, not a libpq binding). A NAT timeout or
        firewall can therefore leave a half-open socket that never delivers
        another notification and never raises. Poke it."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await conn.fetchval("SELECT 1")


async def run(slug: str, drain: bool) -> int:
    listener = Listener(slug, drain)
    backoff = RECONNECT_MIN
    while True:
        try:
            await listener.run_once()
            backoff = RECONNECT_MIN
        except FatalError as exc:
            status(f"FATAL {exc}")
            return 2
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Report the *transition*, not every attempt: a 10-minute DB
            # outage must not become 300 chat notifications. Recovery
            # announces itself via the "ready on ..." line in run_once.
            if not listener.degraded:
                listener.degraded = True
                status(f"WARN lost DB connection for {slug} ({type(exc).__name__}: {exc}); reconnecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m memory_mcp.listener")
    parser.add_argument(
        "--project", required=True, help="recipient project slug (.claude/settings.json -> project.slug)"
    )
    parser.add_argument(
        "--no-drain", action="store_true", help="skip the startup replay of already-unread messages"
    )
    args = parser.parse_args()

    try:
        db.database_url()  # fail loud and early, not inside the retry loop
    except RuntimeError as exc:
        status(f"FATAL {exc}")
        raise SystemExit(2)

    try:
        rc = asyncio.run(run(args.project, drain=not args.no_drain))
    except KeyboardInterrupt:
        rc = 0
    except BaseException as exc:
        # Last line of defence: an uncaught traceback goes to stderr, which
        # Monitor never surfaces. Say something on stdout before dying.
        status(f"FATAL unhandled {type(exc).__name__}: {exc}")
        rc = 1
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
