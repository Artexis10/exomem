## 1. Regression Tests

- [x] 1.1 Add clock-controlled red tests proving `patch_frontmatter` and `batch_replace` fail across a validation/commit tick on stale, current, and missing `updated:` values.
- [x] 1.2 Add cross-kind exact-byte tests asserting validation `after_hash` equals committed Markdown for all seven advertised edit kinds.
- [x] 1.3 Add red tests for the explicit `relation_review_hash` response, validation with review intent but no hash, and strict commit mismatch rejection.
- [x] 1.4 Add a red fill-row validate/review/commit recovery test.
- [x] 1.5 Add red surface tests for route-correct remediation, bootstrap and tool relation examples, and semantic errors without a spurious missing suffix.
- [x] 1.6 Add an Aberdeen-shaped end-to-end `replace_string` test that appends body sections and four observation bullets through the stale-disposition round-trip.

## 2. Deterministic Existing-Edit Handshake

- [x] 2.1 Move bounded reviewed-stamp reuse into `semantic_writes` and use it in the shared edit, batch, frontmatter, and fill-row paths.
- [x] 2.2 Return direct before/after hashes and the `relation_review_hash` compatibility alias from existing validation.
- [x] 2.3 Let validation-only review intent obtain the canonical review hash while keeping commit-time hash matching strict.
- [x] 2.4 Add validation and transition/review guard support to the fill-row edit kind.

## 3. Client Contract Repair

- [x] 3.1 Emit route-specific relation-disposition remediation using only parameters in the relevant public schema.
- [x] 3.2 Document the existing-edit review round-trip and accepted typed-relation bullet in full bootstrap and `edit_memory` discovery.
- [x] 3.3 Preserve semantic error codes and reasons without marking `semantic` as a missing argument.

## 4. Verification And Delivery

- [ ] 4.1 Run the focused regression suite, OpenSpec strict validation, Ruff, and the project test suite with embeddings disabled.
- [x] 4.2 Run an independent code review and verifier pass; address confirmed findings and rerun affected tests.
- [ ] 4.3 Commit the intended scope, integrate current remote main safely, push, and open a ready Conventional Commit pull request noting which Bugs 1–5 were already fixed on main.
