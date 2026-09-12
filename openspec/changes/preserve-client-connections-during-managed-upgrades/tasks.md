## 1. Contract and ingress

- [x] 1.1 Independently review the lifecycle design and resolve blocking findings; verify the OpenSpec change validates strictly.
- [x] 1.2 Write failing admission/forwarding tests, then implement bounded queuing and finite-response drain in `service_ingress.py`; verify exact-once dispatch, queue exhaustion, request limits, header preservation and disconnect ownership.
- [x] 1.3 Preserve authenticated standalone GET/SSE across upstream generations; verify POST streams are drained and never reattached, and invalid credentials remain denied.

## 2. Worker lifecycle and control

- [x] 2.1 Write failing process/control tests, then implement the private worker socket, owner-only control, lifecycle lock and process-tree stop proof; verify a surviving or escaped child blocks replacement.
- [x] 2.2 Implement serialized drain/stop/migrate/start/readiness transitions and durable active/recovery records; verify drain abort, migration/startup failure, control disconnect and resume after supervisor restart.
- [x] 2.3 Implement immutable release staging and operator status/upgrade/resume commands; verify staging failure never pauses the old service and an active interpreter is never modified in place.

## 3. Managed service integration

- [x] 3.1 Wire the private worker runner and Linux opt-in installer/template plus managed upgrade routing; verify existing direct HTTP, stdio and legacy script tests remain green and managed installs cannot be overwritten by the legacy path.
- [x] 3.2 Document initial enablement, regular upgrades, budgets and failure recovery; verify the commands against an isolated user-service fixture.

## 4. Delivery evidence

- [x] 4.1 Exercise one real entered authenticated FastMCP Client context before, during and after process replacement in auto and legacy modes; verify the same bearer, keepalive, GET/SSE, in-flight mutation and exactly-once counters.
- [x] 4.2 Obtain independent code/security review and final behavior verification; resolve findings and rerun affected checks.
- [ ] 4.3 Run the completion-boundary full test corpus, lint, public-artifact privacy and strict OpenSpec validation; deliver through a ready PR and verified merge.
- [ ] 4.4 Synchronize the final delta and archive through OpenSpec after merge evidence, validating before and after closure; verify the owned worktree and branch are retired safely.
