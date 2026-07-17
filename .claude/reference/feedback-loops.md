# Feedback Loop Analysis — detail for /workflow:understand

Read this only when `/workflow:understand` is being re-invoked because
`/workflow:execute` or `/workflow:plan` bounced back with an issue — not on
a fresh task. It replaces a full re-decomposition with a scoped
re-investigation.

## Returning from `/workflow:execute`
Focus on the specific blocker or discovery reported, not a full re-analysis:
- Review the documented issue from implementation
- Investigate the root cause of the integration problem
- Clarify the ambiguous requirement against the real constraints encountered

## Returning from `/workflow:plan`
- Re-examine the specific assumption that proved incorrect
- Validate the new constraint discovered during planning
- Research an alternative approach for the blocked strategy

## Efficiency rule
Target the investigation to the specific feedback — don't re-run the full
task decomposition from scratch. State clearly which findings are carried
over unchanged vs. updated by this pass.
