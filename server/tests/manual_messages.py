"""Manual smoke test for inter-agent messaging: the `messages` table, the
message_send/message_inbox/message_thread/message_mark tools, and the
messages_set_thread / messages_notify triggers they depend on.

Run with:

    EMBED_PROVIDER=mock python tests/manual_messages.py

EMBED_PROVIDER=mock is irrelevant to this path — messaging.py never calls
embed_one() — but kept for invocation consistency with the other
manual_*.py scripts, and it doubles as proof that nothing on this path
reaches the embedding provider (a message send must survive an embedding
outage that would take memory_upsert down).
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncpg

from memory_mcp import db
from memory_mcp.messaging import MAX_REPLY_DEPTH
from memory_mcp.server import (
    message_inbox,
    message_mark,
    message_send,
    message_thread,
    project_create,
)

A = "test-msg-a"
B = "test-msg-b"
C = "test-msg-c"


async def cleanup() -> None:
    for slug in (A, B, C):
        row = await db.fetchrow("SELECT id FROM projects WHERE slug = $1", slug)
        if row:
            await db.execute("DELETE FROM projects WHERE id = $1", row["id"])


async def main() -> None:
    await cleanup()
    await project_create(slug=A, name="Messaging Test A")
    await project_create(slug=B, name="Messaging Test B")
    await project_create(slug=C, name="Messaging Test C")

    # --- Test 1: round trip ---
    m1 = await message_send(
        to_project=B,
        from_project=A,
        kind="ask",
        subject="quote format",
        body="what format does /v1/quote expect?",
        from_session="manual_messages.py",
    )
    assert m1["thread_id"] == m1["id"]
    assert m1["reply_depth"] == 0
    assert m1["status"] == "unread"
    assert m1["replies_left"] == MAX_REPLY_DEPTH
    print("PASS: round trip send")

    # --- Test 2: inbox visibility is one-directional ---
    b_inbox = await message_inbox(project=B)
    b_ids = {m["id"] for m in b_inbox["messages"]}
    assert m1["id"] in b_ids
    b_row = next(m for m in b_inbox["messages"] if m["id"] == m1["id"])
    assert b_row["from_slug"] == A
    a_inbox = await message_inbox(project=A)
    assert m1["id"] not in {m["id"] for m in a_inbox["messages"]}
    print("PASS: inbox one-directional visibility")

    # --- Test 3: reply derivation (from_project required), parent auto-answered ---
    reply = await message_send(in_reply_to=m1["id"], from_project=B, body="it's JSON: {sku, qty}")
    assert reply["kind"] == "reply"
    assert reply["thread_id"] == m1["thread_id"]
    assert reply["reply_depth"] == 1
    assert reply["to_project"] == A
    assert reply["from_project"] == B
    parent_after = await message_thread(m1["id"])
    parent_row = next(mm for mm in parent_after["messages"] if mm["id"] == m1["id"])
    assert parent_row["status"] == "answered"
    assert parent_row["read_at"] is not None
    print("PASS: reply derivation + parent auto-answered")

    # --- Test 3b: regression for task #4 — replying to YOUR OWN prior
    # message in the thread (continuing it, not answering the other side)
    # must not flip direction. This was the bug: an unconditional swap of
    # the parent's from/to flipped the message back at its own sender.
    own_continuation = await message_send(
        in_reply_to=reply["id"], from_project=B, body="also: qty must be > 0"
    )
    assert own_continuation["from_project"] == B
    assert own_continuation["to_project"] == A
    assert own_continuation["reply_depth"] == 2
    print("PASS: reply to own prior message keeps direction (task #4 regression)")

    # --- Test 3c: from_project is required on a reply, and must be a party
    # to the conversation ---
    try:
        await message_send(in_reply_to=m1["id"], body="missing from_project")
        assert False, "expected a reply with no from_project to raise"
    except ValueError as e:
        assert "from_project is required" in str(e)
    try:
        await message_send(in_reply_to=m1["id"], from_project="does-not-exist", body="x")
        assert False, "expected an unknown from_project slug to raise"
    except ValueError as e:
        assert "Unknown project slug" in str(e)
    print("PASS: from_project required + validated on a reply")

    # --- Test 4: thread lookup by a non-root id ---
    t = await message_thread(reply["id"])
    assert t["thread_id"] == m1["thread_id"]
    assert [mm["id"] for mm in t["messages"]] == [m1["id"], reply["id"], own_continuation["id"]]
    assert t["max_depth"] == 2
    assert set(t["participants"]) == {A, B}
    print("PASS: thread lookup by non-root id")

    # --- Test 5: reply-depth cap raises, thread stays intact ---
    # Alternates the sender each hop, like a real back-and-forth exchange,
    # off the own_continuation branch created in test 3b.
    last_id = own_continuation["id"]
    last_sender = B
    for depth in range(2, MAX_REPLY_DEPTH):
        next_sender = A if last_sender == B else B
        r = await message_send(
            in_reply_to=last_id, from_project=next_sender, body=f"reply at depth {depth + 1}"
        )
        last_id = r["id"]
        last_sender = next_sender
    try:
        next_sender = A if last_sender == B else B
        await message_send(in_reply_to=last_id, from_project=next_sender, body="one too many")
        assert False, "expected the reply-depth cap to raise"
    except ValueError as e:
        assert "reply-depth cap" in str(e)
    final_thread = await message_thread(m1["id"])
    assert len(final_thread["messages"]) == MAX_REPLY_DEPTH + 1, (
        "the failed send must not have partially written anything"
    )
    print("PASS: depth cap raises and thread stays intact")

    # --- Test 6: trigger-level guards, exercised at the DB layer directly
    # (the Python layer would normally prevent all three) ---
    root2 = await message_send(to_project=B, from_project=A, kind="ask", body="second thread root")

    # A reply naming a third project not party to {A, B} must still raise —
    # both "flip" (from=B,to=A) and "continue own message" (from=A,to=B)
    # are now valid, so this has to reach outside the conversation to be
    # genuinely misrouted.
    try:
        await db.execute(
            """
            INSERT INTO messages (to_project_id, from_project_id, in_reply_to, body)
            VALUES ((SELECT id FROM projects WHERE slug=$1),
                     (SELECT id FROM projects WHERE slug=$2), $3, 'misrouted')
            """,
            C,
            A,
            root2["id"],
        )
        assert False, "expected a reply addressed to a non-participant project to raise"
    except asyncpg.exceptions.RaiseError:
        pass

    forced = await db.fetchrow(
        """
        INSERT INTO messages (to_project_id, from_project_id, in_reply_to, reply_depth, body)
        VALUES ((SELECT id FROM projects WHERE slug=$1),
                 (SELECT id FROM projects WHERE slug=$2), $3, 99, 'depth ignored')
        RETURNING reply_depth
        """,
        A,
        B,
        root2["id"],
    )
    assert forced["reply_depth"] == 1, "client-supplied reply_depth must be overridden by the trigger"

    try:
        await db.execute(
            """
            INSERT INTO messages (to_project_id, from_project_id, in_reply_to, body)
            VALUES ((SELECT id FROM projects WHERE slug=$1),
                     (SELECT id FROM projects WHERE slug=$2), 999999999, 'no parent')
            """,
            A,
            B,
        )
        assert False, "expected a nonexistent in_reply_to to raise"
    except asyncpg.exceptions.RaiseError:
        pass
    print("PASS: trigger-level guards (misrouted reply, forced depth, missing parent)")

    # --- Test 7: message_mark claim semantics ---
    claim1 = await message_mark(root2["id"])
    assert claim1["claimed"] is True
    claim2 = await message_mark(root2["id"])
    assert claim2["claimed"] is False
    print("PASS: message_mark claim semantics")

    # --- Test 8: rejections ---
    try:
        await message_send(to_project=A, from_project=A, body="talking to myself")
        assert False, "expected self-send to raise"
    except asyncpg.exceptions.CheckViolationError:
        pass
    try:
        await message_send(to_project=B, from_project=A, body="   ")
        assert False, "expected a blank body to raise"
    except asyncpg.exceptions.CheckViolationError:
        pass
    try:
        await message_send(to_project="does-not-exist", from_project=A, body="x")
        assert False, "expected an unknown slug to raise"
    except ValueError as e:
        assert "Unknown project slug" in str(e)
    print("PASS: rejections (self-send, blank body, unknown slug)")

    # --- Test 9: NOTIFY end-to-end, without running listener.py ---
    # Validates the messages_notify() trigger directly: a bare asyncpg
    # LISTEN connection, no listener.py process involved.
    notify_conn = await asyncpg.connect(dsn=db.database_url())
    try:
        b_id = await db.resolve_project_id(B)
        got = asyncio.Event()
        box: dict = {}

        def _on_notify(connection, pid, channel, payload) -> None:
            box.update(json.loads(payload))
            got.set()

        await notify_conn.add_listener(f"mb_msg_{b_id}", _on_notify)
        await message_send(
            to_project=B,
            from_project=A,
            kind="ask",
            subject="line one",
            body="alpha\n\nbeta   gamma",
        )
        await asyncio.wait_for(got.wait(), timeout=5)
        assert box["from"] == A
        assert box["kind"] == "ask"
        assert box["preview"] == "alpha beta gamma", "\\s+ must collapse to a single line"
        assert "\n" not in json.dumps(box)
        print("PASS: NOTIFY end-to-end delivery")
    finally:
        await notify_conn.close()

    # --- Test 10: payload ceiling on a large, newline-heavy body ---
    # NOTIFY payloads are hard-capped at 8000 bytes by Postgres. Without the
    # left()-truncation in messages_notify(), this fails at COMMIT with
    # "payload string too long" — and would fail only in production, on
    # whichever message happened to be long.
    notify_conn2 = await asyncpg.connect(dsn=db.database_url())
    try:
        b_id = await db.resolve_project_id(B)
        got2 = asyncio.Event()
        box2: dict = {}

        def _on_notify2(connection, pid, channel, payload) -> None:
            box2.update(json.loads(payload))
            got2.set()

        await notify_conn2.add_listener(f"mb_msg_{b_id}", _on_notify2)
        big_body = ("word " * 5000) + ("\n" * 50)  # ~25 KB, embedded newlines
        await message_send(to_project=B, from_project=A, kind="fyi", body=big_body)
        await asyncio.wait_for(got2.wait(), timeout=5)
        assert len(box2["preview"]) <= 200
        print("PASS: 20+ KB body does not blow the NOTIFY payload ceiling")
    finally:
        await notify_conn2.close()

    print("\nALL MESSAGING CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
