"""Manual smoke test for phase 5 (memory_import migration).

Imports server/tests/fixtures/legacy_memory_bank/ (a synthetic legacy
memory-bank tree matching the pre-Postgres file format) and checks that
node counts, kinds, task grading, and dependency edges came across
correctly. Run with:

    EMBED_PROVIDER=mock DATABASE_URL=postgresql://memory:memory@localhost:5433/memory_bank \
        python tests/manual_phase5.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory_mcp import db
from memory_mcp.server import memory_get, memory_import, project_create

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "legacy_memory_bank"


async def main() -> None:
    project = await project_create(slug="stayhug-legacy-import-test", name="StayHug (legacy import test)")

    summary = await memory_import(project="stayhug-legacy-import-test", path=str(FIXTURE_ROOT))
    print("import summary:")
    for item in summary["created"]:
        print(" -", item["kind"], "|", item["title"], "| id", item["id"])
    print("edges:", summary["edges"])
    print("warnings:", summary["warnings"])

    by_kind: dict[str, list[dict]] = {}
    for item in summary["created"]:
        by_kind.setdefault(item["kind"], []).append(item)

    # --- brief / pattern from single-section files ---
    assert len(by_kind.get("brief", [])) == 2, "projectbrief.md has 2 '##' sections"
    assert len(by_kind.get("pattern", [])) == 1

    # --- active: one node, Tasks table excluded from its body ---
    assert len(by_kind.get("active", [])) == 1
    active_id = by_kind["active"][0]["id"]
    active_node = await memory_get(node_id=active_id)
    assert "Waiting on OAuth provider sandbox credentials" in active_node["body"]
    assert "| # | Task |" not in active_node["body"], "Tasks table should not leak into active body"
    print("PASS: active node body excludes Tasks table")

    # --- tasks: 4 rows, graded correctly ---
    tasks = by_kind.get("task", [])
    assert len(tasks) == 4, f"expected 4 task nodes, got {len(tasks)}"
    oauth_task = next(t for t in tasks if t["title"] == "Add OAuth login")
    oauth_node = await memory_get(node_id=oauth_task["id"])
    assert oauth_node["priority"] == 9
    assert oauth_node["importance"] == 5
    assert oauth_node["topic"] == ["auth"]
    print("PASS: task grading (priority/importance/topic) round-tripped")

    # --- edges: blocked by / w/ / epic w/ all inferred ---
    rels = {(e["rel"]) for e in summary["edges"]}
    assert "depends_on" in rels, "expected a depends_on edge from 'blocked by #1'"
    assert "relates_to" in rels, "expected relates_to edges from 'w/ #4' and 'epic w/ #3'"
    assert len(summary["edges"]) == 3, f"expected 3 edges total, got {len(summary['edges'])}"
    print("PASS: dependency edges inferred from Depends/Related column")

    # --- progress: split by date ---
    progress = by_kind.get("progress", [])
    assert len(progress) == 2
    assert any("2026-07-10" in p["title"] for p in progress)
    assert any("2026-07-14" in p["title"] for p in progress)
    print("PASS: progress.md split into dated nodes")

    # --- devenv: split by category, topic-tagged ---
    devenv = by_kind.get("devenv", [])
    assert len(devenv) == 3
    gotcha = next(d for d in devenv if d["title"] == "Known Gotchas")
    gotcha_node = await memory_get(node_id=gotcha["id"])
    assert "gotchas" in gotcha_node["topic"] or "known-gotchas" in gotcha_node["topic"]
    print("PASS: devenv.md split into topic-tagged nodes:", [d["title"] for d in devenv])

    # --- plans/*.md ---
    plans = by_kind.get("plan", [])
    assert len(plans) == 1
    assert plans[0]["title"] == "OAuth Login Plan"
    print("PASS: plans/*.md imported")

    # --- taskDependencies.md: reference nodes only, warning surfaced ---
    decisions = by_kind.get("decision", [])
    assert len(decisions) == 1
    assert any("taskDependencies.md" in w for w in summary["warnings"])
    print("PASS: taskDependencies.md imported as reference node with warning")

    print("\nALL PHASE 5 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
