"""Codegraph: optional plugin that builds a function/class/call map of a
project's own source tree, stored in its own `codegraph` Postgres schema
(same database as the memory bank, cleanly separable from it).

Not part of the core server — importing this package requires the optional
`codegraph` extra (`tree-sitter` + one `tree-sitter-<language>` grammar per
supported language). server.py imports this lazily and only registers the
`codegraph_*` MCP tools if the import succeeds, so a memory-bank install
that never runs `pip install -e '.[codegraph]'` is entirely unaffected —
no extra tables are touched, no extra tools appear.

Uninstalling: `DROP SCHEMA codegraph CASCADE;` removes everything this
plugin ever wrote, without touching `nodes`/`edges`/`messages`.

See schema.sql for the data model, parser.py for the tree-sitter extraction,
service.py for the incremental build + query logic, and tools.py for the
MCP tool wrappers.
"""
