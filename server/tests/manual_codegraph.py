"""Manual smoke test for the codegraph plugin: incremental build (add /
unchanged / modify / remove), cross-file call resolution, the
re-resolution pass that heals stale target_entity_id rows after a
neighboring file gets rebuilt, and the issues report (duplicates / unused /
cycles).

Requires the `codegraph` extra installed and the codegraph schema already
applied once against the target database:

    .venv/bin/pip install -e '.[codegraph]'
    .venv/bin/python -m memory_mcp.codegraph.init_schema

Run with:

    EMBED_PROVIDER=mock python tests/manual_codegraph.py

EMBED_PROVIDER=mock is irrelevant here (codegraph never computes
embeddings) but kept for invocation consistency with the other
manual_*.py scripts.
"""

import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory_mcp import db
from memory_mcp.codegraph import service
from memory_mcp.server import project_create

PROJECT = "test-codegraph-manual"


async def cleanup() -> None:
    row = await db.fetchrow("SELECT id FROM projects WHERE slug = $1", PROJECT)
    if row:
        await db.execute("DELETE FROM projects WHERE id = $1", row["id"])


def write(root: str, relpath: str, content: str) -> None:
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


async def main() -> None:
    await cleanup()
    await project_create(slug=PROJECT, name="Codegraph manual test")

    tmp = tempfile.mkdtemp(prefix="codegraph-manual-")
    try:
        write(tmp, "pkg/__init__.py", "")
        write(tmp, "pkg/a.py", "from pkg.b import helper\n\ndef use_helper():\n    helper()\n")
        write(tmp, "pkg/b.py", "def helper():\n    pass\n\ndef duplicate_name():\n    pass\n")
        write(tmp, "pkg/c.py", "def duplicate_name():\n    pass\n\ndef unused_fn():\n    pass\n")

        # --- Test 1: initial build finds everything, resolves the cross-file call ---
        r1 = await service.build_project_graph(PROJECT, tmp)
        assert r1["files_changed"] == 4 and r1["files_removed"] == 0
        deps = await service.get_deps(PROJECT, "pkg.a.use_helper")
        assert deps["calls"] == [{"target_name": "helper", "resolved": True, "line_number": 4}]
        print("PASS: initial build + cross-file call resolution")

        # --- Test 2: re-running with no changes reparses nothing ---
        r2 = await service.build_project_graph(PROJECT, tmp)
        assert r2["files_changed"] == 0 and r2["files_unchanged"] == 4
        print("PASS: unchanged files are not reparsed")

        # --- Test 3: modifying only b.py must not stale-out a.py's resolved
        # call to helper() — the re-resolution pass has to re-attach it to
        # helper's freshly reinserted entity id ---
        write(tmp, "pkg/b.py", "def helper():\n    return 1\n\ndef duplicate_name():\n    pass\n")
        r3 = await service.build_project_graph(PROJECT, tmp)
        assert r3["files_changed"] == 1
        deps3 = await service.get_deps(PROJECT, "pkg.a.use_helper")
        assert deps3["calls"][0]["resolved"] is True, "cross-file call went stale after neighbor rebuild"
        print("PASS: cross-file call survives a neighboring file's rebuild (re-resolution pass)")

        # --- Test 4: issues report ---
        issues = await service.get_issues(PROJECT)
        dup_names = {d["name"] for d in issues["duplicate_names"]}
        assert "duplicate_name" in dup_names
        unused_paths = {u["full_path"] for u in issues["unused_entities"]}
        assert "pkg.c.unused_fn" in unused_paths
        assert "pkg.b.helper" not in unused_paths  # called from pkg.a
        print("PASS: codegraph_issues finds the duplicate name and the unused function")

        # --- Test 4b: decorated functions excluded from unused_entities;
        # self-recursion and real multi-node cycles reported separately ---
        write(
            tmp, "pkg/d.py",
            "def route_decorator(f):\n"
            "    return f\n\n"
            "@route_decorator\n"
            "def registered_handler():\n"
            "    pass\n\n"
            "def recurse_helper(n):\n"
            "    if n <= 0:\n"
            "        return 0\n"
            "    return recurse_helper(n - 1)\n\n"
            "def cycle_a():\n"
            "    return cycle_b()\n\n"
            "def cycle_b():\n"
            "    return cycle_a()\n",
        )
        await service.build_project_graph(PROJECT, tmp)
        issues4b = await service.get_issues(PROJECT)
        unused4b = {u["full_path"] for u in issues4b["unused_entities"]}
        assert "pkg.d.registered_handler" not in unused4b, "decorated function should be excluded from unused"
        assert "pkg.d.recurse_helper" in issues4b["self_recursive"]
        assert any(set(c) == {"pkg.d.cycle_a", "pkg.d.cycle_b"} for c in issues4b["cycles"])
        assert not any("pkg.d.recurse_helper" in c for c in issues4b["cycles"]), "self-loop leaked into cycles"
        print("PASS: decorator exclusion + self_recursive/cycles split")

        # --- Test 5: removing a file drops its entities and un-resolves
        # dependents' calls into it, without deleting the dependent row ---
        os.remove(os.path.join(tmp, "pkg", "b.py"))
        r5 = await service.build_project_graph(PROJECT, tmp)
        assert r5["files_removed"] == 1
        deps5 = await service.get_deps(PROJECT, "pkg.a.use_helper")
        assert deps5["calls"][0]["resolved"] is False
        assert deps5["calls"][0]["target_name"] == "helper"
        m = await service.get_map(PROJECT)
        assert "pkg.b" not in {n["full_path"] for n in m["tree"]}
        print("PASS: file removal cascades entities and un-resolves (not deletes) dependents' call rows")

        # --- Test 6: .gitignore keeps vendored/installed code out of the
        # graph entirely — the actual fix for unbounded entity growth on
        # projects with no hardcoded-name match (e.g. a vendor directory
        # under an arbitrary name), not just the built-in ignore-dir list ---
        write(tmp, ".gitignore", "vendor_stuff/\n")
        write(tmp, "vendor_stuff/thirdparty.py", "def vendored_fn():\n    pass\n")
        r6 = await service.build_project_graph(PROJECT, tmp)
        m6 = await service.get_map(PROJECT)
        assert "vendor_stuff.thirdparty" not in {n["full_path"] for n in m6["tree"]}
        print("PASS: .gitignore-excluded directory is not scanned")

        print("\nALL CODEGRAPH CHECKS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        await cleanup()


if __name__ == "__main__":
    asyncio.run(main())
