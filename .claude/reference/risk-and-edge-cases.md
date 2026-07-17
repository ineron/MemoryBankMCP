# Risk Assessment & Edge Case Planning — detail for /workflow:plan

Read this before finalizing options when the change is high-impact or
high-risk (auth, schema/API contracts, security-sensitive, or anything
touching money/data integrity). For a small, low-risk change, skip this and
go straight to drafting options.

## Risk categories to check
- **High-impact changes**: database schemas, API contracts, authentication flows
- **Performance implications**: algorithm complexity, data volume, caching needs
- **Security considerations**: input validation, access control, data exposure
- **Compatibility concerns**: breaking changes, dependency updates, browser support

## Comparative analysis dimensions
When multiple options are close, weigh each on:
- **Correctness**: how well it solves the problem
- **Maintainability**: long-term code health impact
- **Performance**: runtime and resource implications
- **Risk level**: potential for introducing issues
- **Effort**: implementation and testing time

## Edge cases to plan for (recommended approach)
- Null/undefined/empty state handling
- Boundary conditions and limits
- Error scenarios and recovery paths
- Concurrent access considerations, if applicable
- Data integrity safeguards
