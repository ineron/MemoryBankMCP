---
description: Build/refresh this project's function-class-call map (codegraph plugin) and summarize it
---
# Scan Code Structure

## What this is

Runs the optional **codegraph** plugin (`server/memory_mcp/codegraph/`): a
tree-sitter-based scan of this project's own source tree into a
function/class/method hierarchy plus a best-effort calls/imports graph,
stored in its own `codegraph` Postgres schema (same database as the memory
bank, fully separable — `DROP SCHEMA codegraph CASCADE;` removes it without
touching `nodes`/`edges`/`messages`).

This is a standalone command, not part of the `/workflow:*` loop — run it
directly when you (or the user) want a structural map of the codebase, e.g.
before a review, when picking up unfamiliar code, or after a large refactor.
It's also written so **other commands can call it**: any command whose task
needs "what calls what" or "is this function actually used anywhere" can
invoke `codegraph_build`/`codegraph_deps`/`codegraph_issues`/
`codegraph_search` directly instead of re-deriving that by reading files —
this doc is the reference for what those tools return.

## Prerequisite (one-time per database)

If `codegraph_build` errors with an unknown-schema/relation error, the
plugin hasn't been installed yet:

```bash
cd server
.venv/bin/pip install -e '.[codegraph]'   # tree-sitter + tree-sitter-python
.venv/bin/python -m memory_mcp.codegraph.init_schema
```

Restart the MCP server connection afterward so it picks up the newly
importable plugin (server.py only registers `codegraph_*` tools if the
import succeeds at process start).

## Task

1. Resolve the project slug from `.claude/settings.json`.
2. Call `codegraph_build(project=<slug>, root=<absolute path to this
   project's source root>)`. It's incremental — only files whose content
   changed since the last build are reparsed — so it's cheap to re-run
   after small edits.
3. Call `codegraph_map(project=<slug>)` for the hierarchy and
   `codegraph_issues(project=<slug>)` for duplicate names / unused-looking
   entities / dependency cycles.
4. Present a short summary: file/entity counts from the build result, the
   top-level module list, and anything from `codegraph_issues` worth
   flagging — but read `unused_entities`' caveat literally: it's zero
   *resolved* incoming calls, not proof of dead code (entry points,
   exported APIs, and calls that didn't resolve to a unique target all look
   identical to genuinely unused code here).
5. For a specific question ("what calls X", "where is Y used"), use
   `codegraph_search(project, query)` to find the entity's full path, then
   `codegraph_deps(project, entity_path)` for its calls/called_by/imports/
   imported_by.

## Known limitations (be upfront about these, don't overstate confidence)

- Python only for now (`tree-sitter-python`); adding a language means
  adding its `tree-sitter-<language>` package and one parser function —
  see `codegraph/parser.py`'s module docstring.
- Call resolution is name-based (a call's target text matched against
  entity names), not real symbol resolution — two same-named
  methods on different classes can't always be told apart. A `calls`/
  `imports` entry with `resolved: false` in `codegraph_deps` is a real call
  site whose target couldn't be pinned down, not a dead end.
