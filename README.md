# Memory Bank MCP

A Postgres-backed memory system for [Claude Code](https://claude.com/claude-code) — vector + graph retrieval with server-side relevance filtering, so starting a session costs two lightweight tool calls instead of reading a stack of markdown files.

## Why

Claude Code sessions start with no memory of prior work. A common fix is a folder of markdown files (`projectbrief.md`, `activeContext.md`, `progress.md`, ...) read at the start of every session via slash commands. It works — until the files grow. Then `/start` has to read everything before any real work begins, and trimming content into "archive" files just moves the problem: things get lost, and re-searching for them re-pollutes the conversation with irrelevant material.

This project replaces the file store with a small MCP server backed by Postgres + [pgvector](https://github.com/pgvector/pgvector):

- **`/start` reads two things**: a single "current focus" node and a task list. Nothing else loads until a specific task needs it.
- **Search filters server-side.** `memory_search` returns only above-threshold, graph-connected results — never a raw candidate list for you to sift through.
- **A subagent absorbs the noise.** Task investigation is delegated to a `memory-scan` subagent that reviews candidates in its own disposable context and reports back a short brief. Everything it read but didn't need dies with it.
- **One database, many projects.** Related projects can share `scope="group"` search; unrelated ones stay isolated by default. Filing a task into another project is explicit and lands as `inbox`, never silently merged into someone else's backlog.

## Features

- **Vector + graph memory** — nodes (`brief`/`product`/`pattern`/`tech`/`active`/`progress`/`devenv`/`task`/`plan`/`decision`) with typed edges (`depends_on`/`blocks`/`relates_to`/`supersedes`/`part_of`/`refines`/`cross_ref`), searchable by embedding similarity and graph proximity together.
- **Scan-and-report retrieval** — relevance filtering happens in SQL, not in the conversation; a `memory_mark` call lets a subagent permanently exclude a result from future identical searches instead of it resurfacing every time.
- **Multi-project, cross-project by design** — one shared Postgres instance, `project_id`-scoped by default, `group`-scoped on request, cross-project writes always explicit and always flagged as `inbox` on the receiving end.
- **Stable task numbering** — each project gets its own small, permanent task counter (`task_seq`), decoupled from the raw database id (which is a single sequence shared by every project and would otherwise jump unpredictably whenever *other* projects write to the database).
- **One-command installer** — point it at a project directory and it backs up any existing Claude Code config, wires up the new commands, registers the project, and — if a legacy `memory-bank/*.md` tree exists — previews the import (node counts, warnings, zero API cost) before spending a single real embedding call.
- **Legacy importer** — turns an existing file-based memory bank into nodes: task tables become graded task nodes with real dependency edges, dated progress logs, topic-tagged environment facts, YAML-frontmatter files, and monthly changelog archives are all handled, not just flattened into one blob.

## Cross-project filing, by example

A session working on one project can deliberately leave something in a *different* project — a bug spotted while testing another service, a note that belongs elsewhere. It never happens by accident: writing into another project requires naming it explicitly.

```
memory_upsert(
    project="core-api",                  # target: where the task should land
    kind="task",
    title="Login endpoint returns 500 on an empty password field",
    priority=7, importance=4,
    filed_from_project="landing-page",   # source: where this session is actually working
)
```

The task lands in `core-api` with `status="inbox"` and records where it came from. Next time someone runs `/start` in `core-api`, it shows up in its own block — never silently merged into the regular backlog:

```
📥 Filed from other sessions:

| # | Task | Priority | Importance | Topic | Filed from    |
|---|------|----------|------------|-------|---------------|
| 1 | Login endpoint returns 500 on an empty password field | 🔴7 | ⭐⭐⭐⭐ | bug | landing-page |
```

`/projects` surfaces inbox counts across every connected project at a glance, so a cross-project note can't sit unnoticed for long.

## Quick start

**Requirements:** Python 3.11+, a Postgres instance with the `vector` extension available, and an embedding API key ([Voyage AI](https://www.voyageai.com/) by default, or OpenAI).

```bash
git clone <this-repo>
cd MemoryBankMCP/server
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# edit .env: DATABASE_URL, and VOYAGE_API_KEY (or EMBED_PROVIDER=openai + OPENAI_API_KEY)

psql "$DATABASE_URL" -f memory_mcp/schema.sql
```

No Postgres handy? `docker-compose.yml` in `server/` brings up a local `pgvector/pgvector` container for development.

### Connect a project

```bash
server/.venv/bin/python server/scripts/install.py /path/to/your/project \
    [--slug SLUG] [--name NAME] [--group GROUP] [--yes] [--no-import]
```

This wires up the slash commands, registers the project, and — if the target already has a `memory-bank/*.md` tree from an older setup — offers to import it (with a free dry-run preview first). See [`server/README.md`](server/README.md) for the full setup and manual-registration path.

Then, in that project's own Claude Code session:

```
/init-memory-bank   # first-time setup (or migrates a legacy tree via the importer)
/start              # every session — loads current focus + task list only
```

## Slash commands

| Command | What it does |
|---|---|
| `/init-memory-bank` | Bootstraps a project's initial memory nodes, or imports an existing `memory-bank/*.md` tree |
| `/start` | Loads current focus + prioritized task list — the only two calls made at session start |
| `/save` | End-of-session checkpoint: updates the current-focus node, logs progress, keeps tasks current |
| `/scan-env` | Scans the project for environment facts (connections, ports, auth, gotchas) and records them as searchable nodes |
| `/projects` | Dashboard overview of every project connected to the memory bank |
| `/workflow:understand` → `/workflow:plan` → `/workflow:execute` → `/workflow:update-memory` | A structured task loop; `understand` delegates retrieval to the `memory-scan` subagent, `plan` requires explicit approval before implementation |
| `/commit` | Gathers `git status`/`diff`/`log` and creates a single commit |

## Repository layout

```
.claude/                    # the Claude Code integration
  commands/                 # slash commands (see table above)
  agents/memory-scan.md     # the scan-and-report subagent
  reference/                # detail docs commands read on demand, not unconditionally
  claude-memory-bank.md     # full design rationale — read this for the "why"
server/                     # the MCP server itself
  memory_mcp/
    schema.sql               # Postgres schema (nodes, edges, projects, scan_verdicts, ...)
    server.py                # FastMCP app — every tool is defined here
    retrieval.py              # vector search + graph expansion + verdict filtering
    embeddings.py             # Voyage / OpenAI / mock provider abstraction
    importer.py               # legacy memory-bank/*.md → nodes/edges migration
    migrations/               # schema migrations for an already-running database
  scripts/install.py         # one-command project connector
  tests/manual_*.py          # smoke tests (EMBED_PROVIDER=mock, no API key needed)
```

## Design docs

`.claude/claude-memory-bank.md` is the full design spec — the node/edge model, the lazy-loading principle, the scan-and-report pattern, and cross-project semantics in detail. Read it before modifying the command files; several of them cross-reference it and each other.

## License

MIT — see [LICENSE](LICENSE).
