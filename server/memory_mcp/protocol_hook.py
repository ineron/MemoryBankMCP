"""PreToolUse hook: look up action protocols before Bash/Read/Edit/Write run.

    <server/.venv/bin/python> -m memory_mcp.protocol_hook

Registered in ~/.claude/settings.json (matcher "Bash|Read|Edit|Write"), so it
runs for every registered project on this machine, not just this one. Reads
a Claude Code PreToolUse payload from stdin, classifies the action
(protocols.extract_action), matches it against protocol_match rows
(protocols.match), and prints a hookSpecificOutput JSON:

  - effect='deny' on any action, or no rule found for any action ('unknown')
    -> permissionDecision: deny, reason names the action and (for 'unknown')
       tells the model to ask the user and call protocol_add.
  - effect='ask'  -> permissionDecision: ask + additionalContext with the rule.
  - effect='inform' -> additionalContext only, no permissionDecision (does
    NOT bypass the normal allow/ask/deny flow -- confirmed in the spike
    behind this file; see the plan this was built from).
  - effect='none' (or no actions classified at all) -> prints nothing.

FAIL OPEN, ALWAYS: any exception, timeout, or missing DATABASE_URL exits 0
with no output rather than blocking work over an infrastructure hiccup. Set
PROTOCOL_HOOK=off to bypass this hook entirely without editing settings.json.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import sys

# Absolute, not cwd-relative -- Claude Code launches hooks from whichever
# project repo the session is in, never from server/ (same reasoning as
# server.py / listener.py).
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from . import db, protocols  # noqa: E402 -- must follow load_dotenv

HOOK_TIMEOUT_SECONDS = 4.0
STATE_DIR = os.path.expanduser("~/.claude/hooks/state/protocols")


def project_slug_at(path: str) -> tuple[str | None, str | None]:
    """Nearest ancestor (inclusive) of `path` with .claude/settings.json
    carrying a non-empty project.slug. Same convention as
    ~/.claude/hooks/lib/cross_project_guard.py -- duplicated rather than
    imported since that lives outside this package."""
    cur = os.path.abspath(path)
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        settings_path = os.path.join(cur, ".claude", "settings.json")
        if os.path.isfile(settings_path):
            try:
                with open(settings_path) as f:
                    data = json.load(f)
                slug = (data.get("project") or {}).get("slug")
                if slug:
                    return slug, cur
            except Exception:
                pass
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None, None


def _is_excluded_path(path: str) -> bool:
    """Scratchpad files, plan files, and this session's own auto-memory
    notes are exempt -- routine internal bookkeeping shouldn't trip a
    protocol lookup (or worse, a block-and-ask) on every write."""
    if not path:
        return False
    abspath = os.path.abspath(os.path.expanduser(path))
    if "/scratchpad/" in abspath or abspath.endswith("/scratchpad"):
        return True
    if abspath.startswith(os.path.expanduser("~/.claude/plans/")):
        return True
    memory_glob = os.path.expanduser("~/.claude/projects/*/memory/*")
    if fnmatch.fnmatch(abspath, memory_glob):
        return True
    return False


def _seen_node_ids(session_id: str) -> set[int]:
    path = os.path.join(STATE_DIR, f"{session_id}.seen")
    try:
        with open(path) as f:
            return {int(line) for line in f if line.strip()}
    except Exception:
        return set()


def _mark_seen(session_id: str, node_id: int) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, f"{session_id}.seen"), "a") as f:
            f.write(f"{node_id}\n")
    except Exception:
        pass


def _render_rule(result: "protocols.ProtocolResult", session_id: str, seen: set[int]) -> str:
    if result.node_id in seen:
        return f"Protocol: {result.rule_title}"
    _mark_seen(session_id, result.node_id)
    body = f"\n{result.rule_body}" if result.rule_body else ""
    return f"Protocol: {result.rule_title}{body}"


async def _run(payload: dict) -> dict | None:
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd") or os.getcwd()

    candidate_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if candidate_path and _is_excluded_path(candidate_path):
        return None

    slug, root = project_slug_at(cwd)
    if not slug:
        return None

    try:
        await db.resolve_project_id(slug)
    except ValueError:
        return None  # this project isn't registered in the memory bank DB

    actions = protocols.extract_action(tool_name, tool_input, project_root=root)
    if not actions:
        return None

    results = await protocols.match(slug, actions)
    seen = _seen_node_ids(session_id)

    deny_reasons = [f"[{r.action.action_class}] {r.rule_title}" for r in results if r.effect == "deny"]
    unknown = [r.action for r in results if r.effect == "unknown"]
    ask_lines = [_render_rule(r, session_id, seen) for r in results if r.effect == "ask"]
    inform_lines = [_render_rule(r, session_id, seen) for r in results if r.effect == "inform"]

    if deny_reasons:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Protocol violation:\n" + "\n".join(deny_reasons),
            }
        }

    if unknown:
        classes = ", ".join(sorted({a.action_class for a in unknown}))
        sigs = "; ".join(a.signature for a in unknown)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"No action protocol found for: {sigs}. Ask the user how "
                    f"this class of action ({classes}) should be handled "
                    "(scope: project/group/global, effect: inform/ask/deny/"
                    "none), then call protocol_add(...) to record their "
                    "answer, and retry."
                ),
            }
        }

    if ask_lines:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "additionalContext": "\n".join(ask_lines),
            }
        }

    if inform_lines:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "\n".join(inform_lines),
            }
        }

    return None


async def _main_async(payload: dict) -> dict | None:
    try:
        return await asyncio.wait_for(_run(payload), timeout=HOOK_TIMEOUT_SECONDS)
    finally:
        await db.close_pool()


def main() -> None:
    if os.environ.get("PROTOCOL_HOOK") == "off":
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        result = asyncio.run(_main_async(payload))
    except Exception:
        sys.exit(0)

    if result:
        print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
