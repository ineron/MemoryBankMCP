"""Manual smoke test for phase 3 (retrieval: search, tasks, active, mark).

Uses EMBED_PROVIDER=mock, which is deterministic (identical text -> identical
vector, different text -> ~orthogonal vector). That's enough to validate the
threshold/exclusion/graph-expansion/verdict *mechanics* without needing real
semantic embeddings. Run with:

    EMBED_PROVIDER=mock DATABASE_URL=postgresql://memory:memory@localhost:5433/memory_bank \
        python tests/manual_phase3.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory_mcp import db
from memory_mcp.server import (
    memory_active,
    memory_archive,
    memory_get,
    memory_link,
    memory_mark,
    memory_search,
    memory_tasks,
    memory_upsert,
    project_create,
    project_group_create,
)

KAFKA_QUERY = "Kafka consumer retry backoff configuration for ledgyx-core"


async def main() -> None:
    await project_group_create(slug="test-ledgyx", name="Ledgyx product family")
    core = await project_create(slug="test-ledgyx-core", name="Ledgyx Core", group_slug="test-ledgyx")
    landing = await project_create(slug="test-ledgyx-landing", name="Ledgyx Landing", group_slug="test-ledgyx")

    # Seed node whose embeddable text is EXACTLY the query -> mock similarity 1.0
    kafka_node = await memory_upsert(
        project="test-ledgyx-core",
        kind="pattern",
        title=KAFKA_QUERY,
        body="",
        topic=["kafka", "reliability"],
    )

    # A related node, linked via edge (should surface via graph expansion
    # even though its text has nothing in common with the query).
    related_node = await memory_upsert(
        project="test-ledgyx-core",
        kind="decision",
        title="Dead-letter queue naming convention",
        body="ledgyx.<topic>.dlq — chosen for consistency across consumers.",
        topic=["kafka"],
    )
    await memory_link(src_id=kafka_node["id"], dst_id=related_node["id"], rel="relates_to")

    # A clearly off-topic node that should NOT show up (below threshold, no edge).
    offtopic_node = await memory_upsert(
        project="test-ledgyx-core",
        kind="product",
        title="Hero section copy variant B for the pricing page",
        body="Emphasize annual discount over monthly plan in the headline.",
        topic=["landing", "copy"],
    )

    # --- Test 1: threshold isolation + graph expansion ---
    results = await memory_search(project="test-ledgyx-core", query=KAFKA_QUERY, hops=1)
    result_ids = {r["id"] for r in results}
    print("search results:", [(r["id"], r["title"], r["match"], r.get("similarity")) for r in results])

    assert kafka_node["id"] in result_ids, "exact-text seed should match with similarity 1.0"
    assert related_node["id"] in result_ids, "edge-linked neighbor should surface via graph expansion"
    assert offtopic_node["id"] not in result_ids, "unrelated node should be filtered out by threshold"
    print("PASS: threshold isolation + graph expansion")

    # --- Test 2: memory_mark drops a node from future identical searches ---
    await memory_mark(query=KAFKA_QUERY, node_id=kafka_node["id"], verdict="irrelevant")
    results_after_mark = await memory_search(project="test-ledgyx-core", query=KAFKA_QUERY, hops=1)
    ids_after_mark = {r["id"] for r in results_after_mark}
    assert kafka_node["id"] not in ids_after_mark, "node marked irrelevant for this exact query should be dropped"
    print("PASS: memory_mark exclusion on re-query")

    # --- Test 3: memory_tasks splits own-project tasks vs cross-project inbox ---
    own_task = await memory_upsert(
        project="test-ledgyx-core",
        kind="task",
        title="Add idempotency key to payment webhook handler",
        priority=8,
        importance=5,
        topic=["payments"],
    )
    filed_task = await memory_upsert(
        project="test-ledgyx-core",
        kind="task",
        title="Landing page CTA 404s — likely core routing issue",
        priority=6,
        importance=3,
        topic=["bug"],
        filed_from_project="test-ledgyx-landing",
    )
    task_view = await memory_tasks(project="test-ledgyx-core")
    print("memory_tasks:", task_view)
    own_ids = {t["id"] for t in task_view["tasks"]}
    inbox_ids = {t["id"] for t in task_view["inbox"]}
    assert own_task["id"] in own_ids
    assert filed_task["id"] in inbox_ids
    assert task_view["inbox"][0]["filed_from_slug"] == "test-ledgyx-landing"
    print("PASS: memory_tasks own/inbox split")

    # --- Test 4: memory_active returns the latest active-kind node ---
    assert await memory_active(project="test-ledgyx-core") is None, "no active node yet"
    active_node = await memory_upsert(
        project="test-ledgyx-core",
        kind="active",
        title="Current focus",
        body="Wiring the Kafka retry path; next step is the DLQ naming decision.",
    )
    active_view = await memory_active(project="test-ledgyx-core")
    assert active_view["id"] == active_node["id"]
    print("PASS: memory_active")

    # --- Test 5: memory_archive removes a node from default search/tasks ---
    await memory_archive(node_id=related_node["id"])
    archived_check = await memory_get(node_id=related_node["id"])
    assert archived_check["status"] == "archived"
    results_post_archive = await memory_search(project="test-ledgyx-core", query=KAFKA_QUERY, hops=1)
    assert related_node["id"] not in {r["id"] for r in results_post_archive}
    print("PASS: memory_archive excludes from default search")

    # --- Test 6: scope="group" reaches into a sibling project ---
    landing_node = await memory_upsert(
        project="test-ledgyx-landing",
        kind="product",
        title="Only-in-landing marker node for scope test",
        body="",
        topic=["scope-test"],
    )
    project_scope = await memory_search(
        project="test-ledgyx-core", query="Only-in-landing marker node for scope test", hops=0
    )
    group_scope = await memory_search(
        project="test-ledgyx-core", query="Only-in-landing marker node for scope test", hops=0, scope="group"
    )
    assert landing_node["id"] not in {r["id"] for r in project_scope}, "project-scope must not reach landing"
    assert landing_node["id"] in {r["id"] for r in group_scope}, "group-scope must reach landing"
    print("PASS: scope=project vs scope=group")

    print("\nALL PHASE 3 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
