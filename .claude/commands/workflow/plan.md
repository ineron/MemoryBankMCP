---
description: Analyze options and create implementation strategy
---
# Detailed Analysis & Strategy Planning

## Task
Think deeply about implementation strategy and develop comprehensive options.

$ARGUMENTS

## Process

1. **Technical deep dive** — validate assumptions against the code, check existing patterns/architectural constraints, review related tests. Use TodoWrite for complex analyses.
2. **Develop 2-3 viable approaches** — for each: method and key steps, alignment with project patterns, scope of changes, estimated complexity/timeline, specific risks.

**High-impact or high-risk change** (auth, schema/API contracts, security,
money/data integrity)? Read `.claude/reference/risk-and-edge-cases.md`
before finalizing options — it has the full risk-category and edge-case
checklist. For a small, low-risk change, skip it and go straight to Output.

## Output

### Implementation Options
Present 2-3 options:

**Option N: [Name]**
- **Approach**: implementation method
- **Key Changes**: files/components affected, new patterns/dependencies
- **Pros** / **Cons**
- **Risk Level**: Low/Medium/High with justification
- **Estimated Time**

### Recommendation
**Recommended Approach**: Option [X], with justification (fit, alignment with architecture, risk mitigation)

### Next Steps (upon approval)
1. Save the approved plan: `memory_upsert(project, kind="plan", title=..., body=<selected option, rationale, risks, success criteria>, topic=["plan"])`
2. Implement via `/workflow:execute`

If more analysis is needed, return to `/workflow:understand`; if there's a
significant blocker, discuss it with the user directly rather than picking
an option unilaterally.

---
**⚠️ Wait for explicit user confirmation before proceeding to implementation.**

## Guidelines
- Present genuine alternatives, not strawmen; include "do nothing" if status quo is viable
- Be transparent about trade-offs/uncertainties; consider both immediate and long-term impact
- Reference specific code when relevant
