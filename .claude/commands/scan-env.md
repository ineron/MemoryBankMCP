---
description: Scan project and populate the memory bank with discovered environment details
---
# Scan Developer Environment

## Task
Explore the project and record discovered environment details as `devenv`
nodes in the memory bank MCP server (resolve the project slug from
`.claude/settings.json` first). There is no `devenv.md` file anymore — each
finding below becomes its own node, tagged by topic, so it's retrievable by
`memory_search`/`memory-scan` without ever needing a blanket dump of every
environment fact at once.

## What to scan

### 1. Configuration Files
Read these files if they exist:
- `.env`, `.env.local`, `.env.example`
- `docker-compose.yml`, `docker-compose.*.yml`
- `settings.json`, `config.yml`, `config.json`
- `CLAUDE.md`, `README.md`
- Any `*.conf`, `nginx.conf`, `postgresql.conf`

### 2. Source Code Patterns
Search codebase for:
- Database connection strings and DSNs
- Port numbers and host definitions
- Auth patterns (JWT, API keys, OAuth)
- Service URLs and endpoints

### 3. Package Files
- `package.json` — scripts, dependencies
- `requirements.txt`, `pyproject.toml`
- `Makefile` — useful targets

## Output — one `devenv` node per finding

For each item below, first check whether a matching node already exists
(`memory_search(project, query=<item's natural-language description>,
kinds=["devenv"], hops=0, threshold=0.6)`); if a close match comes back,
`memory_upsert(..., id=<that node's id>)` to refresh it in place instead of
creating a duplicate on every re-scan.

- **Database connections** — `topic=["devenv", "database"]`, e.g. body:
  `psql -U [user] -h [host] -p [port] -d [db]`
- **Service ports** — `topic=["devenv", "ports"]`, one node listing the
  service/port/notes table, or one node per service if the project has many
- **Auth methods** — `topic=["devenv", "auth"]`, one per service
- **Testing cheatsheet** — `topic=["devenv", "testing"]`: how to check
  rendering, verify DB state, run tests
- **Useful one-liners** — `topic=["devenv", "one-liners"]`
- **Known gotchas** — `topic=["devenv", "gotchas"]`

## Guidelines
- Only write what you actually found — no placeholder nodes
- If something is unclear — say so in the body ("unclear, needs verification")
  rather than guessing
- Prefer concrete commands over descriptions
- Keep each node focused on one topic rather than one giant node for
  everything — that's what makes later retrieval precise instead of
  all-or-nothing
