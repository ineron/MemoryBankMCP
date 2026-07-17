#!/usr/bin/env python3
"""Memory Bank installer: point it at a project directory, it does the rest.

Automates the exact sequence that's been done by hand for every project
connected so far (StayHugCRM, ledgyx-landing, ledgyx-admin-ui, pg_ilib,
pg_igraph, pg_ipl):

    1. Back up the project's existing .claude/ (if any) — never overwrite
       without a copy, especially since most of these projects don't
       git-track .claude/ or memory-bank/ at all.
    2. Drop stray backup artifacts (*.back, *.old) some editors/linters
       leave behind in .claude/commands/.
    3. Copy this repo's canonical commands/agents/claude-memory-bank.md in.
    4. Write .claude/settings.json (slug/name/group), dropping any legacy
       "memory"/"hooks" keys from the old file-based design.
    5. Write .mcp.json pointing at this server (absolute path — the server
       is shared across projects, so a relative path would break once
       copied elsewhere).
    6. Register the project (and group, if given) in the shared DB.
    7. If memory-bank/*.md exists: dry-run the import first (no embeddings,
       no DB writes — just counts/warnings), ask for confirmation, then run
       it for real, then rename memory-bank/ aside (never delete).

Usage:
    server/.venv/bin/python server/scripts/install.py /path/to/project \\
        [--slug SLUG] [--name NAME] [--group GROUP] [--yes] [--no-import]

Safe to re-run: project registration is idempotent (upsert), and if
memory-bank/ was already renamed aside by a previous run, the import step
is simply skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SERVER_DIR = SCRIPT_DIR.parent
REPO_ROOT = SERVER_DIR.parent
CANONICAL_CLAUDE = REPO_ROOT / ".claude"
SERVER_VENV_PYTHON = SERVER_DIR / ".venv" / "bin" / "python"

sys.path.insert(0, str(SERVER_DIR))

from memory_mcp.server import memory_import, project_create, project_group_create  # noqa: E402


def _today() -> str:
    return date.today().strftime("%Y%m%d")


def backup_and_replace_claude(project_dir: Path) -> None:
    claude_dir = project_dir / ".claude"
    if claude_dir.is_dir():
        backup_dir = project_dir / f".claude.legacy-backup-{_today()}"
        if not backup_dir.exists():
            shutil.copytree(claude_dir, backup_dir)
            print(f"  backed up existing .claude/ -> {backup_dir.name}/")
        for stray in list(claude_dir.rglob("*.back")) + list(claude_dir.rglob("*.old")):
            stray.unlink()
            print(f"  removed stray artifact: {stray.relative_to(project_dir)}")

    (claude_dir / "commands").mkdir(parents=True, exist_ok=True)
    shutil.copytree(CANONICAL_CLAUDE / "commands", claude_dir / "commands", dirs_exist_ok=True)
    shutil.copytree(CANONICAL_CLAUDE / "agents", claude_dir / "agents", dirs_exist_ok=True)
    if (CANONICAL_CLAUDE / "reference").is_dir():
        shutil.copytree(CANONICAL_CLAUDE / "reference", claude_dir / "reference", dirs_exist_ok=True)
    shutil.copy2(CANONICAL_CLAUDE / "claude-memory-bank.md", claude_dir / "claude-memory-bank.md")
    print("  commands/, agents/, reference/, claude-memory-bank.md installed")


def write_settings_json(project_dir: Path, slug: str, name: str, group: str | None, description: str) -> None:
    import json

    settings = {
        "project": {
            "slug": slug,
            "name": name,
            "group": group,
        }
    }
    if description:
        settings["project"]["description"] = description

    settings_path = project_dir / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote .claude/settings.json (slug={slug}, group={group})")


def write_mcp_json(project_dir: Path) -> None:
    import json

    mcp_config = {
        "mcpServers": {
            "memory-bank": {
                "command": str(SERVER_VENV_PYTHON),
                "args": ["-m", "memory_mcp.server"],
            }
        }
    }
    (project_dir / ".mcp.json").write_text(json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8")
    print("  wrote .mcp.json")


def read_existing_project_meta(project_dir: Path) -> dict:
    """Best-effort read of an existing settings.json for name/description,
    so re-running the installer (or connecting a project that already has
    the old file-based settings.json) doesn't lose that context."""
    import json

    settings_path = project_dir / ".claude" / "settings.json"
    if not settings_path.exists():
        return {}
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data.get("project", {})


