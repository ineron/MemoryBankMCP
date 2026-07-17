---
description: Execute planned implementation with quality checks
---
# Implementation & Quality Assurance

## Task
Implement the approved plan (or direct task) with focus on quality.

$ARGUMENTS

## Process
- Review the approved plan/requirements; confirm correct branch, no conflicts. Use TodoWrite for complex work.
- Follow existing conventions/patterns; implement incrementally, validating between changes; MultiEdit for cohesive multi-file changes.
- Test after each component; check for compile/syntax/type errors as you go; handle edge cases.
- Run project quality tools (lint, typecheck, tests, build) and validate manually — no leftover debug code/TODOs, error messages are user-friendly, implementation matches requirements.

## If blocked mid-implementation

| Situation | Do this |
|---|---|
| Requirements unclear / ambiguous edge case | Pause → document the gap → `/workflow:understand` |
| Found a better approach (existing utility, cleaner design) | Document it → `/workflow:plan` to evaluate |
| Integration conflict (dependency, breaking change) | Minor: continue with a documented workaround. Major: `/workflow:understand` (context) or `/workflow:plan` (architecture) |
| Quality checks failing | Fix first, within the current approach. If it persists: `/workflow:plan` (approach) or `/workflow:understand` (constraints) |

## Output

**Changes:** files modified, key implementation decisions
**Quality:** results of lint/typecheck/tests/build
**Next:** ✅ done → `/workflow:update-memory` for significant changes · 🔄 blocked on clarification → `/workflow:understand` · 🔀 better approach found → `/workflow:plan` · ⚠️ other blocker → document it and return to the appropriate phase
