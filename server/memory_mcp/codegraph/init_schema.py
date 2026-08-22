"""Standalone initializer for the codegraph plugin's Postgres schema.

Deliberately separate from the core server's schema.sql / migrations flow
(see server/README.md's Setup section for that one) — this plugin is meant
to be added or removed from an existing memory-bank install independently,
so it gets its own init entry point rather than being folded into the
core's schema history.

Usage (from server/, with the `codegraph` extra installed):

    source .venv/bin/activate
    set -a; source .env; set +a
    python -m memory_mcp.codegraph.init_schema

Safe to re-run — schema.sql is idempotent (CREATE SCHEMA/TABLE/INDEX IF NOT
EXISTS). To remove the plugin entirely: `psql "$DATABASE_URL" -c
"DROP SCHEMA codegraph CASCADE;"`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from .. import db

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


async def init_schema() -> None:
    sql = _SCHEMA_PATH.read_text()
    async with db.acquire() as conn:
        await conn.execute(sql)
    print(f"codegraph schema applied ({_SCHEMA_PATH}).")


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
    asyncio.run(init_schema())


if __name__ == "__main__":
    main()
