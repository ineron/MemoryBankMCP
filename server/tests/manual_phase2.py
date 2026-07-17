"""Manual smoke test for phase 2 (core write tools).

Not a pytest suite — a quick script to exercise the MCP tool functions
directly (bypassing the MCP transport) against a live DB, using the mock
embedding provider so no API key is required. Run with:

    EMBED_PROVIDER=mock DATABASE_URL=postgresql://memory:memory@localhost:5433/memory_bank \
        python tests/manual_phase2.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory_mcp import db
from memory_mcp.server import (
    memory_get,
    memory_link,
    memory_upsert,
    project_create,
    project_group_create,
    project_list,
)


async def main() -> None:
    group = await project_group_create(slug="test-ledgyx", name="Ledgyx product family")
    print("group:", group)

    core = await project_create(slug="test-ledgyx-core", name="Ledgyx Core", group_slug="test-ledgyx")
    landing = await project_create(slug="test-ledgyx-landing", name="Ledgyx Landing", group_slug="test-ledgyx")
    print("projects:", core, landing)

    projects = await project_list(group="test-ledgyx")
    print("project_list(group=test-ledgyx):", projects)
    assert len(projects) == 2

    node = await memory_upsert(
        project="test-ledgyx-core",
        kind="decision",
        title="Use Postgres for ledger storage",
        body="Chose Postgres over a document store for ACID guarantees on ledger entries.",
        topic=["storage", "architecture"],
    )
    print("created node:", node)
    assert node["project_id"] == core["id"]

    fetched = await memory_get(node_id=node["id"])
    print("fetched node:", {k: v for k, v in fetched.items() if k != "body"})
    assert fetched["title"] == "Use Postgres for ledger storage"
    assert fetched["topic"] == ["storage", "architecture"]

    # Verify the embedding round-tripped with the correct dimension.
    row = await db.fetchrow("SELECT embedding FROM nodes WHERE id = $1", node["id"])
    assert row["embedding"] is not None
    emb = row["embedding"].to_list() if hasattr(row["embedding"], "to_list") else list(row["embedding"])
    assert len(emb) == 1024, f"expected 1024 dims, got {len(emb)}"
    print("embedding dim OK:", len(emb))

    # Update in place (id passed).
    updated = await memory_upsert(
        project="test-ledgyx-core",
        kind="decision",
        title="Use Postgres for ledger storage",
        body="Updated rationale: ACID + pgvector reuse for the memory bank itself.",
        topic=["storage", "architecture"],
        id=node["id"],
    )
    assert updated["id"] == node["id"]
    print("updated node:", updated)

    # Cross-project write: file a bug from "landing" session into "core".
    filed = await memory_upsert(
        project="test-ledgyx-core",
        kind="task",
        title="Broken CTA link found while testing landing page",
        body="The 'Get started' button 404s; likely a core routing issue.",
        topic=["bug", "routing"],
        priority=7,
        importance=3,
        filed_from_project="test-ledgyx-landing",
    )
    print("cross-project filed task:", filed)
    assert filed["status"] == "inbox", f"expected inbox status, got {filed['status']}"

    filed_row = await db.fetchrow(
        "SELECT filed_from_project_id, status FROM nodes WHERE id = $1", filed["id"]
    )
    assert filed_row["filed_from_project_id"] == landing["id"]
    assert filed_row["status"] == "inbox"
    print("cross-project provenance OK")

    # memory_link + memory_get with hops.
    link = await memory_link(src_id=filed["id"], dst_id=node["id"], rel="cross_ref")
    print("link:", link)

    with_neighbors = await memory_get(node_id=filed["id"], hops=1)
    print("neighbors:", with_neighbors["neighbors"])
    assert any(n["id"] == node["id"] for n in with_neighbors["neighbors"])

    print("\nALL PHASE 2 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
