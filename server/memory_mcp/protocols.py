"""Action protocols: deterministic "how do we do X" rules, matched against a
tool call BEFORE it runs, keyed on the action being taken rather than the
task's topic.

Two halves, mirroring retrieval.py's split between vector search and graph
expansion:

  - `extract_action` classifies a raw tool_name/tool_input (from a Claude
    Code PreToolUse payload) into one or more `Action`s, purely by pattern
    matching -- no DB, no network.
  - `match` looks up applicable protocol_match rows for those actions: an
    exact-field SQL match first (no embedding call), falling back to a
    cached-or-fresh vector search over kind='protocol' nodes only when the
    exact match misses. See schema.sql's "Action protocols" section for the
    table shapes this reads/writes.

protocol_hook.py (the PreToolUse hook) is the only other piece; it just
calls extract_action + match and turns the results into hookSpecificOutput.
The MCP tools (protocol_add/protocol_check/protocol_list in server.py) are
thin wrappers around add_protocol/list_protocols/this module's match().
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Optional

from . import db, retrieval
from .embeddings import embed_one

VECTOR_THRESHOLD = 0.55
# If the top two vector candidates are within this margin of each other, the
# match is treated as ambiguous rather than confident -- silence (fall
# through to "unknown") is safer than guessing between two close rules.
VECTOR_MARGIN = 0.05

_CONTROL_OPS = {"&&", "||", ";", "|", "&"}

_READONLY_UTILS = {
    "ls", "cat", "grep", "egrep", "fgrep", "find", "head", "tail", "wc",
    "echo", "pwd", "which", "file", "stat", "diff", "less", "more", "tree",
    "jq", "du", "df", "ps", "env", "printenv", "date", "whoami", "id",
    "uname", "sort", "uniq", "cut", "awk", "column", "true", "false",
}

_GIT_WRITE_SUBCOMMANDS = {
    "add", "commit", "push", "checkout", "reset", "rebase", "merge",
    "apply", "stash", "rm", "mv", "cherry-pick", "commit-tree", "tag",
    "branch", "clean", "restore",
}

_SQL_GRANT_RE = re.compile(r"\b(GRANT|REVOKE)\b", re.IGNORECASE)
_SQL_DDL_RE = re.compile(r"\b(CREATE|ALTER|DROP)\b", re.IGNORECASE)
_SQL_DML_RE = re.compile(r"\b(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
_PSQL_ROLE_URL_RE = re.compile(r"postgres(?:ql)?://([^:/@]+)(?::[^@]*)?@")


@dataclass
class Action:
    tool: str
    action_class: str
    path: Optional[str] = None
    db_role: Optional[str] = None
    extra: dict[str, str] = field(default_factory=dict)
    signature: str = ""

    def __post_init__(self) -> None:
        if not self.signature:
            parts = [self.action_class]
            if self.db_role:
                parts.append(f"role={self.db_role}")
            if self.path:
                parts.append(f"path={self.path}")
            for k, v in sorted(self.extra.items()):
                parts.append(f"{k}={v}")
            self.signature = " ".join(parts)


@dataclass
class ProtocolResult:
    action: Action
    effect: str  # 'inform' | 'ask' | 'deny' | 'none' | 'unknown'
    via: str  # 'exact' | 'vector' | 'none'
    node_id: Optional[int] = None
    rule_title: Optional[str] = None
    rule_body: Optional[str] = None


# ---------------------------------------------------------------------
# Classification: raw tool call -> Action(s). No DB access.
# ---------------------------------------------------------------------


def _rel_path(path: str, project_root: Optional[str]) -> str:
    if not path:
        return path
    if project_root:
        try:
            rel = os.path.relpath(os.path.abspath(path), os.path.abspath(project_root))
            if not rel.startswith(".."):
                return rel
        except ValueError:
            pass  # different drive on Windows, or similar -- fall through
    return path


def _split_segments(command: str) -> list[list[str]]:
    """Tokenize a shell command into per-simple-command token lists, split
    on &&/||/;/|/&. Best-effort: this is a classifier heuristic, not a shell
    parser -- on anything it can't tokenize it falls back to one segment."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments: list[list[str]] = []
    current: list[str] = []
    try:
        for tok in lexer:
            if tok in _CONTROL_OPS:
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(tok)
    except ValueError:
        return [command.split()]
    if current:
        segments.append(current)
    return segments or [[]]


