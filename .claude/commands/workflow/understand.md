---
description: Understand context and perform initial task analysis
---
# Context Review & Initial Assessment

## Memory Bank Context

Do not call `memory_search` or `memory_get` directly, and do not read any
memory-bank files (there are none — the memory bank is the Postgres MCP
server now). Instead, dispatch the **`memory-scan`** subagent
(`.claude/agents/memory-scan.md`) with the project slug (from
`.claude/settings.json`) and the task description/topic from `$ARGUMENTS` or
the task's `Topic` column if this was picked from `/start`'s table.

The subagent reviews candidate nodes in its own disposable context and
returns only a synthesized brief — that's what keeps scanned-but-irrelevant
material out of this session. Treat its brief as the ground truth for "what
do we already know"; do not second-guess it by re-running `memory_search`
yourself unless the brief explicitly says it found nothing and you need a
differently-worded query.

If the task spans multiple areas, it's fine to dispatch `memory-scan` more
than once with different queries rather than one broad query — narrow
queries produce tighter, more useful briefs.

## Task
Think through the task requirements and current project context to establish comprehensive understanding.

$ARGUMENTS

## Process

1. **Decompose** — core requirements, explicit/implicit constraints, scope, assumptions to validate.
2. **Discover context** — Task agent for broad pattern searches, Grep/Glob for specific files, read relevant configs/docs, check git history if evolution matters.
3. **Analyze** — map current state and components, identify dependencies/integration points, spot existing patterns and contradictions.
4. **Synthesize** — connect findings to project docs/patterns/constraints and the current focus.

**Returning here because `/workflow:execute` or `/workflow:plan` bounced
back with an issue** (not a fresh task)? Read
`.claude/reference/feedback-loops.md` first — it has the scoped
re-investigation approach so you don't redo the full decomposition above.

## Output

**Task Understanding:** requirements, constraints/assumptions, scope boundaries
**Current State:** relevant files/components, existing patterns, dependencies
**Initial Assessment:** complexity, potential challenges, what needs deeper investigation
**Next Steps:** recommended path (→ `/workflow:plan` for complex tasks, → `/workflow:execute` for simple/clear ones) — ask the user directly if something needs clarification first

## Guidelines
- Focus on understanding, not solutioning; scale effort with task complexity
- Use TodoWrite for multi-step exploration across several unknown areas
- Acknowledge when more investigation is needed rather than guessing
