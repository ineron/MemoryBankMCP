"""Incremental build + query logic for the codegraph plugin.

Kept out of tools.py the same way retrieval.py/messaging.py are kept out of
server.py — tools.py stays a thin MCP-wrapper layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pathspec

from .. import db
from . import parser

# Baseline safety net for a project with no (or an incomplete) .gitignore —
# NOT the primary exclusion mechanism. That's _load_ignore_spec below: a
# project's own .gitignore already knows what its dependencies/build
# output/vendor directories are called, across whatever language and
# package manager it uses, so respecting it scales to every ecosystem
# instead of this file chasing an ever-growing hardcoded name list. Without
# either layer, a build indexes installed packages right alongside the
# project's own code — unbounded entity-count growth with no signal, the
# exact problem reported from a comparable from-scratch implementation that
# had no such filtering at all.
_DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn",
    ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox",
    "node_modules", "dist", "build", "site-packages",
}


def _find_git_top(start: Path) -> Path | None:
    current = start
    for _ in range(20):  # bounded walk-up — never wander arbitrarily far
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def _load_ignore_spec(root: Path) -> pathspec.PathSpec | None:
    """Combines `root`'s own .gitignore with the enclosing git repo's
    top-level .gitignore (if root is inside one, and different from root),
    so a build naturally skips whatever the project already excludes from
    version control.

    Simplification, not full git nested-.gitignore semantics: patterns from
    both files are merged into one PathSpec and matched against paths
    relative to `root`, rather than each file's patterns being anchored to
    its own directory as git itself would. For the common case this exists
    to solve — one top-level .gitignore excluding .venv/node_modules/
    dist/etc. — that difference doesn't matter; a project relying on
    deeply nested, directory-specific .gitignore rules could see a pattern
    applied slightly more broadly than git would."""
    lines: list[str] = []
    root_gitignore = root / ".gitignore"
    if root_gitignore.is_file():
        lines.extend(root_gitignore.read_text(errors="ignore").splitlines())

    git_top = _find_git_top(root)
    if git_top is not None and git_top != root:
        top_gitignore = git_top / ".gitignore"
        if top_gitignore.is_file():
            lines.extend(top_gitignore.read_text(errors="ignore").splitlines())

    return pathspec.PathSpec.from_lines("gitwildmatch", lines) if lines else None


def _discover_files(root: Path) -> dict[str, tuple[str, bytes]]:
    """relpath (posix, relative to root) -> (language, content bytes), for
    every file under `root` whose extension is supported. Skips common
    vendor/build/VCS directories, anything under a hidden directory, and
    anything the project's own .gitignore excludes."""
    ignore_spec = _load_ignore_spec(root)
    found: dict[str, tuple[str, bytes]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts[:-1]
        if any(part in _DEFAULT_IGNORE_DIRS or part.startswith(".") for part in rel_parts):
            continue
        language = parser.SUPPORTED_EXTENSIONS.get(path.suffix)
        if language is None:
            continue
        relpath = path.relative_to(root).as_posix()
        if ignore_spec is not None and ignore_spec.match_file(relpath):
            continue
        found[relpath] = (language, path.read_bytes())
    return found


def _module_full_path(relpath: str) -> str:
    parts = Path(relpath).parts
    parts = list(parts[:-1]) + [Path(relpath).stem]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else Path(relpath).stem


async def build_project_graph(project: str, root: str) -> dict[str, Any]:
    """Incrementally (re)build the codegraph for `project`'s source tree at
    `root`. Only files whose content hash changed since the last build are
    reparsed; unchanged files' entities/dependencies are left untouched;
    files removed from disk have their rows deleted (cascades to their
    entities and outgoing dependencies)."""
    project_id = await db.resolve_project_id(project)
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise ValueError(f"root path does not exist or is not a directory: {root_path}")

    disk_files = _discover_files(root_path)
    existing = await db.fetch(
        "SELECT path, file_hash FROM codegraph.files WHERE project_id = $1", project_id
    )
    existing_hash = {r["path"]: r["file_hash"] for r in existing}

    parsed_by_path: dict[str, tuple[str, str, parser.ParsedFile]] = {}
    changed_paths: list[str] = []
    for relpath, (language, content) in disk_files.items():
        new_hash = parser.file_hash(content)
        if existing_hash.get(relpath) == new_hash:
            continue
        changed_paths.append(relpath)
        module_full_path = _module_full_path(relpath)
        parsed = parser.parse_file(Path(relpath), module_full_path, content)
        parsed_by_path[relpath] = (language, new_hash, parsed)

    removed_paths = [p for p in existing_hash if p not in disk_files]

    async with db.acquire() as conn:
        async with conn.transaction():
            if removed_paths:
                await conn.execute(
                    "DELETE FROM codegraph.files WHERE project_id = $1 AND path = ANY($2::text[])",
                    project_id, removed_paths,
                )

            all_new_deps: list[parser.RawDependency] = []
            for relpath, (language, file_hash_val, parsed) in parsed_by_path.items():
                file_row = await conn.fetchrow(
                    """
                    INSERT INTO codegraph.files (project_id, path, language, file_hash)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (project_id, path)
                    DO UPDATE SET file_hash = EXCLUDED.file_hash,
                                  language = EXCLUDED.language,
                                  updated_at = now()
                    RETURNING id
                    """,
                    project_id, relpath, language, file_hash_val,
                )
                file_id = file_row["id"]
                # Old entities for this file must go before reinserting —
                # ON CONFLICT above only touched the `files` row itself.
                await conn.execute("DELETE FROM codegraph.entities WHERE file_id = $1", file_id)
                await _insert_entities(conn, project_id, file_id, parsed.entities)
                all_new_deps.extend(parsed.dependencies)

            if all_new_deps:
                await _insert_new_dependencies(conn, project_id, all_new_deps)
            if changed_paths:
                # Any file's rebuild can delete-and-reinsert entities that
                # OTHER, unchanged files' dependency rows point at as their
                # target — those rows correctly go target_entity_id=NULL
                # (ON DELETE SET NULL) rather than disappearing, but stay
                # NULL forever unless re-checked against the now-current
                # entity table. Without this pass, "unused_entities" and
                # "called_by" would silently rot every time only *some* of a
                # project's files change between builds — the common case.
                await _reresolve_unresolved_dependencies(conn, project_id)

    return {
        "project": project,
        "root": str(root_path),
        "files_scanned": len(disk_files),
        "files_changed": len(changed_paths),
        "files_removed": len(removed_paths),
        "files_unchanged": len(disk_files) - len(changed_paths),
    }


async def _insert_entities(conn, project_id: int, file_id: int, entities: list[parser.RawEntity]) -> None:
    # Entities come out of the parser parent-first (module, then its classes,
    # then their methods, ...), so a simple left-to-right pass can always
    # resolve parent_id from what's already been inserted.
    id_by_full_path: dict[str, int] = {}
    for e in entities:
        parent_id = id_by_full_path.get(e.parent_full_path) if e.parent_full_path else None
        row = await conn.fetchrow(
            """
            INSERT INTO codegraph.entities
                (project_id, file_id, parent_id, kind, name, full_path, start_line, end_line, is_public, has_decorator)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (project_id, full_path)
            DO UPDATE SET file_id = EXCLUDED.file_id, parent_id = EXCLUDED.parent_id,
                          kind = EXCLUDED.kind, start_line = EXCLUDED.start_line,
                          end_line = EXCLUDED.end_line, is_public = EXCLUDED.is_public,
                          has_decorator = EXCLUDED.has_decorator
            RETURNING id
            """,
            project_id, file_id, parent_id, e.kind, e.name, e.full_path,
            e.start_line, e.end_line, e.is_public, e.has_decorator,
        )
        id_by_full_path[e.full_path] = row["id"]


async def _entity_index(conn, project_id: int):
    """(by_full_path, by_suffix, modules_by_suffix) over the project's
    *current* entities — called fresh each time a resolution pass runs,
    never cached, since a build can change entities out from under it.

    by_suffix covers function/method/class, keyed by the entity's last
    dotted component: 'class' is included because the parser doesn't
    distinguish a constructor call (ClassName(...)) from any other call —
    both are "call" nodes — so without this, every class would show up as
    "unused" in codegraph_issues regardless of how often it's instantiated.

    modules_by_suffix is the same idea restricted to kind='module', used to
    recognize "this call's receiver is one of this project's own modules"
    (see _resolve_target)."""
    entity_rows = await conn.fetch(
        "SELECT id, full_path, kind FROM codegraph.entities WHERE project_id = $1", project_id
    )
    by_full_path = {r["full_path"]: r for r in entity_rows}
    by_suffix: dict[str, list] = {}
    modules_by_suffix: dict[str, list] = {}
    for r in entity_rows:
        if r["kind"] in ("function", "method", "class"):
            by_suffix.setdefault(r["full_path"].rsplit(".", 1)[-1], []).append(r)
        elif r["kind"] == "module":
            modules_by_suffix.setdefault(r["full_path"].rsplit(".", 1)[-1], []).append(r)
    return by_full_path, by_suffix, modules_by_suffix


def _resolve_target(
    dependency_type: str, target_name: str, source, by_full_path, by_suffix, modules_by_suffix
) -> int | None:
    """Best-effort resolution — never real symbol resolution, just
    progressively narrower name matching, most-specific first:

    1. self.foo()/cls.foo() inside a method resolves against that method's
       *own* enclosing class first, before any project-wide name search.
    2. A dotted call whose receiver names one of this project's own modules
       (by bare name or last dotted component, matching how it's commonly
       imported) resolves against *that module's* namespace specifically —
       this is what makes "db.fetch()" mean db.py's fetch() and nothing
       else, deterministically, with no ambiguity to even consider.
    3. Any other dotted call (receiver is presumably a local variable or
       instance, not a reference to this project's own module/class) can
       only plausibly be a bound method — a bare project-level function
       with the same name is excluded from candidates. This is what stops
       "conn.fetch()" (an asyncpg Connection method, reached through a
       local variable) from being confused with the unrelated top-level
       function "db.fetch" purely because they share a name.
    4. A bare, receiver-less call matches only functions/classes (never
       methods — Python has no way to call a bound method without some
       receiver).

    Anything still ambiguous or unmatched after that is left unresolved
    (caller keeps target_name, target_entity_id NULL) rather than guessed
    at or dropped — see schema.sql's comment on codegraph.dependencies for
    why."""
    if dependency_type == "imports":
        target = by_full_path.get(target_name)
        return target["id"] if target is not None and target["kind"] == "module" else None

    if "." not in target_name:
        candidates = [c for c in by_suffix.get(target_name, []) if c["kind"] in ("function", "class")]
        return candidates[0]["id"] if len(candidates) == 1 else None

    receiver, _, method_name = target_name.rpartition(".")

    if receiver in ("self", "cls") and source is not None and source["kind"] == "method":
        class_full_path = source["full_path"].rsplit(".", 1)[0]
        same_class = by_full_path.get(f"{class_full_path}.{method_name}")
        if same_class is not None and same_class["kind"] == "method":
            return same_class["id"]

    module_matches = [
        by_full_path[f"{m['full_path']}.{method_name}"]
        for m in modules_by_suffix.get(receiver, [])
        if f"{m['full_path']}.{method_name}" in by_full_path
    ]
    if len(module_matches) == 1:
        return module_matches[0]["id"]

    candidates = [c for c in by_suffix.get(method_name, []) if c["kind"] == "method"]
    return candidates[0]["id"] if len(candidates) == 1 else None


async def _insert_new_dependencies(conn, project_id: int, deps: list[parser.RawDependency]) -> None:
    by_full_path, by_suffix, modules_by_suffix = await _entity_index(conn, project_id)

    rows = []
    for dep in deps:
        source = by_full_path.get(dep.source_full_path)
        if source is None:
            continue  # defensive — shouldn't happen, source is always just-inserted
        target_id = _resolve_target(
            dep.dependency_type, dep.target_name, source, by_full_path, by_suffix, modules_by_suffix
        )
        rows.append((project_id, source["id"], target_id, dep.target_name, dep.dependency_type, dep.line_number))

    if rows:
        await conn.executemany(
            """
            INSERT INTO codegraph.dependencies
                (project_id, source_entity_id, target_entity_id, target_name, dependency_type, line_number)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            rows,
        )


async def _reresolve_unresolved_dependencies(conn, project_id: int) -> None:
    """Re-attempt resolution for every still-unresolved dependency row in
    the project (not just ones touched by this build) against the current
    entity table. Needed because rebuilding file A deletes-and-reinserts
    A's entities, which sets target_entity_id=NULL on any row from an
    UNCHANGED file B that called into A — B itself won't be reparsed just
    because A changed, so without this pass that edge would stay NULL
    forever instead of healing once A's new entities exist."""
    by_full_path, by_suffix, modules_by_suffix = await _entity_index(conn, project_id)
    unresolved = await conn.fetch(
        """
        SELECT d.id, d.target_name, d.dependency_type,
               e.full_path AS source_full_path, e.kind AS source_kind
        FROM codegraph.dependencies d JOIN codegraph.entities e ON e.id = d.source_entity_id
        WHERE d.project_id = $1 AND d.target_entity_id IS NULL
        """,
        project_id,
    )
    updates = []
    for r in unresolved:
        source = {"full_path": r["source_full_path"], "kind": r["source_kind"]}
        target_id = _resolve_target(
            r["dependency_type"], r["target_name"], source, by_full_path, by_suffix, modules_by_suffix
        )
        if target_id is not None:
            updates.append((target_id, r["id"]))
    if updates:
        await conn.executemany(
            "UPDATE codegraph.dependencies SET target_entity_id = $1 WHERE id = $2", updates
        )


async def get_map(project: str, prefix: str | None = None) -> dict[str, Any]:
    """Hierarchical module/class/function/method tree. `prefix` (a full_path
    prefix, e.g. "pkg.mod") restricts to that subtree."""
    project_id = await db.resolve_project_id(project)
    if prefix:
        rows = await db.fetch(
            """
            SELECT id, parent_id, kind, name, full_path, start_line, end_line, is_public
            FROM codegraph.entities
            WHERE project_id = $1 AND (full_path = $2 OR full_path LIKE $2 || '.%')
            ORDER BY full_path
            """,
            project_id, prefix,
        )
    else:
        rows = await db.fetch(
            """
            SELECT id, parent_id, kind, name, full_path, start_line, end_line, is_public
            FROM codegraph.entities WHERE project_id = $1
            ORDER BY full_path
            """,
            project_id,
        )

    nodes = {r["id"]: {**dict(r), "children": []} for r in rows}
    roots = []
    for r in rows:
        node = nodes[r["id"]]
        parent = nodes.get(r["parent_id"]) if r["parent_id"] is not None else None
        if parent is not None:
            parent["children"].append(node)
        else:
            roots.append(node)
    return {"project": project, "entity_count": len(rows), "tree": roots}


async def get_deps(project: str, entity_path: str) -> dict[str, Any]:
    """calls/called_by/imports/imported_by for one entity, found by exact
    full_path or (if that misses) a unique suffix match — the same
    convenience `codegraph_search` offers, so callers can pass a short
    name instead of the fully dotted path."""
    project_id = await db.resolve_project_id(project)
    entity = await db.fetchrow(
        "SELECT id, kind, full_path FROM codegraph.entities WHERE project_id = $1 AND full_path = $2",
        project_id, entity_path,
    )
    if entity is None:
        candidates = await db.fetch(
            "SELECT id, kind, full_path FROM codegraph.entities WHERE project_id = $1 AND full_path LIKE '%' || $2",
            project_id, "." + entity_path,
        )
        if len(candidates) == 1:
            entity = candidates[0]
        elif len(candidates) > 1:
            return {
                "error": f"ambiguous entity_path {entity_path!r}",
                "candidates": [c["full_path"] for c in candidates],
            }
        else:
            return {"error": f"no entity found matching {entity_path!r}"}

    calls = await db.fetch(
        """
        SELECT target_name, target_entity_id IS NOT NULL AS resolved, line_number
        FROM codegraph.dependencies
        WHERE source_entity_id = $1 AND dependency_type = 'calls' ORDER BY line_number
        """,
        entity["id"],
    )
    called_by = await db.fetch(
        """
        SELECT e.full_path AS source, d.line_number
        FROM codegraph.dependencies d JOIN codegraph.entities e ON e.id = d.source_entity_id
        WHERE d.target_entity_id = $1 AND d.dependency_type = 'calls' ORDER BY e.full_path
        """,
        entity["id"],
    )
    imports = await db.fetch(
        """
        SELECT target_name, target_entity_id IS NOT NULL AS resolved, line_number
        FROM codegraph.dependencies
        WHERE source_entity_id = $1 AND dependency_type = 'imports' ORDER BY line_number
        """,
        entity["id"],
    )
    imported_by = await db.fetch(
        """
        SELECT e.full_path AS source, d.line_number
        FROM codegraph.dependencies d JOIN codegraph.entities e ON e.id = d.source_entity_id
        WHERE d.target_entity_id = $1 AND d.dependency_type = 'imports' ORDER BY e.full_path
        """,
        entity["id"],
    )
    return {
        "entity": entity["full_path"],
        "kind": entity["kind"],
        "calls": [dict(r) for r in calls],
        "called_by": [dict(r) for r in called_by],
        "imports": [dict(r) for r in imports],
        "imported_by": [dict(r) for r in imported_by],
    }


async def search(project: str, query: str, limit: int = 20) -> dict[str, Any]:
    project_id = await db.resolve_project_id(project)
    rows = await db.fetch(
        """
        SELECT e.kind, e.full_path, e.start_line, e.end_line, e.is_public, f.path AS file_path
        FROM codegraph.entities e JOIN codegraph.files f ON f.id = e.file_id
        WHERE e.project_id = $1 AND e.full_path ILIKE '%' || $2 || '%'
        ORDER BY e.full_path LIMIT $3
        """,
        project_id, query, limit,
    )
    return {"project": project, "query": query, "matches": [dict(r) for r in rows]}


async def get_issues(project: str) -> dict[str, Any]:
    """duplicate function/method names, unused public functions/classes/
    methods (zero resolved incoming calls, excluding test_* by name and
    anything @decorator-wrapped — Flask/FastAPI routes, @pytest.fixture,
    etc. are invoked by a framework at runtime, not by a name lookup this
    graph can see, so counting them as unused would be mostly noise on any
    web-framework project), and dependency cycles (full Tarjan SCC
    decomposition over calls+imports edges — every node is visited exactly
    once across the whole run, unlike a naive DFS with a start-node loop
    sharing one visited set, which silently undercounts cycles and can stop
    at the first one found per start node). Cycles are split into `cycles`
    (a real multi-function cycle — usually a meaningful architectural
    finding) and `self_recursive` (a function directly calling itself —
    normal for a recursive helper, and indistinguishable from a real
    problem if lumped in with `cycles`)."""
    project_id = await db.resolve_project_id(project)

    dup_rows = await db.fetch(
        """
        SELECT name, array_agg(full_path ORDER BY full_path) AS full_paths
        FROM codegraph.entities
        WHERE project_id = $1 AND kind IN ('function', 'method')
        GROUP BY name HAVING count(*) > 1
        ORDER BY name
        """,
        project_id,
    )

    unused_rows = await db.fetch(
        """
        SELECT e.full_path, e.kind
        FROM codegraph.entities e
        WHERE e.project_id = $1
          AND e.kind IN ('function', 'method', 'class')
          AND e.is_public
          AND NOT e.has_decorator
          AND e.name NOT LIKE 'test\\_%' ESCAPE '\\'
          AND NOT EXISTS (
              SELECT 1 FROM codegraph.dependencies d
              WHERE d.target_entity_id = e.id AND d.dependency_type = 'calls'
          )
        ORDER BY e.full_path
        """,
        project_id,
    )

    edge_rows = await db.fetch(
        """
        SELECT source_entity_id, target_entity_id FROM codegraph.dependencies
        WHERE project_id = $1 AND target_entity_id IS NOT NULL
        """,
        project_id,
    )
    cycles = _find_cycles(edge_rows)
    full_path_by_id = {}
    if cycles:
        entity_ids = {i for cycle in cycles for i in cycle}
        rows = await db.fetch(
            "SELECT id, full_path FROM codegraph.entities WHERE id = ANY($1::bigint[])",
            list(entity_ids),
        )
        full_path_by_id = {r["id"]: r["full_path"] for r in rows}

    named_cycles = [[full_path_by_id[i] for i in cycle] for cycle in cycles]
    return {
        "project": project,
        "duplicate_names": [dict(r) for r in dup_rows],
        "unused_entities": [dict(r) for r in unused_rows],
        "cycles": [c for c in named_cycles if len(c) > 1],
        "self_recursive": [c[0] for c in named_cycles if len(c) == 1],
    }


def _find_cycles(edge_rows) -> list[list[int]]:
    """Tarjan's SCC algorithm. Returns each strongly-connected component of
    size > 1 (a real cycle — a single self-referential node would also be
    size 1 unless it calls itself, which IS a cycle and IS included since
    Tarjan puts a self-loop node in its own size-1 SCC only when it has no
    self-edge; we treat any SCC with an internal edge as a reported cycle)."""
    adjacency: dict[int, list[int]] = {}
    self_loops: set[int] = set()
    for r in edge_rows:
        src, dst = r["source_entity_id"], r["target_entity_id"]
        if src == dst:
            self_loops.add(src)
            continue
        adjacency.setdefault(src, []).append(dst)

    index_counter = [0]
    stack: list[int] = []
    on_stack: set[int] = set()
    indices: dict[int, int] = {}
    lowlink: dict[int, int] = {}
    sccs: list[list[int]] = []

    nodes = set(adjacency.keys()) | {d for dsts in adjacency.values() for d in dsts}

    def strongconnect(v: int) -> None:
        indices[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in adjacency.get(v, []):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            sccs.append(scc)

    for v in nodes:
        if v not in indices:
            strongconnect(v)

    cycles = [scc for scc in sccs if len(scc) > 1]
    cycles.extend([[node] for node in self_loops if all(node not in c for c in cycles)])
    return cycles
