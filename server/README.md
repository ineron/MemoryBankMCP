# memory-bank MCP server

Postgres-backed memory bank for Claude Code: vector + graph retrieval with
server-side relevance filtering, multi-project support, and explicit
cross-project filing. See `../.claude/claude-memory-bank.md` for the design
rationale (why Postgres instead of Markdown, the scan-and-report pattern,
cross-project semantics).

## Setup

```bash
cd server
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# edit .env: set DATABASE_URL to a real Postgres instance with pgvector
# available (CREATE EXTENSION vector requires superuser once per DB), and
# set VOYAGE_API_KEY (or switch EMBED_PROVIDER=openai + OPENAI_API_KEY)

# apply the schema once, against that database:
psql "$DATABASE_URL" -f memory_mcp/schema.sql
```

`docker-compose.yml` is kept as an optional local-dev alternative (a
pgvector/pgvector container) if you don't have a real Postgres available —
but the intended real setup is a durable Postgres instance you already run,
not a throwaway container, since this is meant to be long-lived shared
memory across many projects.

## Connecting a project

**Use the installer script** rather than doing this by hand:

```bash
server/.venv/bin/python server/scripts/install.py /path/to/project \
    [--slug SLUG] [--name NAME] [--group GROUP] [--yes] [--no-import]
```

It backs up any existing `.claude/` (most projects don't git-track it, so
this is the only undo available), installs the current
commands/agents/`claude-memory-bank.md`, writes `.claude/settings.json` +
`.mcp.json`, registers the project (and group, if given) in the shared DB,
and — if a legacy `memory-bank/*.md` tree exists — dry-runs the import
first (prints node/edge counts and warnings, no embeddings or DB writes
yet), asks for confirmation, then runs it for real and renames
`memory-bank/` aside (never deletes it). `--yes` skips the confirmation
prompt (for scripting); `--no-import` skips the memory-bank step entirely
if you'd rather run `/init-memory-bank` manually later.

Safe to re-run: project registration is an upsert, and if `memory-bank/`
was already renamed aside by a previous run, the import step is skipped.

### Doing it by hand (what the script automates)

If you'd rather do it manually, or need to adapt a step: copy
`.mcp.json.example` to `.mcp.json` at the project root and fill in the real
path —

```json
{
  "mcpServers": {
    "memory-bank": {
      "command": "/absolute/path/to/MemoryBankMCP/server/.venv/bin/python",
      "args": ["-m", "memory_mcp.server"]
    }
  }
}
```

**Use an absolute path**, even inside this repo's own `.mcp.json` — the
server is meant to be shared across multiple project repos, so a path
relative to "wherever this file happens to be" won't resolve correctly
once copied into another project's repo. `.mcp.json` itself is
git-ignored precisely because that path is machine-specific — commit
`.mcp.json.example` (the template) instead, the same `.env`/`.env.example`
pattern as the server's own config.

Then set `.claude/settings.json`:

```json
{
  "project": { "slug": "your-project-slug", "name": "...", "group": null }
}
```

and register the project once (from any session with the MCP tools
available): `project_create(slug="your-project-slug", name="...")`, adding
`group_slug=...` if it belongs to a family of related projects that should
share `scope="group"` search. If it has an existing `memory-bank/*.md`
tree, call `memory_import(project=..., path="memory-bank", dry_run=True)`
first to preview before running it for real.

## Embedding provider

Default is Voyage AI (`voyage-3.5`, 1024-dim). OpenAI (`text-embedding-3-small`,
truncated to 1024-dim via the `dimensions` param) works against the same
schema without changes. See `.env.example` for the switch.

`EMBED_PROVIDER=mock` is also available — a deterministic, hash-based
vector with no network call or API key, used only by the tests in
`tests/manual_*.py`. Never point it at data you actually want to search
over meaningfully; it has no semantic quality.

## Manual smoke tests

Not a pytest suite — quick scripts exercising the tool functions directly
against a live DB. Run with `EMBED_PROVIDER=mock` (no API key needed) against
whatever Postgres your `.env`'s `DATABASE_URL` points at:

```bash
source .venv/bin/activate
set -a; source .env; set +a
EMBED_PROVIDER=mock python tests/manual_phase2.py   # core writes: upsert/link/get, cross-project filing
EMBED_PROVIDER=mock python tests/manual_phase3.py   # retrieval: threshold filtering, graph expansion,
                                                     # scan-verdict exclusion, task/inbox split, group scope
EMBED_PROVIDER=mock python tests/manual_phase5.py   # markdown importer: task grading, dependency
                                                     # edges, dated progress, devenv topic-splitting
```

These scripts create real projects (`test-ledgyx`/`test-ledgyx-core`/
`test-ledgyx-landing` for phase2/3, `stayhug-legacy-import-test` for
phase5) in whatever database you point them at — the `test-` prefix keeps
them from colliding with any real project of the same conceptual name
(e.g. a real `ledgyx-landing`). Run against a real, in-use DB, clean up
afterward:

```bash
python3 -c "
import asyncio
from memory_mcp import db
async def main():
    for slug in ('test-ledgyx-core', 'test-ledgyx-landing', 'stayhug-legacy-import-test'):
        row = await db.fetchrow('SELECT id FROM projects WHERE slug = \$1', slug)
        if row:
            await db.execute('DELETE FROM projects WHERE id = \$1', row['id'])
asyncio.run(main())
"
```
