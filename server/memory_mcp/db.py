"""Connection pool and low-level query helpers for the Memory Bank MCP server.

Every tool in server.py goes through here rather than opening its own
connection, so pool lifecycle is centralized and tests can swap the pool
easily.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg
from pgvector.asyncpg import register_vector

_pool: asyncpg.Pool | None = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy server/.env.example to server/.env "
            "and fill it in, or export DATABASE_URL directly."
        )
    return url


async def _init_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=database_url(),
            min_size=1,
            max_size=10,
            init=_init_connection,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    async with acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    async with acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    async with acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    async with acquire() as conn:
        return await conn.execute(query, *args)


# ---------------------------------------------------------------------
# Project resolution helpers — used by every tool to turn a user-supplied
# slug into an id, and to resolve group scope for cross-project search.
# ---------------------------------------------------------------------


async def resolve_project_id(slug: str) -> int:
    row = await fetchrow("SELECT id FROM projects WHERE slug = $1", slug)
    if row is None:
        raise ValueError(
            f"Unknown project slug '{slug}'. Use project_list() to see "
            "registered projects, or create one first."
        )
    return row["id"]


async def project_group_id(project_id: int) -> int | None:
    row = await fetchrow("SELECT group_id FROM projects WHERE id = $1", project_id)
    return row["group_id"] if row else None


async def next_task_seq(project_id: int) -> int:
    """Atomically assign the next stable, per-project task display number.
    Unlike `nodes.id` (a single sequence shared by every project — jumps
    unpredictably whenever *other* projects insert nodes), this counter is
    scoped to one project and only ever increments, so a task's number is
    permanent even after the task is archived."""
    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO project_task_counters (project_id, next_seq)
                VALUES ($1, 1)
                ON CONFLICT (project_id) DO NOTHING
                """,
                project_id,
            )
            row = await conn.fetchrow(
                """
                UPDATE project_task_counters
                SET next_seq = next_seq + 1
                WHERE project_id = $1
                RETURNING next_seq - 1 AS seq
                """,
                project_id,
            )
            return row["seq"]


async def project_ids_in_group(group_id: int) -> list[int]:
    rows = await fetch("SELECT id FROM projects WHERE group_id = $1", group_id)
    return [r["id"] for r in rows]
