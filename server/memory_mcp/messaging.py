"""Inter-agent messaging: a conversational channel between project sessions,
separate from the inbox-task mechanism in server.py's memory_upsert(...,
filed_from_project=...). A message carries no embedding and no graph edges
(memory_upsert always calls embed_one() before touching Postgres, so a
chat-shaped write would cost an API call per message and would fail outright
during an embedding-provider outage) — see migrations/002_add_messages.sql
for the schema and full rationale.

Live delivery is NOT this module's job: server.py is a request/response
stdio process and cannot push. See listener.py, which turns the
messages_notify() trigger's pg_notify into Claude Code chat notifications
via the Monitor tool.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from . import db

# Hard backstop on reply_depth is 32 (schema CHECK); this is the tighter,
# operator-tunable cap actually enforced here. Two idle autonomous agents
# answering each other will otherwise trade messages until one exhausts its
# context — the cap forces a human back into the loop instead.
MAX_REPLY_DEPTH = int(os.environ.get("MESSAGE_MAX_REPLY_DEPTH", "6"))

# A runaway body must not blow /start's or message_inbox's context budget;
# message_thread has no cap and returns the full body regardless.
BODY_PREVIEW_LEN = 4000

VALID_KINDS = ("ask", "reply", "fyi")
VALID_MARK_STATUSES = ("read", "answered")
VALID_INBOX_STATUSES = ("unread", "read", "answered")


def _with_preview(row: Any) -> dict[str, Any]:
    d = dict(row)
    body = d.get("body") or ""
    if len(body) > BODY_PREVIEW_LEN:
        d["body"] = body[:BODY_PREVIEW_LEN]
        d["body_truncated"] = True
    else:
        d["body_truncated"] = False
    if "reply_depth" in d:
        d["replies_left"] = MAX_REPLY_DEPTH - d["reply_depth"]
    return d


async def send(
    body: str,
    to_project: Optional[str] = None,
    subject: str = "",
    kind: Optional[str] = None,
    from_project: Optional[str] = None,
    from_session: str = "",
    in_reply_to: Optional[int] = None,
) -> dict[str, Any]:
    if in_reply_to is not None:
        parent = await db.fetchrow(
            """
            SELECT m.id, m.thread_id, m.reply_depth,
                   m.to_project_id, m.from_project_id,
                   tp.slug AS to_slug, fp.slug AS from_slug
            FROM messages m
            JOIN projects tp ON tp.id = m.to_project_id
            LEFT JOIN projects fp ON fp.id = m.from_project_id
            WHERE m.id = $1
            """,
            in_reply_to,
        )
        if parent is None:
            raise ValueError(f"No message id={in_reply_to} to reply to")
        if parent["from_project_id"] is None:
            raise ValueError(
                f"Message {in_reply_to}'s sender project no longer exists — "
                "cannot route a reply to it"
            )

        # Routing is derived from the parent, never taken fresh from the
        # caller — a reply that disagreed with its own parent would be a
        # silent misaddress. If the caller passed routing args anyway,
        # they must agree with what's derived, or this raises.
        derived_to_id = parent["from_project_id"]
        derived_from_id = parent["to_project_id"]
        if to_project is not None:
            if await db.resolve_project_id(to_project) != derived_to_id:
                raise ValueError(
                    f"in_reply_to={in_reply_to} routes to project "
                    f"'{parent['from_slug']}', but to_project='{to_project}' "
                    "was also passed and disagrees — omit to_project on a "
                    "reply, it is derived from the parent"
                )
        if from_project is not None:
            if await db.resolve_project_id(from_project) != derived_from_id:
                raise ValueError(
                    f"in_reply_to={in_reply_to} routes from project "
                    f"'{parent['to_slug']}', but from_project='{from_project}' "
                    "was also passed and disagrees — omit from_project on a "
                    "reply, it is derived from the parent"
                )

        if parent["reply_depth"] + 1 > MAX_REPLY_DEPTH:
            raise ValueError(
                f"thread {parent['thread_id']} has hit the reply-depth cap "
                f"(MESSAGE_MAX_REPLY_DEPTH={MAX_REPLY_DEPTH}) — stop replying, "
                f"mark message {in_reply_to} read instead, and surface this "
                "thread to the user"
            )

        to_id, from_id = derived_to_id, derived_from_id
        resolved_kind = kind or "reply"
    else:
        if to_project is None or from_project is None:
            raise ValueError(
                "to_project and from_project are both required to start a "
                "new thread (in_reply_to is not set)"
            )
        to_id = await db.resolve_project_id(to_project)
        from_id = await db.resolve_project_id(from_project)
        resolved_kind = kind or "ask"

    if resolved_kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}")

    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO messages
                    (to_project_id, from_project_id, from_session,
                     kind, subject, body, in_reply_to)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, thread_id, reply_depth, kind, status, created_at
                """,
                to_id,
                from_id,
                from_session,
                resolved_kind,
                subject,
                body,
                in_reply_to,
            )
            if in_reply_to is not None:
                # Same transaction: the recipient's /start never shows a
                # question it has already answered.
                await conn.execute(
                    """
                    UPDATE messages
                       SET status = 'answered', read_at = COALESCE(read_at, now())
                     WHERE id = $1 AND status <> 'answered'
                    """,
                    in_reply_to,
                )

    to_row = await db.fetchrow("SELECT slug FROM projects WHERE id = $1", to_id)
    from_row = await db.fetchrow("SELECT slug FROM projects WHERE id = $1", from_id)

    result = dict(row)
    result["to_project"] = to_row["slug"] if to_row else None
    result["from_project"] = from_row["slug"] if from_row else None
    result["replies_left"] = MAX_REPLY_DEPTH - result["reply_depth"]
    return result


