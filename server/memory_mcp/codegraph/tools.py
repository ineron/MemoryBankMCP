"""MCP tool wrappers for the codegraph plugin — thin delegation to
service.py, mirroring how server.py's own tools delegate to
messaging.py/retrieval.py. register(mcp) is called from server.py only
after confirming the optional `codegraph` extra actually imports, so these
tools simply don't exist on an install that never opted in.
"""

from __future__ import annotations

from typing import Any, Optional

from . import service


def register(mcp) -> None:
    @mcp.tool()
    async def codegraph_build(project: str, root: str) -> dict[str, Any]:
        """(Re)build the function/class/call map for `project`'s source tree
        at `root` (an absolute path). Incremental: a file is only reparsed
        if its content changed since the last build; files deleted from disk
        since the last build are dropped from the graph. Respects `root`'s
        own .gitignore (plus the enclosing git repo's top-level one, if
        different) in addition to a small built-in ignore list, so
        installed dependencies/vendored code/build output stay out of the
        graph regardless of what ecosystem or package manager the project
        uses — without this, entity count grows unbounded with no signal,
        indexing every installed package alongside the project's own code.
        Requires the `codegraph` Postgres schema to already exist — run
        `python -m memory_mcp.codegraph.init_schema` once per database
        first. Call this before codegraph_map/deps/issues/search, and again
        whenever the source tree has changed meaningfully since the last
        build (this tool does not watch the filesystem)."""
        return await service.build_project_graph(project, root)

    @mcp.tool()
    async def codegraph_map(project: str, prefix: Optional[str] = None) -> dict[str, Any]:
        """Hierarchical module → class → function/method tree for `project`,
        as last built by codegraph_build. `prefix` (a dotted full_path, e.g.
        "pkg.module" or "pkg.module.ClassName") restricts the result to that
        subtree — omit it for the whole project, which can be large."""
        return await service.get_map(project, prefix=prefix)

    @mcp.tool()
    async def codegraph_deps(project: str, entity_path: str) -> dict[str, Any]:
        """calls / called_by / imports / imported_by for one entity.
        `entity_path` accepts either the full dotted path (e.g.
        "pkg.module.ClassName.method") or, if that doesn't match, a shorter
        suffix as long as it's unambiguous (e.g. "ClassName.method") — an
        ambiguous suffix returns the candidate full paths instead of
        guessing. Call resolution is best-effort name matching, not real
        symbol resolution, so `resolved: false` entries in `calls`/`imports`
        are real call sites whose target couldn't be pinned to exactly one
        entity — not necessarily dead ends."""
        return await service.get_deps(project, entity_path)

    @mcp.tool()
    async def codegraph_search(project: str, query: str, limit: int = 20) -> dict[str, Any]:
        """Substring search over entity full_paths (module/class/function/
        method) in `project`'s last-built codegraph. Use this to find an
        entity_path to pass to codegraph_deps when you don't know the exact
        dotted path."""
        return await service.search(project, query, limit=limit)

    @mcp.tool()
    async def codegraph_issues(project: str) -> dict[str, Any]:
        """Static-analysis findings over `project`'s last-built codegraph:
        duplicate_names (same function/method name defined more than once —
        a common source of picking the wrong one when call resolution is
        name-based), unused_entities (public functions/classes/methods with
        zero resolved incoming calls, excluding test_* by name and anything
        @decorator-wrapped — a heuristic, not proof of dead code: entry
        points, exported APIs, and unresolved call sites all look the same
        as truly unused; decorator exclusion cuts most but not all of that
        noise — Flask/FastAPI routes etc. are excluded, but a call reached
        only through a module-level singleton instance variable can still
        misfire as unused if that method's name happens to be unambiguous
        project-wide), cycles (a real multi-function dependency cycle among
        calls + imports — via a full strongly-connected-components pass so
        every cycle is reported, not just the first one found from an
        arbitrary start node), and self_recursive (a function directly
        calling itself — normal for a recursive helper, reported separately
        from `cycles` so one doesn't read as the other)."""
        return await service.get_issues(project)
