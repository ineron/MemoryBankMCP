"""Memory Bank MCP server.

Exposes the Postgres-backed memory (vector + graph) as MCP tools. Run with:

    python -m memory_mcp.server

Tool groups (see plan doc for full rationale):
  - project/group bootstrap: project_create, project_group_create, project_list
  - core writes: memory_upsert, memory_link, memory_get
  - retrieval (phase 3): memory_search, memory_tasks, memory_active, memory_mark
  - lifecycle (phase 3): memory_archive
  - migration (phase 5): memory_import
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from . import db, retrieval
from .embeddings import embed_one
from .importer import import_markdown_tree

# Load server/.env by absolute path (not cwd-relative) — an MCP client
# launching this via stdio may set the process cwd to the *project* root
# rather than server/, so auto-discovery from cwd can't be relied on.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

mcp = FastMCP("memory-bank")


# ---------------------------------------------------------------------
# Project / group bootstrap
# ---------------------------------------------------------------------


@mcp.tool()
async def project_group_create(slug: str, name: str) -> dict[str, Any]:
    """Create a group of logically related projects (e.g. 'ledgyx' grouping
    core + landing + kafka + pg-libs) so they can be searched together with
    memory_search(scope="group")."""
    row = await db.fetchrow(
        """
        INSERT INTO project_groups (slug, name) VALUES ($1, $2)
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
        RETURNING id, slug, name
        """,
        slug,
        name,
    )
    return dict(row)


@mcp.tool()
async def project_create(slug: str, name: str, group_slug: Optional[str] = None) -> dict[str, Any]:
    """Register a project. group_slug (optional) links it into a project
    group for cross-project group-scoped search."""
    group_id = None
    if group_slug is not None:
        group_row = await db.fetchrow("SELECT id FROM project_groups WHERE slug = $1", group_slug)
        if group_row is None:
            raise ValueError(f"Unknown project group '{group_slug}' — create it with project_group_create first")
        group_id = group_row["id"]

    row = await db.fetchrow(
        """
        INSERT INTO projects (slug, name, group_id) VALUES ($1, $2, $3)
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, group_id = EXCLUDED.group_id
        RETURNING id, slug, name, group_id
        """,
        slug,
        name,
        group_id,
    )
    return dict(row)


@mcp.tool()
async def project_list(group: Optional[str] = None) -> list[dict[str, Any]]:
    """List registered projects, optionally filtered to one group. Use this
    to discover valid project slugs before filing a cross-project write."""
    if group is not None:
        rows = await db.fetch(
            """
            SELECT p.id, p.slug, p.name, g.slug AS group_slug
            FROM projects p JOIN project_groups g ON g.id = p.group_id
            WHERE g.slug = $1
            ORDER BY p.slug
            """,
            group,
        )
    else:
        rows = await db.fetch(
            """
            SELECT p.id, p.slug, p.name, g.slug AS group_slug
            FROM projects p LEFT JOIN project_groups g ON g.id = p.group_id
            ORDER BY p.slug
            """
        )
    return [dict(r) for r in rows]


@mcp.tool()
async def project_overview(group: Optional[str] = None) -> list[dict[str, Any]]:
    """Dashboard-style summary of every registered project (or one group):
    node counts by kind, active/inbox task counts, and when the project's
    active-context node was last updated. Use this to see everything
    connected to the memory bank at a glance, e.g. before deciding what to
    work on next or whether a project has gone stale."""
    projects = await project_list(group=group)
    if not projects:
        return []
    project_ids = [p["id"] for p in projects]

    kind_rows = await db.fetch(
        """
        SELECT project_id, kind, status, count(*) AS cnt
        FROM nodes
        WHERE project_id = ANY($1::bigint[])
        GROUP BY project_id, kind, status
        """,
        project_ids,
    )
    active_rows = await db.fetch(
        """
        SELECT DISTINCT ON (project_id) project_id, updated_at
        FROM nodes
        WHERE project_id = ANY($1::bigint[]) AND kind = 'active'
        ORDER BY project_id, updated_at DESC
        """,
        project_ids,
    )
    last_active_by_project = {r["project_id"]: r["updated_at"] for r in active_rows}

    by_project: dict[int, dict[str, Any]] = {
        p["id"]: {"node_counts": {}, "tasks_active": 0, "tasks_inbox": 0, "total_nodes": 0} for p in projects
    }
    for r in kind_rows:
        entry = by_project[r["project_id"]]
        if r["status"] != "archived":
            entry["total_nodes"] += r["cnt"]
            entry["node_counts"][r["kind"]] = entry["node_counts"].get(r["kind"], 0) + r["cnt"]
        if r["kind"] == "task" and r["status"] == "active":
            entry["tasks_active"] += r["cnt"]
        elif r["kind"] == "task" and r["status"] == "inbox":
            entry["tasks_inbox"] += r["cnt"]

    result = []
    for p in projects:
        entry = by_project[p["id"]]
        result.append(
            {
                "slug": p["slug"],
                "name": p["name"],
                "group_slug": p["group_slug"],
                "total_nodes": entry["total_nodes"],
                "node_counts": entry["node_counts"],
                "tasks_active": entry["tasks_active"],
                "tasks_inbox": entry["tasks_inbox"],
                "last_active_updated": last_active_by_project.get(p["id"]),
            }
        )
    return result


# ---------------------------------------------------------------------
# Core writes
# ---------------------------------------------------------------------


def _embeddable_text(title: str, body: str) -> str:
    return f"{title}\n\n{body}" if body else title


@mcp.tool()
async def memory_upsert(
    project: str,
    kind: str,
    title: str,
    body: str = "",
    topic: Optional[list[str]] = None,
    priority: Optional[int] = None,
    importance: Optional[int] = None,
    depends_note: Optional[str] = None,
    id: Optional[int] = None,
    filed_from_project: Optional[str] = None,
) -> dict[str, Any]:
    """Create or update a memory node in `project`.

    Pass `filed_from_project` when writing into a DIFFERENT project than the
    one the current session is working on (cross-project filing) — this
    records provenance and, for kind="task", automatically lands the node
    with status="inbox" so the target project's /start surfaces it under
    "Filed from other sessions" instead of silently merging it into the
    backlog. Omit it for ordinary same-project writes.

    New kind="task" nodes get a `task_seq` assigned automatically — a
    stable, per-project display number (never reused, never shifted by
    archiving), returned in the result. Use THIS number when talking to the
    user about "task #N", not the raw `id` — `id` is a single sequence
    shared by every project in the DB, so it jumps unpredictably whenever
    other projects insert nodes.
    """
    project_id = await db.resolve_project_id(project)
    filed_from_id = None
    status = "active"
    if filed_from_project is not None:
        filed_from_id = await db.resolve_project_id(filed_from_project)
        if filed_from_id != project_id and kind == "task":
            status = "inbox"

    topic = topic or []
    vector = await embed_one(_embeddable_text(title, body))

    if id is not None:
        row = await db.fetchrow(
            """
            UPDATE nodes SET
                kind = $2, title = $3, body = $4, topic = $5,
                priority = $6, importance = $7, depends_note = $8,
                embedding = $9,
                filed_from_project_id = COALESCE($10, filed_from_project_id)
            WHERE id = $1 AND project_id = $11
            RETURNING id, project_id, kind, title, status
            """,
            id,
            kind,
            title,
            body,
            topic,
            priority,
            importance,
            depends_note,
            vector,
            filed_from_id,
            project_id,
        )
        if row is None:
            raise ValueError(f"No node id={id} found in project '{project}'")
        return dict(row)

    task_seq = await db.next_task_seq(project_id) if kind == "task" else None

    row = await db.fetchrow(
        """
        INSERT INTO nodes
            (project_id, filed_from_project_id, kind, title, body, topic,
             status, priority, importance, depends_note, embedding, task_seq)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        RETURNING id, project_id, kind, title, status, task_seq
        """,
        project_id,
        filed_from_id,
        kind,
        title,
        body,
        topic,
        status,
        priority,
        importance,
        depends_note,
        vector,
        task_seq,
    )
    return dict(row)


@mcp.tool()
async def memory_link(src_id: int, dst_id: int, rel: str, weight: float = 1.0) -> dict[str, Any]:
    """Create a typed graph edge between two nodes. `rel` may cross project
    boundaries (use rel="cross_ref" to tie a filed note back to its origin
    context in another project)."""
    row = await db.fetchrow(
        """
        INSERT INTO edges (src_id, dst_id, rel, weight)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (src_id, dst_id, rel) DO UPDATE SET weight = EXCLUDED.weight
        RETURNING id, src_id, dst_id, rel, weight
        """,
        src_id,
        dst_id,
        rel,
        weight,
    )
    return dict(row)


@mcp.tool()
async def memory_get(node_id: int, hops: int = 0) -> dict[str, Any]:
    """Fetch a single node by id, optionally including its graph neighbors
    up to `hops` steps away (0 = node only)."""
    node = await db.fetchrow(
        """
        SELECT n.id, n.project_id, p.slug AS project_slug, n.filed_from_project_id,
               n.kind, n.title, n.body, n.topic, n.status,
               n.priority, n.importance, n.depends_note,
               n.created_at, n.updated_at
        FROM nodes n JOIN projects p ON p.id = n.project_id
        WHERE n.id = $1
        """,
        node_id,
    )
    if node is None:
        raise ValueError(f"No node with id={node_id}")

    result = dict(node)
    if hops > 0:
        neighbor_rows = await db.fetch(
            """
            WITH RECURSIVE expand(id, depth) AS (
                SELECT $1::bigint, 0
                UNION
                SELECT CASE WHEN e.src_id = ex.id THEN e.dst_id ELSE e.src_id END, ex.depth + 1
                FROM edges e
                JOIN expand ex ON e.src_id = ex.id OR e.dst_id = ex.id
                WHERE ex.depth < $2
            )
            SELECT DISTINCT n.id, n.project_id, p.slug AS project_slug, n.kind, n.title, n.status
            FROM expand ex
            JOIN nodes n ON n.id = ex.id
            JOIN projects p ON p.id = n.project_id
            WHERE ex.id != $1
            """,
            node_id,
            hops,
        )
        result["neighbors"] = [dict(r) for r in neighbor_rows]
    return result


# ---------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------


@mcp.tool()
async def memory_search(
    project: str,
    query: str,
    kinds: Optional[list[str]] = None,
    topic: Optional[list[str]] = None,
    scope: str = "project",
    projects: Optional[list[str]] = None,
    limit: int = 8,
    hops: int = 1,
    follow_cross_edges: bool = False,
    include_archived: bool = False,
    threshold: float = retrieval.DEFAULT_SIMILARITY_THRESHOLD,
) -> list[dict[str, Any]]:
    """Semantic + graph search over the memory bank. Returns ONLY ranked,
    excerpted, relevant results — below-threshold candidates and anything
    previously memory_mark'd "irrelevant" for this exact query are filtered
    out server-side and never returned.

    scope: "project" (default, search only `project`) or "group" (search all
    projects in `project`'s group). `projects` (explicit slug list)
    overrides scope entirely for ad-hoc cross-project queries.

    hops: how many graph edge-hops to expand from the top vector matches
    (0 = pure vector search, no expansion). follow_cross_edges: whether that
    expansion may step outside the searched project(s) — set True to also
    pull the originating context of a node that was filed cross-project.

    This is meant to be called primarily from the memory-scan subagent
    (.claude/agents/memory-scan.md), which reviews these results in its own
    disposable context and reports back only a synthesized brief — keeping
    scanned-but-unneeded material out of the main session entirely.
    """
    qvec = await embed_one(query)
    qhash = retrieval.query_hash(query)
    return await retrieval.search(
        project=project,
        query_vector=qvec,
        qhash=qhash,
        kinds=kinds,
        topic=topic,
        scope=scope,
        projects=projects,
        limit=limit,
        hops=hops,
        follow_cross_edges=follow_cross_edges,
        include_archived=include_archived,
        threshold=threshold,
    )


@mcp.tool()
async def memory_mark(query: str, node_id: int, verdict: str) -> dict[str, Any]:
    """Record that `node_id` was reviewed for `query` and judged 'relevant'
    or 'irrelevant'. Future memory_search calls with the same (normalized)
    query will drop nodes marked 'irrelevant' rather than resurfacing them —
    this is the durable version of "I already checked that, stop showing it
    to me." Intended to be called by the memory-scan subagent after it
    reviews search results, not by the main session."""
    if verdict not in ("relevant", "irrelevant"):
        raise ValueError("verdict must be 'relevant' or 'irrelevant'")
    qhash = retrieval.query_hash(query)
    row = await db.fetchrow(
        """
        INSERT INTO scan_verdicts (query_hash, node_id, verdict)
        VALUES ($1, $2, $3)
        RETURNING id, query_hash, node_id, verdict, at
        """,
        qhash,
        node_id,
        verdict,
    )
    return dict(row)


@mcp.tool()
async def memory_tasks(project: str, include_archived: bool = False) -> dict[str, Any]:
    """Structured task rows for a project — what /start reads. Returns two
    lists: 'tasks' (this project's own backlog, status='active') and
    'inbox' (tasks filed into this project from another project's session,
    status='inbox') so the caller can render them as separate blocks rather
    than silently merging them.

    Each row's `task_seq` is the stable, per-project display number to show
    the user and to resolve "task #N" references back to `id` — never
    present the raw `id` as "the task number" (it's a single sequence
    shared by every project in the DB, so it jumps unpredictably whenever
    other projects insert nodes)."""
    project_id = await db.resolve_project_id(project)
    statuses = ["active", "inbox"] + (["archived"] if include_archived else [])
    rows = await db.fetch(
        """
        SELECT n.id, n.task_seq, n.title, n.status, n.priority, n.importance, n.topic,
               n.depends_note, fp.slug AS filed_from_slug
        FROM nodes n
        LEFT JOIN projects fp ON fp.id = n.filed_from_project_id
        WHERE n.project_id = $1 AND n.kind = 'task' AND n.status::text = ANY($2::text[])
        ORDER BY n.priority DESC NULLS LAST, n.importance DESC NULLS LAST
        """,
        project_id,
        statuses,
    )
    tasks = [dict(r) for r in rows if r["status"] != "inbox"]
    inbox = [dict(r) for r in rows if r["status"] == "inbox"]
    return {"tasks": tasks, "inbox": inbox}


@mcp.tool()
async def memory_active(project: str) -> Optional[dict[str, Any]]:
    """The current-focus summary for a project (the analog of the old
    activeContext.md prose — focus/blockers/next-step), used by /start.
    Returns the most recently updated kind='active' node, or None if the
    project has no active-context node yet."""
    project_id = await db.resolve_project_id(project)
    row = await db.fetchrow(
        """
        SELECT id, title, body, updated_at
        FROM nodes
        WHERE project_id = $1 AND kind = 'active' AND status != 'archived'
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        project_id,
    )
    return dict(row) if row else None


@mcp.tool()
async def memory_archive(node_id: int, status: str = "archived") -> dict[str, Any]:
    """Change a node's lifecycle status (default: archive it). Archived
    nodes are excluded from memory_search by default (include_archived=True
    opts back in) and from memory_tasks, but are never deleted — nothing
    gets permanently lost the way trimmed markdown files used to lose
    content."""
    if status not in ("active", "archived", "inbox"):
        raise ValueError("status must be 'active', 'archived', or 'inbox'")
    row = await db.fetchrow(
        "UPDATE nodes SET status = $2 WHERE id = $1 RETURNING id, title, status",
        node_id,
        status,
    )
    if row is None:
        raise ValueError(f"No node with id={node_id}")
    return dict(row)


# ---------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------


@mcp.tool()
async def memory_import(project: str, path: str, dry_run: bool = False) -> dict[str, Any]:
    """One-time import of a legacy memory-bank/*.md tree (path, e.g.
    "memory-bank" or an absolute path) into nodes/edges for `project`.
    Best-effort: see the returned 'warnings' list for anything it couldn't
    confidently classify (freeform taskDependencies.md prose, malformed
    task-table rows, etc.) — those need manual memory_upsert/memory_link
    follow-up. Safe to re-run against additional files later, but re-running
    against the same tree twice will create duplicate nodes (it does not
    dedupe against previously imported content).

    Pass dry_run=True first to preview node/edge counts and warnings without
    computing embeddings or writing to the DB — useful before committing to
    a large import (real API calls, real cost/time)."""
    project_id = await db.resolve_project_id(project)
    return await import_markdown_tree(project_id, Path(path), dry_run=dry_run)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
