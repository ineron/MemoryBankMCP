"""Manual smoke test for the action-protocols layer (schema.sql's "Action
protocols" section, protocols.py, and the MCP tools protocol_add/
protocol_check/protocol_list in server.py). Does NOT exercise protocol_hook.py
itself (that's a stdin/stdout CLI script -- see its own module docstring for
how to pipe a payload into it by hand).

Uses EMBED_PROVIDER=mock: identical embeddable text -> similarity ~1.0,
different text -> ~orthogonal. That's enough to validate exact-match
priority, the vector fallback's cache, and effect branching without needing
real semantic embeddings -- same assumption manual_phase3.py relies on. Run
with:

    EMBED_PROVIDER=mock DATABASE_URL=postgresql://memory:memory@localhost:5433/memory_bank \
        python tests/manual_protocols.py

Requires migration 005 (or a fresh schema.sql) already applied -- this needs
node_kind='protocol', the '_global' project, and the protocol_match /
protocol_vector_cache tables to exist.
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory_mcp import db, protocols
from memory_mcp.protocols import Action, extract_action
from memory_mcp.server import project_create, project_group_create


def test_classifier() -> None:
    cases = [
        ("Bash", {"command": 'psql -U api_user -c "select * from t"'}, "db.query", "api_user"),
        ("Bash", {"command": 'psql -U postgres -c "CREATE TABLE foo(id int)"'}, "db.ddl", "postgres"),
        ("Bash", {"command": 'psql -U postgres -c "GRANT SELECT ON foo TO api_user"'}, "db.grant", "postgres"),
        ("Bash", {"command": "curl https://api.example.com/v1/x"}, "api.call", None),
        ("Bash", {"command": "git push origin main"}, "git.write", None),
        ("Bash", {"command": "git log --oneline"}, "git.read", None),
        ("Bash", {"command": "ls -la"}, "shell.readonly", None),
        ("Bash", {"command": "rm -rf /tmp/x"}, "shell.rm", None),
    ]
    for tool, inp, expected_class, expected_role in cases:
        actions = extract_action(tool, inp)
        assert len(actions) == 1, f"expected 1 action for {inp!r}, got {actions}"
        assert actions[0].action_class == expected_class, f"{inp!r} -> {actions[0].action_class}, want {expected_class}"
        assert actions[0].db_role == expected_role, f"{inp!r} role -> {actions[0].db_role}, want {expected_role}"

    # Composite command -> one Action per segment.
    composite = extract_action("Bash", {"command": "git pull && psql -U api_user -c 'select 1' && ls"})
    assert [a.action_class for a in composite] == ["git.read", "db.query", "shell.readonly"]

    # Read/Edit classify by tool, and resolve a relative path against project_root.
    read_action = extract_action("Read", {"file_path": "/repo/config/app.yaml"}, project_root="/repo")[0]
    assert read_action.action_class == "file.read" and read_action.path == "config/app.yaml"
    edit_action = extract_action("Edit", {"file_path": "/repo/src/main.py"}, project_root="/repo")[0]
    assert edit_action.action_class == "file.edit" and edit_action.path == "src/main.py"

    print("PASS: classifier covers psql roles/DDL/DML/GRANT, curl, git, readonly utils, composite commands, paths")


async def main() -> None:
    test_classifier()

    await project_group_create(slug="test-protoc-grp", name="Protocol test group")
    core = await project_create(slug="test-protoc-core", name="Protocol Test Core", group_slug="test-protoc-grp")
    await project_create(slug="test-protoc-sibling", name="Protocol Test Sibling", group_slug="test-protoc-grp")
    solo = await project_create(slug="test-protoc-solo", name="Protocol Test Solo (no group)")

    # --- Test: scope priority (project > group > global) ---
    await protocols.add_protocol(
        project="test-protoc-core", rule="GLOBAL rule for test.priority",
        scope="global", action_class="test.priority", effect="inform",
    )
    await protocols.add_protocol(
        project="test-protoc-core", rule="GROUP rule for test.priority",
        scope="group", action_class="test.priority", effect="inform",
    )
    await protocols.add_protocol(
        project="test-protoc-core", rule="PROJECT rule for test.priority",
        scope="project", action_class="test.priority", effect="inform",
    )
    results = await protocols.match("test-protoc-core", [Action(tool="Bash", action_class="test.priority")])
    assert results[0].via == "exact"
    assert results[0].rule_title == "PROJECT rule for test.priority", results[0].rule_title
    # Sibling project (same group, no project-level rule of its own) should fall through to the group rule.
    sibling_results = await protocols.match(
        "test-protoc-sibling", [Action(tool="Bash", action_class="test.priority")]
    )
    assert sibling_results[0].rule_title == "GROUP rule for test.priority", sibling_results[0].rule_title
    # Solo project (no group at all) should fall through to the global rule.
    solo_results = await protocols.match("test-protoc-solo", [Action(tool="Bash", action_class="test.priority")])
    assert solo_results[0].rule_title == "GLOBAL rule for test.priority", solo_results[0].rule_title
    print("PASS: scope priority project > group > global")

    # --- Test: path_glob specificity beats a generic same-scope rule ---
    await protocols.add_protocol(
        project="test-protoc-core", rule="Generic file.read rule",
        scope="project", action_class="file.read", effect="inform",
    )
    await protocols.add_protocol(
        project="test-protoc-core", rule="Secret file.read rule",
        scope="project", action_class="file.read", path_glob="*.secret", effect="deny",
    )
    generic_hit = await protocols.match(
        "test-protoc-core", [Action(tool="Read", action_class="file.read", path="notes.md")]
    )
    specific_hit = await protocols.match(
        "test-protoc-core", [Action(tool="Read", action_class="file.read", path="config.secret")]
    )
    assert generic_hit[0].rule_title == "Generic file.read rule"
    assert specific_hit[0].rule_title == "Secret file.read rule"
    assert specific_hit[0].effect == "deny"
    print("PASS: path_glob specificity beats a generic rule at the same scope")

    # --- Test: effect branches (inform/ask/deny/none) round-trip through match() ---
    for effect in ("inform", "ask", "deny", "none"):
        action_class = f"test.effect.{effect}"
        await protocols.add_protocol(
            project="test-protoc-core", rule=f"{effect} rule", scope="project",
            action_class=action_class, effect=effect,
        )
        r = await protocols.match("test-protoc-core", [Action(tool="Bash", action_class=action_class)])
        assert r[0].effect == effect, f"{action_class} -> {r[0].effect}"
    print("PASS: effect branches inform/ask/deny/none")

    # --- Test: a trailing '.*' action_class matches the whole family ---
    # The prefix is everything before the FIRST dot (matching the real
    # vocabulary: 'db.query'/'db.ddl'/'db.grant' all share family 'db.*'),
    # so the test action_class here must be single-dot too.
    await protocols.add_protocol(
        project="test-protoc-core", rule="Wildcard rule for test.*",
        scope="project", action_class="test.*", effect="inform",
    )
    wild_hit = await protocols.match(
        "test-protoc-core", [Action(tool="Bash", action_class="test.wildmember")]
    )
    assert wild_hit[0].via == "exact" and wild_hit[0].rule_title == "Wildcard rule for test.*"
    print("PASS: trailing '.*' action_class matches any subclass sharing that family")

    # --- Test: no rule anywhere -> effect='unknown' ---
    # A different family than 'test.*' above -- that wildcard now matches
    # anything starting with 'test.', so this needs its own namespace to
    # stay genuinely unmatched.
    unknown = await protocols.match(
        "test-protoc-core", [Action(tool="Bash", action_class="reallyunseen.action")]
    )
    assert unknown[0].effect == "unknown" and unknown[0].via == "none"
    print("PASS: unmatched action_class comes back effect='unknown'")

    # --- Test: vector fallback + cache, and invalidation on protocol_add ---
    # Unique per run (this test's own nodes are never cleaned up between
    # manual runs, same as the rest of this file's test-* residue) so a
    # repeat run doesn't find a prior run's own match and short-circuit the
    # "nothing matches yet" assertion below.
    # Not in the 'test.*' family either -- that wildcard (added above) would
    # otherwise win via exact prefix match before the vector step ever runs.
    vector_class = f"vectoronly.{uuid.uuid4().hex[:8]}"
    vector_action = Action(tool="Bash", action_class=vector_class)
    core_id = await db.resolve_project_id("test-protoc-core")

    # Nothing matches yet -> exact miss, vector miss (no candidate nodes at
    # all for this text) -> 'unknown', and a None-node_id row gets cached.
    before = await protocols.match("test-protoc-core", [vector_action])
    assert before[0].effect == "unknown"
    cached_row = await db.fetchrow(
        "SELECT node_id FROM protocol_vector_cache WHERE project_id = $1 AND signature_hash = $2",
        core_id,
        protocols._signature_hash(core_id, vector_action.signature),
    )
    assert cached_row is not None and cached_row["node_id"] is None, "expected a cached miss"

    # Add a protocol whose title is EXACTLY the action's signature (mock
    # embedding -> similarity ~1.0) but give it an unrelated action_class so
    # the EXACT match step still misses and this only comes through vector
    # search + protocol_add's own cache invalidation.
    added = await protocols.add_protocol(
        project="test-protoc-core", rule=vector_action.signature, scope="project",
        action_class="test.vectoronly.unrelated-for-exact-match", effect="ask",
    )
    after = await protocols.match("test-protoc-core", [vector_action])
    assert after[0].via == "vector", after
    assert after[0].node_id == added["node_id"]
    assert after[0].effect == "ask"
    print("PASS: vector fallback matches + protocol_add invalidates the stale cached miss")

    print("\nALL PROTOCOL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
