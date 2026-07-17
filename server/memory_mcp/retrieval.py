"""Retrieval: vector search + graph expansion + scan-verdict filtering.

This is the server-side half of the "scan-and-report" pattern: memory_search
must never hand back irrelevant candidates for the caller to sift through —
filtering happens here, in SQL, before anything is returned. The other half
(a subagent reviewing what IS returned in its own throwaway context) lives
in .claude/agents/memory-scan.md, not in this file.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from . import db

DEFAULT_SIMILARITY_THRESHOLD = 0.2
BODY_EXCERPT_LEN = 500


def query_hash(query: str) -> str:
    """Normalize + hash a query string for scan_verdicts lookups.

    Matching is exact-normalized-string only (lowercased, whitespace
    collapsed) — this catches literal re-runs of the same search, which is
    the common "I already checked this, don't show it again" case. It does
    NOT do fuzzy/semantic matching of *similar* queries; that would need
    embedding the query history too, which is a reasonable future extension
    but out of scope for the first cut.
    """
    normalized = " ".join(query.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def _scope_project_ids(
    project_id: int, scope: str, projects: Optional[list[str]]
) -> list[int]:
    if projects:
        ids = []
        for slug in projects:
            ids.append(await db.resolve_project_id(slug))
        return ids
    if scope == "group":
        group_id = await db.project_group_id(project_id)
        if group_id is None:
            return [project_id]  # ungrouped project: group scope degrades to itself
        return await db.project_ids_in_group(group_id)
    return [project_id]


async def _vector_candidates(
    project_ids: list[int],
    query_vector: list[float],
    kinds: Optional[list[str]],
    topic: Optional[list[str]],
    include_archived: bool,
    limit: int,
) -> list[dict[str, Any]]:
    statuses = ["active", "inbox"] + (["archived"] if include_archived else [])
    rows = await db.fetch(
        """
        SELECT n.id, n.project_id, p.slug AS project_slug, n.kind, n.title, n.body,
               n.topic, n.status, n.priority, n.importance, n.depends_note,
               1 - (n.embedding <=> $1::vector) AS similarity
        FROM nodes n
        JOIN projects p ON p.id = n.project_id
        WHERE n.project_id = ANY($2::bigint[])
          AND n.status::text = ANY($3::text[])
          AND n.embedding IS NOT NULL
          AND ($4::node_kind[] IS NULL OR n.kind = ANY($4::node_kind[]))
          AND ($5::text[] IS NULL OR n.topic && $5::text[])
        ORDER BY n.embedding <=> $1::vector
        LIMIT $6
        """,
        query_vector,
        project_ids,
        statuses,
        kinds,
        topic,
        limit,
    )
    return [dict(r) for r in rows]


async def _graph_expand(
    seed_ids: list[int],
    hops: int,
    allowed_project_ids: list[int],
    follow_cross_edges: bool,
) -> list[dict[str, Any]]:
    if hops <= 0 or not seed_ids:
        return []
    rows = await db.fetch(
        """
        WITH RECURSIVE expand(id, depth) AS (
            SELECT unnest($1::bigint[]), 0
            UNION
            SELECT CASE WHEN e.src_id = ex.id THEN e.dst_id ELSE e.src_id END, ex.depth + 1
            FROM edges e
            JOIN expand ex ON e.src_id = ex.id OR e.dst_id = ex.id
            WHERE ex.depth < $2
        )
        SELECT DISTINCT ON (n.id)
               n.id, n.project_id, p.slug AS project_slug, n.kind, n.title, n.body,
               n.topic, n.status, n.priority, n.importance, n.depends_note,
               ex.depth AS hop_distance
        FROM expand ex
        JOIN nodes n ON n.id = ex.id
        JOIN projects p ON p.id = n.project_id
        WHERE ex.depth > 0
          AND n.status != 'archived'
          AND ($3::boolean OR n.project_id = ANY($4::bigint[]))
        ORDER BY n.id, ex.depth ASC
        """,
        seed_ids,
        hops,
        follow_cross_edges,
        allowed_project_ids,
    )
    return [dict(r) for r in rows]


async def _apply_verdicts(qhash: str, node_ids: list[int]) -> dict[int, str]:
    if not node_ids:
        return {}
    rows = await db.fetch(
        """
        SELECT node_id, verdict FROM scan_verdicts
        WHERE query_hash = $1 AND node_id = ANY($2::bigint[])
        """,
        qhash,
        node_ids,
    )
    return {r["node_id"]: r["verdict"] for r in rows}


def _excerpt(body: str) -> str:
    if len(body) <= BODY_EXCERPT_LEN:
        return body
    return body[:BODY_EXCERPT_LEN].rsplit(" ", 1)[0] + "…"


async def search(
    *,
    project: str,
    query_vector: list[float],
    qhash: str,
    kinds: Optional[list[str]] = None,
    topic: Optional[list[str]] = None,
    scope: str = "project",
    projects: Optional[list[str]] = None,
    limit: int = 8,
    hops: int = 1,
    follow_cross_edges: bool = False,
    include_archived: bool = False,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[dict[str, Any]]:
    """Ranked, filtered, excerpted results — the only thing memory_search
    hands back to a caller. Below-threshold and previously-marked-irrelevant
    candidates are dropped here, not returned for the caller to sift."""
    project_id = await db.resolve_project_id(project)
    scoped_ids = await _scope_project_ids(project_id, scope, projects)

    seeds = await _vector_candidates(
        scoped_ids, query_vector, kinds, topic, include_archived, limit=limit * 3
    )
    seeds = [s for s in seeds if s["similarity"] >= threshold]

    seed_ids = [s["id"] for s in seeds]
    neighbors = await _graph_expand(seed_ids, hops, scoped_ids, follow_cross_edges)

    merged: dict[int, dict[str, Any]] = {}
    for s in seeds:
        merged[s["id"]] = {**s, "hop_distance": 0, "match": "vector"}
    for n in neighbors:
        if n["id"] not in merged:
            merged[n["id"]] = {**n, "similarity": None, "match": "graph"}

    verdicts = await _apply_verdicts(qhash, list(merged.keys()))
    results = []
    for node_id, data in merged.items():
        if verdicts.get(node_id) == "irrelevant":
            continue  # already reviewed and rejected for this exact query — never resurface
        results.append(data)

    def sort_key(r: dict[str, Any]) -> tuple:
        # vector hits first (by similarity desc), then graph hits (by hop distance asc)
        if r["match"] == "vector":
            return (0, -r["similarity"])
        return (1, r["hop_distance"])

    results.sort(key=sort_key)
    results = results[:limit]

    for r in results:
        r["body"] = _excerpt(r["body"])
    return results
