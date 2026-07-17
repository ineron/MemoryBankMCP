"""One-time migration: legacy memory-bank/*.md tree -> Postgres nodes/edges.

Splits each known file into one or more nodes by '##' heading, with a few
special cases:
  - activeContext.md: the '## Tasks' table becomes individual task nodes
    (not prose); everything else in the file becomes the single 'active'
    node's body.
  - progress.md / devenv.md: each '##' section becomes its own node (dated
    progress entries, or topic-tagged devenv facts) rather than one big node.
  - plans/*.md: one 'plan' node per file.
  - taskDependencies.md: imported as reference 'decision' nodes, NOT
    auto-converted to edges — its prose is freeform and unreliable to parse.
    Edges ARE created, though, from the Tasks table's own Depends/Related
    column, whose grammar ('blocked by #N', 'blocks #N', 'w/ #N',
    'epic w/ #N') is constrained enough to parse safely.

This is deliberately best-effort: anything it can't confidently classify is
left for a human (or /init-memory-bank's manual fallback) to handle, and is
called out in the returned summary's `warnings` list.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from . import db
from .embeddings import embed_one

FILE_KIND_MAP: dict[str, str] = {
    "projectbrief.md": "brief",
    "productContext.md": "product",
    "systemPatterns.md": "pattern",
    "techContext.md": "tech",
    "activeContext.md": "active",  # special-cased
    "progress.md": "progress",  # special-cased
    "devenv.md": "devenv",  # special-cased
}

# Pure indices — files whose entire content is "links to other files in this
# directory". Once every linked file is imported as its own searchable node,
# the index itself carries no information memory_search doesn't already
# provide, so it's skipped (with a warning) rather than imported as a node
# that's just a stale table of contents.
_PURE_INDEX_FILES = {"MEMORY.md"}

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_FRONTMATTER_DESCRIPTION_RE = re.compile(r'^description:\s*"?(.*?)"?\s*$', re.MULTILINE)
_SECTION_RE_L2 = re.compile(r"^##\s+(.*)$", re.MULTILINE)
_SECTION_RE_L3 = re.compile(r"^###\s+(.*)$", re.MULTILINE)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LEADING_TITLE_RE = re.compile(r"^#\s+.*\n?")
_DEPENDS_REF_RE = re.compile(r"(blocked by|blocks|epic w/|w/)\s*#(\d+)", re.IGNORECASE)


def _strip_frontmatter(text: str) -> str:
    """Strip a leading YAML frontmatter block (--- ... ---), e.g. the
    name/description/metadata header some hand-maintained memory files use
    (mirrors the auto-memory format: name/description/metadata.type). If a
    `description:` line is present, prepend it as a plain-text lead-in so
    that summary isn't lost, just de-YAML'd."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text
    desc_match = _FRONTMATTER_DESCRIPTION_RE.search(m.group(1))
    remainder = text[m.end():].lstrip("\n")
    if desc_match and desc_match.group(1):
        return f"_{desc_match.group(1)}_\n\n{remainder}"
    return remainder


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown on '## Heading' boundaries -> [(heading, body), ...],
    falling back to '### Heading' if the file has no '##' headings at all
    (some projects nest one level deeper, e.g. changelog entries titled
    '### Session N'). Content before the first heading (e.g. a leading
    '# Title' + intro prose) becomes an 'Overview' section if non-trivial.
    A file with no headings at either level comes back as a single section
    titled from its frontmatter `description:` if it has one (far more
    useful for browsing than a generic "Overview" — e.g. a feedback_*.md
    file's description becomes its node title), else 'Overview'."""
    fm_match = _FRONTMATTER_RE.match(text)
    fallback_title = "Overview"
    if fm_match:
        desc_match = _FRONTMATTER_DESCRIPTION_RE.search(fm_match.group(1))
        if desc_match and desc_match.group(1):
            fallback_title = desc_match.group(1).strip()

    text = _strip_frontmatter(text)
    matches = list(_SECTION_RE_L2.finditer(text))
    if not matches:
        matches = list(_SECTION_RE_L3.finditer(text))
    if not matches:
        return [(fallback_title, _LEADING_TITLE_RE.sub("", text).strip())]

    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        preamble = _LEADING_TITLE_RE.sub("", text[: matches[0].start()]).strip()
        if preamble:
            sections.append(("Overview", preamble))

    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((title, text[start:end].strip()))
    return sections


def _slug_topic(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _parse_task_table(body: str) -> list[dict[str, Any]]:
    """Parse a '## Tasks' markdown table into structured rows. Tolerates
    either the rich 6-column format (#, Task, Priority, Importance, Topic,
    Depends/Related) or a plainer subset — any row is accepted as long as
    its first cell is a bare integer (the old row number)."""
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or not re.match(r"^\d+$", cells[0]):
            continue

        old_num = int(cells[0])
        title = cells[1] if len(cells) > 1 else f"Imported task #{old_num}"
        priority_raw = cells[2] if len(cells) > 2 else ""
        importance_raw = cells[3] if len(cells) > 3 else ""
        topic_raw = cells[4] if len(cells) > 4 else ""
        depends_raw = cells[5] if len(cells) > 5 else ""

        priority_match = re.search(r"(\d)", priority_raw)
        priority = int(priority_match.group(1)) if priority_match else None
        star_count = importance_raw.count("⭐")
        importance = star_count if star_count else None
        topic = [t.strip() for t in re.split(r"[,/]", topic_raw) if t.strip()]
        depends_note = depends_raw if depends_raw not in ("", "—", "-") else None

        rows.append(
            {
                "old_num": old_num,
                "title": title,
                "priority": priority,
                "importance": importance,
                "topic": topic,
                "depends_note": depends_note,
            }
        )
    return rows


def _infer_depends_edges(depends_note: Optional[str]) -> list[tuple[int, str]]:
    """Turn a Depends/Related cell into [(old_num_referenced, rel), ...].
    Only the constrained grammar /save and /workflow:update-memory actually
    write is handled; anything else stays as free-text depends_note only."""
    if not depends_note:
        return []
    edges = []
    for m in _DEPENDS_REF_RE.finditer(depends_note):
        verb, num = m.group(1).lower(), int(m.group(2))
        if verb == "blocked by":
            edges.append((num, "depends_on"))
        elif verb == "blocks":
            edges.append((num, "blocks"))
        else:  # 'w/' or 'epic w/'
            edges.append((num, "relates_to"))
    return edges


async def import_markdown_tree(project_id: int, root: Path, dry_run: bool = False) -> dict[str, Any]:
    """Best-effort import of a legacy memory-bank/*.md tree into nodes/edges
    for `project_id`. Returns {"created": [...], "edges": [...], "warnings": [...]}.

    With dry_run=True, no embeddings are computed and no DB writes happen —
    `created`/`edges` get sequential placeholder ids so the counts, kinds,
    and warnings can be inspected (and the real API/DB cost estimated)
    before committing to a real import. This is what the installer script
    uses to show a preview and ask for confirmation first."""
    summary: dict[str, Any] = {"created": [], "edges": [], "warnings": []}
    old_to_new: dict[int, int] = {}
    pending_edges: list[tuple[int, Optional[str]]] = []  # (old_num, depends_note)
    _dry_run_next_id = [0]

    async def create_node(
        kind: str,
        title: str,
        body: str,
        topic: Optional[list[str]] = None,
        priority: Optional[int] = None,
        importance: Optional[int] = None,
        depends_note: Optional[str] = None,
    ) -> int:
        if dry_run:
            _dry_run_next_id[0] += 1
            node_id = _dry_run_next_id[0]
            task_seq = _dry_run_next_id[0] if kind == "task" else None
        else:
            task_seq = await db.next_task_seq(project_id) if kind == "task" else None
            vector = await embed_one(f"{title}\n\n{body}" if body else title)
            row = await db.fetchrow(
                """
                INSERT INTO nodes
                    (project_id, kind, title, body, topic, priority, importance, depends_note, embedding, task_seq)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id
                """,
                project_id,
                kind,
                title,
                body,
                topic or [],
                priority,
                importance,
                depends_note,
                vector,
                task_seq,
            )
            node_id = row["id"]
        summary["created"].append({"kind": kind, "title": title, "id": node_id, "task_seq": task_seq})
        return node_id

    if not root.is_dir():
        summary["warnings"].append(f"'{root}' is not a directory — nothing imported")
        return summary

    for filename, kind in FILE_KIND_MAP.items():
        path = root / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        sections = _split_sections(text)

        if filename == "activeContext.md":
            active_body_parts = []
            for title, body in sections:
                if title.strip().lower() == "tasks":
                    for row in _parse_task_table(body):
                        node_id = await create_node(
                            "task",
                            row["title"],
                            "",
                            topic=row["topic"],
                            priority=row["priority"],
                            importance=row["importance"],
                            depends_note=row["depends_note"],
                        )
                        old_to_new[row["old_num"]] = node_id
                        pending_edges.append((row["old_num"], row["depends_note"]))
                else:
                    active_body_parts.append(f"## {title}\n{body}")
            if active_body_parts:
                await create_node("active", "Current focus", "\n\n".join(active_body_parts))

        elif filename == "progress.md":
            for title, body in sections:
                node_title = title if _DATE_RE.search(title) else f"Progress — {title}"
                await create_node("progress", node_title, body)

        elif filename == "devenv.md":
            for title, body in sections:
                await create_node("devenv", title, body, topic=["devenv", _slug_topic(title)])

        else:
            if len(sections) == 1:
                await create_node(kind, filename.replace(".md", ""), sections[0][1])
            else:
                for title, body in sections:
                    await create_node(kind, title, body)

    # Edges from the Tasks table's Depends/Related column (constrained
    # grammar only — see _infer_depends_edges' docstring).
    for src_old_num, depends_note in pending_edges:
        src_id = old_to_new.get(src_old_num)
        if src_id is None:
            continue
        for dst_old_num, rel in _infer_depends_edges(depends_note):
            dst_id = old_to_new.get(dst_old_num)
            if dst_id is None:
                summary["warnings"].append(
                    f"Task #{src_old_num}'s depends_note referenced #{dst_old_num}, "
                    "which wasn't found in the Tasks table — edge not created"
                )
                continue
            if not dry_run:
                await db.execute(
                    """
                    INSERT INTO edges (src_id, dst_id, rel)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (src_id, dst_id, rel) DO NOTHING
                    """,
                    src_id,
                    dst_id,
                    rel,
                )
            summary["edges"].append({"src": src_id, "dst": dst_id, "rel": rel})

    # plans/*.md -> one 'plan' node per file
    plans_dir = root / "plans"
    if plans_dir.is_dir():
        for plan_file in sorted(plans_dir.glob("*.md")):
            text = plan_file.read_text(encoding="utf-8")
            title_match = re.search(r"^#\s+(.*)$", text, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else plan_file.stem
            await create_node("plan", title, text, topic=["plan"])

    # changelog/*.md -> archived 'progress' nodes, one per entry (adaptive
    # ##/### splitting handles both "## <date>" and "### Session N" styles).
    # index.md (a "which file has which sessions" pointer, per the archiving
    # process claude-memory-bank.md originally described) is redundant once
    # every archived file is its own searchable node, so it's skipped.
    changelog_dir = root / "changelog"
    if changelog_dir.is_dir():
        for cl_file in sorted(changelog_dir.glob("*.md")):
            if cl_file.name == "index.md":
                summary["warnings"].append(
                    "changelog/index.md skipped — pure index of the archived files "
                    "below, redundant once each is imported as its own searchable node"
                )
                continue
            for title, body in _split_sections(cl_file.read_text(encoding="utf-8")):
                node_title = f"{title} ({cl_file.stem})" if title != "Overview" else cl_file.stem
                await create_node("progress", node_title, body, topic=["changelog"])

    # Flag any other subdirectory (e.g. 'diffs/' with raw git-style diffs) —
    # non-.md content isn't retrofitted into memory nodes; call it out so a
    # human decides whether it needs manual handling.
    for extra_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in ("plans", "changelog")):
        file_count = sum(1 for _ in extra_dir.iterdir())
        summary["warnings"].append(
            f"Subdirectory '{extra_dir.name}/' ({file_count} file(s)) was not scanned — "
            "only 'plans/' and 'changelog/' are handled specially; its contents were "
            "not imported and need manual handling if they matter."
        )

    # Flag any non-.md top-level file (e.g. a stray .tar.gz backup someone
    # dropped in memory-bank/) — same reasoning as the subdirectory check
    # above: don't silently ignore it, even though only markdown is imported.
    for extra_file in sorted(p for p in root.iterdir() if p.is_file() and p.suffix != ".md"):
        summary["warnings"].append(
            f"'{extra_file.name}' is not a .md file — not imported (binary/non-markdown "
            "content isn't retrofitted into memory nodes); handle manually if it matters."
        )

    # taskDependencies.md -> reference nodes only, not auto-converted to edges
    deps_path = root / "taskDependencies.md"
    if deps_path.exists():
        summary["warnings"].append(
            "taskDependencies.md found but not auto-converted to edges (its prose "
            "is freeform and unreliable to parse); imported as 'decision' nodes "
            "for manual memory_link follow-up."
        )
        for title, body in _split_sections(deps_path.read_text(encoding="utf-8")):
            await create_node(
                "decision",
                f"Task dependency rationale — {title}",
                body,
                topic=["task-dependency-rationale"],
            )

    # Any other top-level *.md file not covered above (the old design
    # explicitly allowed ad-hoc extra files — "Additional Context" in
    # claude-memory-bank.md — for things like complex feature docs or API
    # specs). Import as 'decision' nodes rather than silently dropping them;
    # flag in warnings so a human can reclassify to a more specific kind
    # (pattern/tech/product) if one fits better.
    handled = set(FILE_KIND_MAP) | {"taskDependencies.md"}
    for extra_path in sorted(root.glob("*.md")):
        if extra_path.name in handled:
            continue
        if extra_path.name in _PURE_INDEX_FILES:
            summary["warnings"].append(
                f"'{extra_path.name}' skipped — pure index of other memory-bank files, "
                "redundant once each is imported as its own searchable node"
            )
            continue
        summary["warnings"].append(
            f"'{extra_path.name}' is not one of the recognized memory-bank files — "
            "imported as generic 'decision' nodes (topic tagged with its filename); "
            "reclassify to a more specific kind manually if one fits better."
        )
        topic_tag = _slug_topic(extra_path.stem)
        # 'feedback_*.md' is a convention some projects use for the
        # auto-memory "feedback" type (session rules/gotchas, often with a
        # metadata.type: feedback frontmatter block). It's a distinct,
        # high-value category — tag it as such in addition to the per-file
        # topic, so it stays group-searchable rather than scattered under N
        # unrelated per-file tags.
        topics = [topic_tag, "feedback"] if extra_path.stem.startswith("feedback_") else [topic_tag]
        for title, body in _split_sections(extra_path.read_text(encoding="utf-8")):
            await create_node("decision", title, body, topic=topics)

    return summary
