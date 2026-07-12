## 1. Regression coverage

- [x] 1.1 Add focused server-run tests proving HTTP transports enable stateless mode.
- [x] 1.2 Prove stdio construction remains unchanged.

## 2. Restart-safe transport

- [x] 2.1 Configure remote HTTP runs with explicit FastMCP stateless transport.
- [x] 2.2 Document restart/failover continuity and the remaining OAuth boundary.
- [x] 2.3 Serve and test the OIDC discovery alias observed in the Codex reconnect flow.

## 3. Verification

- [x] 3.1 Run focused tests, Ruff, and strict OpenSpec validation.
- [x] 3.2 Run the lean suite (2,010 passed; eight unrelated Windows harness/encoding failures documented).
- [x] 3.3 Deploy once and prove repeated authenticated calls before and after a controlled restart.

### Live verification evidence

- Before restart, a freshly reopened Codex connection completed initialization and repeated authenticated `POST /mcp` requests with `200 OK` / `202 Accepted` responses.
- After the controlled 2026-07-12 19:00 Europe/Tallinn service restart, Exomem started in stateless mode and the client reopened five authenticated `GET /mcp` streams with `200 OK`; there was no `401`, stale-session `404`, discovery loop, or reauthorization prompt.