def _psql_role(tokens: list[str], raw: str) -> Optional[str]:
    for i, tok in enumerate(tokens):
        if tok in ("-U", "--username") and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("-U") and len(tok) > 2:
            return tok[2:]
        if tok.startswith("--username="):
            return tok.split("=", 1)[1]
    m = _PSQL_ROLE_URL_RE.search(raw)
    return m.group(1) if m else None


def _psql_sql_class(tokens: list[str], raw: str) -> str:
    sql_text = None
    for i, tok in enumerate(tokens):
        if tok in ("-c", "--command") and i + 1 < len(tokens):
            sql_text = tokens[i + 1]
            break
    # No -c: this may be a heredoc (`psql <<'SQL' ... SQL`) or an -f file --
    # the SQL body isn't in the tokenized segment either way, but for a
    # heredoc it IS still present in the raw multi-line command string, so
    # scanning the whole thing is a reasonable heuristic. For -f (a file
    # path) this will just find nothing and fall through to 'db.query'.
    haystack = sql_text if sql_text is not None else raw
    if _SQL_GRANT_RE.search(haystack):
        return "db.grant"
    if _SQL_DDL_RE.search(haystack):
        return "db.ddl"
    if _SQL_DML_RE.search(haystack):
        return "db.dml"
    return "db.query"


_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _classify_segment(tokens: list[str], raw: str, project_root: Optional[str]) -> Optional[Action]:
    # Skip leading inline env-var assignments (`FOO=bar cmd ...`) so the
    # classifier looks at the actual command, not e.g. "shell.FOO=bar".
    idx = 0
    while idx < len(tokens) and _ENV_ASSIGNMENT_RE.match(tokens[idx]):
        idx += 1
    tokens = tokens[idx:]
    if not tokens:
        return None
    cmd = tokens[0]
    base = os.path.basename(cmd)

    if base in ("psql",):
        role = _psql_role(tokens, raw)
        action_class = _psql_sql_class(tokens, raw)
        return Action(tool="Bash", action_class=action_class, db_role=role)

    if base in ("curl", "http", "httpie", "wget"):
        host = None
        for tok in tokens[1:]:
            if tok.startswith("http://") or tok.startswith("https://"):
                host = tok.split("/")[2] if "/" in tok[8:] else tok
                break
        return Action(tool="Bash", action_class="api.call", extra={"host": host} if host else {})

    if base == "git":
        subcmd = tokens[1] if len(tokens) > 1 else ""
        cls = "git.write" if subcmd in _GIT_WRITE_SUBCOMMANDS else "git.read"
        return Action(tool="Bash", action_class=cls)

    if base in _READONLY_UTILS:
        return Action(tool="Bash", action_class="shell.readonly")

    return Action(tool="Bash", action_class=f"shell.{base}")


def extract_action(
    tool_name: str, tool_input: dict[str, Any], project_root: Optional[str] = None
) -> list[Action]:
    """Classify a PreToolUse tool call into one Action per shell segment (for
    Bash) or a single Action (for Read/Edit/Write). Returns [] for tools we
    don't have a protocol vocabulary for."""
    if tool_name == "Read":
        path = _rel_path(tool_input.get("file_path", ""), project_root)
        return [Action(tool="Read", action_class="file.read", path=path)]

    if tool_name in ("Edit", "Write", "NotebookEdit"):
        raw_path = tool_input.get("file_path") or tool_input.get("notebook_path", "")
        path = _rel_path(raw_path, project_root)
        return [Action(tool=tool_name, action_class="file.edit", path=path)]

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if not command:
            return []
        actions = []
        for tokens in _split_segments(command):
            action = _classify_segment(tokens, command, project_root)
            if action:
                actions.append(action)
        return actions

    return []


# ---------------------------------------------------------------------
# Matching: Action -> applicable protocol_match row(s). DB + (sometimes)
# an embedding call.
# ---------------------------------------------------------------------


def _action_class_prefix(action_class: str) -> str:
    return action_class.split(".", 1)[0] + ".*"