async def register_project(slug: str, name: str, group: str | None) -> None:
    if group:
        await project_group_create(slug=group, name=group)
        print(f"  ensured project group '{group}'")
    await project_create(slug=slug, name=name, group_slug=group)
    print(f"  registered project '{slug}'" + (f" in group '{group}'" if group else ""))


async def run_import(project_dir: Path, slug: str, assume_yes: bool) -> None:
    memory_bank = project_dir / "memory-bank"
    if not memory_bank.is_dir():
        print("  no memory-bank/ directory found — nothing to import")
        return

    print(f"  found memory-bank/ — running dry-run preview...")
    preview = await memory_import(project=slug, path=str(memory_bank), dry_run=True)
    by_kind: dict[str, int] = {}
    for item in preview["created"]:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    print(f"  would create {len(preview['created'])} nodes: {by_kind}")
    print(f"  would create {len(preview['edges'])} edges")
    if preview["warnings"]:
        print("  warnings:")
        for w in preview["warnings"]:
            print(f"    - {w}")

    if not assume_yes:
        answer = input(f"  Proceed with real import ({len(preview['created'])} embedding API calls)? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("  skipped — memory-bank/ left in place, re-run later to import")
            return

    print("  running real import (this makes real embedding API calls)...")
    summary = await memory_import(project=slug, path=str(memory_bank), dry_run=False)
    print(f"  imported {len(summary['created'])} nodes, {len(summary['edges'])} edges")

    backup_name = f"memory-bank.imported-{_today()}"
    if not (project_dir / backup_name).exists():
        memory_bank.rename(project_dir / backup_name)
        print(f"  renamed memory-bank/ -> {backup_name}/ (preserved, not deleted)")


async def main_async(args: argparse.Namespace) -> None:
    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(f"error: '{project_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    existing = read_existing_project_meta(project_dir)
    slug = args.slug or existing.get("name") or project_dir.name
    name = args.name or existing.get("name") or slug
    description = args.description or existing.get("description", "")
    group = args.group

    print(f"Installing memory bank for '{project_dir}' as slug='{slug}'" + (f", group='{group}'" if group else ""))

    print("\n[1/4] Backing up and replacing .claude/...")
    backup_and_replace_claude(project_dir)

    print("\n[2/4] Writing config...")
    write_settings_json(project_dir, slug, name, group, description)
    write_mcp_json(project_dir)

    print("\n[3/4] Registering project in the shared DB...")
    await register_project(slug, name, group)

    if args.no_import:
        print("\n[4/4] Skipping import (--no-import given)")
    else:
        print("\n[4/4] Checking for legacy memory-bank/ to import...")
        await run_import(project_dir, slug, args.yes)

    print(f"\nDone. '{slug}' is connected — open a Claude Code session in {project_dir} and run /start.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_dir", help="Path to the project to connect")
    parser.add_argument("--slug", help="Project slug (default: existing settings.json name, else directory name)")
    parser.add_argument("--name", help="Display name (default: same as slug)")
    parser.add_argument("--description", help="Project description")
    parser.add_argument("--group", help="Project group slug for cross-project scope=\"group\" search (default: none)")
    parser.add_argument("--yes", action="store_true", help="Skip the import confirmation prompt")
    parser.add_argument("--no-import", action="store_true", help="Only set up config/registration, skip memory-bank/ import entirely")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
