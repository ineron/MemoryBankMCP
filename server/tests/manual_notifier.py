"""Manual smoke test for memory_mcp.notifier: the routine-fire claim query,
its suppression by a live listener lock, idempotency, and the failed-fire
retry reset — without ever making a real HTTP call to claude.ai.

Run with:

    EMBED_PROVIDER=mock python tests/manual_notifier.py

EMBED_PROVIDER=mock is irrelevant to this path (same reasoning as
manual_messages.py) but kept for invocation consistency.

This does NOT test notifier.py's asyncio run loop (LISTEN/reconnect/
project-refresh) end-to-end — that needs a live, restartable process and is
better exercised manually per server/README.md's "Offline notifier"
section. This script targets the two pieces that are wrong-once-means-
double-notify-or-silent-drop: the claim SQL and the failure-reset path.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncpg

from memory_mcp import db, notifier
from memory_mcp.server import message_send, project_create

A = "test-notifier-a"
B = "test-notifier-b"


async def cleanup() -> None:
    for slug in (A, B):
        row = await db.fetchrow("SELECT id FROM projects WHERE slug = $1", slug)
        if row:
            await db.execute("DELETE FROM projects WHERE id = $1", row["id"])


async def main() -> None:
    await cleanup()
    await project_create(slug=A, name="Notifier Test A")
    project_b = await project_create(slug=B, name="Notifier Test B")
    b_id = project_b["id"]

    # --- Test 1: claimable when nobody holds B's listener lock ---
    m1 = await message_send(to_project=B, from_project=A, kind="ask", body="notifier test 1")
    rows = await db.fetch(notifier.CLAIM_SQL, [m1["id"]], notifier.LOCK_CLASS)
    assert len(rows) == 1, "expected the message to be claimable with no live listener"
    assert rows[0]["id"] == m1["id"]
    assert rows[0]["from_slug"] == A
    print("PASS: claimable with no live listener")

    # --- Test 2: idempotent — a second claim attempt on the same id returns nothing ---
    rows_again = await db.fetch(notifier.CLAIM_SQL, [m1["id"]], notifier.LOCK_CLASS)
    assert rows_again == [], "a message must never be claimed twice"
    check = await db.fetchrow("SELECT routine_fired_at, status FROM messages WHERE id = $1", m1["id"])
    assert check["routine_fired_at"] is not None
    assert check["status"] == "unread", "the notifier must never touch message status itself"
    print("PASS: idempotent claim, status left untouched")

    # --- Test 3: suppressed while a listener holds B's advisory lock ---
    m2 = await message_send(to_project=B, from_project=A, kind="fyi", body="notifier test 2")
    lock_conn = await asyncpg.connect(dsn=db.database_url())
    try:
        got = await lock_conn.fetchval(
            "SELECT pg_try_advisory_lock($1::int, $2::int)", notifier.LOCK_CLASS, b_id
        )
        assert got, "test setup: could not take the lock notifier is supposed to respect"
        rows = await db.fetch(notifier.CLAIM_SQL, [m2["id"]], notifier.LOCK_CLASS)
        assert rows == [], "must not fire while a listener holds the project's lock"
        check2 = await db.fetchrow("SELECT routine_fired_at FROM messages WHERE id = $1", m2["id"])
        assert check2["routine_fired_at"] is None
        print("PASS: suppressed while a live listener holds the lock")

        await lock_conn.fetchval("SELECT pg_advisory_unlock($1::int, $2::int)", notifier.LOCK_CLASS, b_id)
        rows_after = await db.fetch(notifier.CLAIM_SQL, [m2["id"]], notifier.LOCK_CLASS)
        assert len(rows_after) == 1, "must become claimable once the lock is released"
        print("PASS: claimable again once the listener lock is released")
    finally:
        await lock_conn.close()

    # --- Test 4: a failed fire resets routine_fired_at so the next sweep retries ---
    m3 = await message_send(to_project=B, from_project=A, kind="ask", body="notifier test 3")

    async def _fake_post_fire_fail(payload: dict) -> tuple[bool, str]:
        return False, "simulated failure"

    orig_post_fire = notifier.Notifier._post_fire
    notifier.Notifier._post_fire = lambda self, payload: _fake_post_fire_fail(payload)
    try:
        inst = notifier.Notifier()
        inst.projects[b_id] = B
        await inst._maybe_fire(b_id, [m3["id"]])
        check3 = await db.fetchrow("SELECT routine_fired_at FROM messages WHERE id = $1", m3["id"])
        assert check3["routine_fired_at"] is None, "a failed fire must reset routine_fired_at to NULL"
        print("PASS: failed fire resets routine_fired_at for retry")
    finally:
        notifier.Notifier._post_fire = orig_post_fire

    # --- Test 5: a successful fire leaves routine_fired_at set and builds the documented payload shape ---
    captured: dict = {}

    async def _fake_post_fire_ok(payload: dict) -> tuple[bool, str]:
        captured.update(payload)
        return True, " (session fake)"

    notifier.Notifier._post_fire = lambda self, payload: _fake_post_fire_ok(payload)
    try:
        inst = notifier.Notifier()
        inst.projects[b_id] = B
        await inst._maybe_fire(b_id, [m3["id"]])
        check4 = await db.fetchrow("SELECT routine_fired_at FROM messages WHERE id = $1", m3["id"])
        assert check4["routine_fired_at"] is not None
        assert captured["source"] == "memory-bank"
        assert captured["to"] == B
        assert captured["truncated"] == 0
        assert len(captured["messages"]) == 1
        assert captured["messages"][0]["id"] == m3["id"]
        assert captured["messages"][0]["from"] == A
        print("PASS: successful fire claims the message and builds the documented payload")
    finally:
        notifier.Notifier._post_fire = orig_post_fire

    # --- Test 6: unconfigured ROUTINE_FIRE_URL/TOKEN -> main() exits 0, not an error ---
    # Reflects this repo's actual current state until the routine is created
    # per server/README.md's "Offline notifier" setup steps.
    if not notifier.FIRE_URL or not notifier.FIRE_TOKEN:
        try:
            notifier.main()
            assert False, "main() must call sys.exit"
        except SystemExit as e:
            assert e.code == 0, f"expected exit 0 when unconfigured, got {e.code}"
        print("PASS: main() exits 0 when ROUTINE_FIRE_URL/TOKEN are unset")
    else:
        print("SKIP: ROUTINE_FIRE_URL/TOKEN are set in this environment — not exercising the disabled path")

    await cleanup()
    print("\nALL NOTIFIER CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