async def _exact_match(
    project_id: int, group_id: Optional[int], global_id: int, action: Action
) -> Optional[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT pm.node_id, pm.scope, pm.group_id, pm.tools, pm.action_class,
               pm.path_glob, pm.db_role, pm.effect, n.title, n.body
        FROM protocol_match pm
        JOIN nodes n ON n.id = pm.node_id
        WHERE n.status = 'active'
          AND (
            (pm.scope = 'project' AND n.project_id = $1)
            OR (pm.scope = 'group' AND $2::bigint IS NOT NULL AND pm.group_id = $2)
            OR (pm.scope = 'global' AND n.project_id = $3)
          )
          AND (pm.tools IS NULL OR $4 = ANY(pm.tools))
          AND (pm.action_class = $5 OR pm.action_class = $6)
          AND (pm.db_role IS NULL OR pm.db_role = $7)
        """,
        project_id,
        group_id,
        global_id,
        action.tool,
        action.action_class,
        _action_class_prefix(action.action_class),
        action.db_role,
    )
    candidates = [
        dict(r) for r in rows
        if not r["path_glob"] or (action.path and fnmatch.fnmatch(action.path, r["path_glob"]))
    ]
    if not candidates:
        return None

    scope_rank = {"project": 0, "group": 1, "global": 2}

    def specificity(row: dict[str, Any]) -> tuple:
        exact_class = row["action_class"] == action.action_class
        return (
            scope_rank[row["scope"]],
            0 if row["path_glob"] else 1,
            0 if row["db_role"] else 1,
            0 if exact_class else 1,
        )

    candidates.sort(key=specificity)
    return candidates[0]


def _signature_hash(project_id: int, signature: str) -> str:
    return hashlib.sha256(f"{project_id}:{signature}".encode("utf-8")).hexdigest()


async def _vector_match(
    project: str, project_id: int, action: Action
) -> Optional[dict[str, Any]]:
    sig_hash = _signature_hash(project_id, action.signature)
    cached = await db.fetchrow(
        "SELECT node_id FROM protocol_vector_cache WHERE project_id = $1 AND signature_hash = $2",
        project_id,
        sig_hash,
    )
    if cached is not None:
        if cached["node_id"] is None:
            return None
        row = await db.fetchrow(
            """
            SELECT pm.node_id, pm.scope, pm.group_id, pm.tools, pm.action_class,
                   pm.path_glob, pm.db_role, pm.effect, n.title, n.body
            FROM protocol_match pm JOIN nodes n ON n.id = pm.node_id
            WHERE pm.node_id = $1 AND n.status = 'active'
            """,
            cached["node_id"],
        )
        return dict(row) if row else None

    group_id = await db.project_group_id(project_id)
    projects = [project]
    if group_id is not None:
        member_ids = await db.project_ids_in_group(group_id)
        member_rows = await db.fetch(
            "SELECT slug FROM projects WHERE id = ANY($1::bigint[])", member_ids
        )
        projects.extend(r["slug"] for r in member_rows if r["slug"] != project)
    projects.append("_global")

    vector = await embed_one(action.signature)
    qhash = retrieval.query_hash(action.signature)
    results = await retrieval.search(
        project=project,
        query_vector=vector,
        qhash=qhash,
        kinds=["protocol"],
        scope="project",
        projects=projects,
        hops=0,
        limit=2,
        threshold=VECTOR_THRESHOLD,
    )

    matched_node_id = None
    if results:
        top = results[0]
        confident = len(results) == 1 or (top["similarity"] - results[1]["similarity"]) >= VECTOR_MARGIN
        if confident:
            matched_node_id = top["id"]

    await db.execute(
        """
        INSERT INTO protocol_vector_cache (project_id, signature_hash, node_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (project_id, signature_hash) DO UPDATE SET node_id = EXCLUDED.node_id, at = now()
        """,
        project_id,
        sig_hash,
        matched_node_id,
    )

    if matched_node_id is None:
        return None
    row = await db.fetchrow(
        """
        SELECT pm.node_id, pm.scope, pm.group_id, pm.tools, pm.action_class,
               pm.path_glob, pm.db_role, pm.effect, n.title, n.body
        FROM protocol_match pm JOIN nodes n ON n.id = pm.node_id
        WHERE pm.node_id = $1
        """,
        matched_node_id,
    )
    return dict(row) if row else None


async def match(project: str, actions: list[Action]) -> list[ProtocolResult]:
    """Resolve each action to its applicable protocol, exact match first,
    vector search only on a miss. An action with no rule anywhere comes back
    effect='unknown', via='none' -- protocol_hook.py treats that as
    "ask the user, then protocol_add"."""
    project_id = await db.resolve_project_id(project)
    group_id = await db.project_group_id(project_id)
    global_id = await db.resolve_project_id("_global")

    results = []
    for action in actions:
        row = await _exact_match(project_id, group_id, global_id, action)
        via = "exact"
        if row is None:
            row = await _vector_match(project, project_id, action)
            via = "vector" if row else "none"
        if row is None:
            results.append(ProtocolResult(action=action, effect="unknown", via="none"))
        else:
            results.append(
                ProtocolResult(
                    action=action,
                    effect=row["effect"],
                    via=via,
                    node_id=row["node_id"],
                    rule_title=row["title"],
                    rule_body=row["body"],
                )
            )
    return results


# ---------------------------------------------------------------------
# CRUD: backs the protocol_add/protocol_list MCP tools in server.py.
# ---------------------------------------------------------------------


async def invalidate_cache() -> None:
    """Wipe the whole vector-match cache. Called after any write that could
    change which protocol a signature resolves to (protocol_add, or
    memory_archive on a kind='protocol' node) -- the cache has no per-row
    invalidation, just this blunt whole-table reset, since it's small and
    writes are rare compared to lookups."""
    await db.execute("DELETE FROM protocol_vector_cache")


async def add_protocol(
    project: str,
    rule: str,
    body: str = "",
    scope: str = "project",
    action_class: str = "",
    tools: Optional[list[str]] = None,
    path_glob: Optional[str] = None,
    db_role: Optional[str] = None,
    effect: str = "inform",
) -> dict[str, Any]:
    if scope not in ("project", "group", "global"):
        raise ValueError("scope must be 'project', 'group', or 'global'")
    if effect not in ("inform", "ask", "deny", "none"):
        raise ValueError("effect must be 'inform', 'ask', 'deny', or 'none'")
    if not action_class:
        raise ValueError("action_class is required, e.g. 'db.ddl' or 'file.read'")

    owning_project = "_global" if scope == "global" else project
    project_id = await db.resolve_project_id(owning_project)
    group_id = None
    if scope == "group":
        group_id = await db.project_group_id(project_id)
        if group_id is None:
            raise ValueError(f"project '{project}' is not in a group; scope='group' needs one")

    vector = await embed_one(f"{rule}\n\n{body}" if body else rule)
    node = await db.fetchrow(
        """
        INSERT INTO nodes (project_id, kind, title, body, topic, embedding)
        VALUES ($1, 'protocol', $2, $3, $4, $5)
        RETURNING id
        """,
        project_id,
        rule,
        body,
        [action_class],
        vector,
    )
    node_id = node["id"]
    await db.execute(
        """
        INSERT INTO protocol_match (node_id, scope, group_id, tools, action_class, path_glob, db_role, effect)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        node_id,
        scope,
        group_id,
        tools,
        action_class,
        path_glob,
        db_role,
        effect,
    )
    await invalidate_cache()
    return {"node_id": node_id, "scope": scope, "action_class": action_class, "effect": effect}


async def list_protocols(project: str) -> list[dict[str, Any]]:
    project_id = await db.resolve_project_id(project)
    group_id = await db.project_group_id(project_id)
    global_id = await db.resolve_project_id("_global")
    rows = await db.fetch(
        """
        SELECT pm.node_id, pm.scope, pm.tools, pm.action_class, pm.path_glob,
               pm.db_role, pm.effect, n.title, n.body, n.status
        FROM protocol_match pm
        JOIN nodes n ON n.id = pm.node_id
        WHERE n.status != 'archived'
          AND (
            (pm.scope = 'project' AND n.project_id = $1)
            OR (pm.scope = 'group' AND $2::bigint IS NOT NULL AND pm.group_id = $2)
            OR (pm.scope = 'global' AND n.project_id = $3)
          )
        ORDER BY pm.scope, pm.action_class
        """,
        project_id,
        group_id,
        global_id,
    )
    return [dict(r) for r in rows]
