## 1. Executable Fixture Contract

- [x] 1.1 Add a red executable test that loads the checked pre-seeded payload and drives every note through real `remember` validation/commit plus exact `read_memory` readback in a fresh vault.
- [x] 1.2 Add a red regression proving a semantically invalid fixture note fails executable validation even when its JSON shape and payload digest are valid.
- [x] 1.3 Implement the transport-neutral fixture seeding state machine with strict draft-field, blocker, commit, and readback checks.
- [x] 1.4 Replace the v1 payload with a naturally authored v2 fixture, update review-case bindings, and make the executable tests pass without exemptions or contract changes.

## 2. Pre-Seal Bootstrap Ordering

- [x] 2.1 Add red bootstrap tests proving token exchange is mandatory and readiness/seed failures make zero reviewer-credential calls.
- [x] 2.2 Add red call-order tests proving `CELL_READY`, complete fixture seeding, and exact verification all precede sibling reviewer-credential issuance.
- [x] 2.3 Implement bounded owner-status polling with a staged-release reserve and content-free progress recording.
- [x] 2.4 Implement the authenticated JSON/SSE MCP adapter without persisting or printing bearer, cookie, fixture-body, or content-bearing response data.
- [x] 2.5 Wire the shared v2 seeder into `reviewer_bootstrap.run`, write the protected content-free seed receipt, and move both provider credential calls after successful verification.
- [x] 2.6 Add partial-progress and exact-mismatch tests so an ambiguous write never causes blind overwrite or credential sealing.

## 3. Deterministic Release Artifacts

- [x] 3.1 Rerender all directory packets bound to fixture v2 and verify generated packet bytes and fixture digests are current.
- [x] 3.2 Update focused operator documentation to state that bootstrap now prepares the fixture before the human clean-client runs.

## 4. Verification and Delivery

- [x] 4.1 Run focused fixture, semantic-write, marketplace-release, plugin-rendering, and reviewer-bootstrap tests with embeddings disabled.
- [x] 4.2 Run strict OpenSpec validation, Ruff on changed Python, generated-artifact checks, public-artifact privacy validation, and secret-pattern review.
- [ ] 4.3 Inspect the final diff, commit only the repair scope, integrate current `origin/main`, push, and open a ready Conventional-Commit PR with verification evidence.
- [ ] 4.4 After merge, cut a fresh release/candidate and reviewer window; complete genuine ChatGPT and Claude evidence before promotion, then continue to Paddle paid-flow proof and invitations.
