## 1. Coordination Core

- [x] 1.1 Add pure-logic tests for lease configuration, coordinator responses, ownership errors, fencing, and default-off behavior
- [x] 1.2 Implement environment-backed lease configuration and the provider-neutral HTTP coordinator client
- [x] 1.3 Implement a transactional SQLite reference coordinator with acquire, renew, release, and status operations

## 2. Common Mutation Boundary

- [x] 2.1 Add tests for read bypass, writer execution, follower refusal, coordinator outage, and stale-token refusal
- [x] 2.2 Implement the shared command invoker and durable local idempotency records
- [x] 2.3 Route MCP, REST, and CLI registry commands through the shared invoker and map stable error statuses

## 3. Operations and Lifecycle

- [x] 3.1 Add the read-only `coordination_status` command consistently across MCP, REST, and CLI
- [x] 3.2 Add server lease renewal and graceful release lifecycle behavior
- [x] 3.3 Add the runnable self-hosted reference coordinator service and document its HTTP contract and deployment configuration

## 4. Verification

- [x] 4.1 Add integration tests covering single-writer exclusivity, expiry takeover, retry idempotency, surface metadata, and status redaction
- [x] 4.2 Run targeted tests, full non-embedding tests, and Ruff; resolve regressions