async def inbox(
    project: str,
    status: str = "unread",
    limit: int = 20,
    include_sent: bool = False,
) -> dict[str, Any]:
    project_id = await db.resolve_project_id(project)
    if status == "all":
        statuses = list(VALID_INBOX_STATUSES)
    elif status in VALID_INBOX_STATUSES:
        statuses = [status]
    else:
        raise ValueError(f"status must be one of {VALID_INBOX_STATUSES + ('all',)}")

    rows = await db.fetch(
        """
        SELECT m.id, m.thread_id, m.in_reply_to, m.reply_depth, m.kind,
               m.subject, m.body, m.status, m.created_at, m.read_at,
               m.from_session, fp.slug AS from_slug
        FROM messages m
        LEFT JOIN projects fp ON fp.id = m.from_project_id
        WHERE m.to_project_id = $1 AND m.status = ANY($2::text[])
        ORDER BY m.created_at DESC
        LIMIT $3
        """,
        project_id,
        statuses,
        limit,
    )
    messages = [_with_preview(r) for r in rows]

    awaiting_reply: list[dict[str, Any]] = []
    if include_sent:
        sent_rows = await db.fetch(
            """
            SELECT m.id, m.thread_id, m.in_reply_to, m.reply_depth, m.kind,
                   m.subject, m.body, m.status, m.created_at, m.read_at,
                   m.from_session, tp.slug AS to_slug
            FROM messages m
            JOIN projects tp ON tp.id = m.to_project_id
            WHERE m.from_project_id = $1 AND m.kind = 'ask' AND m.status != 'answered'
            ORDER BY m.created_at DESC
            LIMIT 20
            """,
            project_id,
        )
        awaiting_reply = [_with_preview(r) for r in sent_rows]

    return {"messages": messages, "awaiting_reply": awaiting_reply}


async def thread(message_id: int) -> dict[str, Any]:
    root = await db.fetchrow("SELECT thread_id FROM messages WHERE id = $1", message_id)
    if root is None:
        raise ValueError(f"No message id={message_id}")
    thread_id = root["thread_id"]

    rows = await db.fetch(
        """
        SELECT m.id, m.thread_id, m.in_reply_to, m.reply_depth, m.kind,
               m.subject, m.body, m.status, m.created_at, m.read_at,
               m.from_session, fp.slug AS from_slug, tp.slug AS to_slug
        FROM messages m
        JOIN projects tp ON tp.id = m.to_project_id
        LEFT JOIN projects fp ON fp.id = m.from_project_id
        WHERE m.thread_id = $1
        ORDER BY m.id
        """,
        thread_id,
    )
    messages = [dict(r) for r in rows]
    max_depth = max((m["reply_depth"] for m in messages), default=0)
    participants = sorted(
        {m["from_slug"] for m in messages if m["from_slug"]} | {m["to_slug"] for m in messages}
    )
    return {
        "thread_id": thread_id,
        "messages": messages,
        "max_depth": max_depth,
        "replies_left": MAX_REPLY_DEPTH - max_depth,
        "participants": participants,
    }


async def mark(message_id: int, status: str = "read") -> dict[str, Any]:
    if status not in VALID_MARK_STATUSES:
        raise ValueError(f"status must be one of {VALID_MARK_STATUSES}")

    # Conditional UPDATE is the concurrency primitive: if two sessions of
    # the same project are both live (both listening on the same channel),
    # exactly one of them claims the message.
    row = await db.fetchrow(
        """
        UPDATE messages
           SET status = $2, read_at = COALESCE(read_at, now())
         WHERE id = $1 AND status = 'unread'
        RETURNING id, thread_id, status, read_at
        """,
        message_id,
        status,
    )
    if row is not None:
        result = dict(row)
        result["claimed"] = True
        return result

    existing = await db.fetchrow(
        "SELECT id, thread_id, status, read_at FROM messages WHERE id = $1",
        message_id,
    )
    if existing is None:
        raise ValueError(f"No message id={message_id}")
    result = dict(existing)
    result["claimed"] = False
    return result
