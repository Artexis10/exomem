## 0. Canonical contract ownership

- [x] 0.1 Sync the already-accepted `close-technical-memory-gaps` delta requirements into canonical specs so `product-e2e` has one main-spec owner before this change modifies it.

## 1. Red-first release contracts

- [x] 1.1 Add focused failing tests for the self-contained manual Records fixture and two-session Records phase in `scripts/e2e_product_loop.py`; record the failure before implementation.
- [x] 1.2 Add a focused failing CI workflow contract for the pytest/job ceilings, duration reporting, lane-specific JUnit, and always-run artifact upload; record the failure before workflow changes.
- [x] 1.3 Preserve the pre-change red evidence from Actions runs `31364355540`/`93379461964` and `31363972628`/`93378330587`, then exercise the same held-boundary, admission, boundary-state, and constrained-prune-budget conditions through semantic replacement tests.
- [x] 1.4 Add failing public-command and egress-validator tests proving `record_memory(action="inspect")` currently omits the accepted opaque Planning descriptor. Require `contract.plans` as an always-present list (including `[]`), at most 32 exact `{reference, query}` entries, canonical opaque references, exact `filters`/`limit` query keys, bounded plain-JSON filters, `1 <= limit <= HARD_ROW_CAP`, hostile/extra-value refusal, hidden/missing target parity, withheld nested query-link filtering, and denied collection/source non-disclosure.

## 2. Installed-product Records journey

- [x] 2.1 Add a self-contained chronological-log manifest, canonical log, and ordinary template/manual insertion helper to the product E2E temporary vault without importing repository test fixtures.
- [x] 2.2 Correct the existing governed inspection projection to include bounded opaque Planning descriptors; extend `_validate_record_inspection` to strictly reconstruct them while preserving missing/hidden target parity and link-typed query filtering; then require installed `record_memory` discovery and implement the first stdio session's manual query, guarded append, guarded targeted update, derived view, Planning descriptor, and recall-isolation assertions.
- [x] 2.3 Directly edit the canonical log while the server is stopped and implement second-session assertions for the visible edit, prior mutations, and a positive audit gap without silent repair.
- [x] 2.4 Preserve the existing no-auth HTTP harness as explicit local-owner mode. Launch installed `python -m exomem --transport http` on a second port with explicit temporary overrides for `EXOMEM_BASE_URL`, both GitHub client variables, username, positive numeric user ID, and JWT signing key. Raw-POST a protocol-valid JSON-RPC `tools/call` naming `record_memory` with MCP Accept/JSON content headers and no Authorization; require exactly 401, a Bearer challenge naming the local protected-resource metadata URL, and raw response bytes without disclosure. Do not accept a generic HTTP error, inherit real credentials, fabricate remote identity on the owner server, or claim ingress refusal executed command-level governance.

## 3. Deterministic semantic regressions

- [x] 3.1 Rewrite the twenty-write mutation test to prove non-committed `MUTATION_BUSY`, concurrent safe retry after release, complete canonical/index/log state, and no residue without changing the production timeout.
- [x] 3.2 Rewrite the per-vault Records boundary test with positive attempt/admission/release events and generous deadlock-only guards.
- [x] 3.3 Replace narrow/wide critical-section sleeps and millisecond thresholds with direct observation of mutation state at relation-review evaluation.
- [x] 3.4 Give the expired-checkpoint cleanup semantic test a test-only completion budget while preserving dedicated production-budget coverage.

## 4. Bounded CI evidence

- [x] 4.1 Add `--session-timeout=1500` between-test termination, retain the sixty-second per-item timeout, and add a thirty-minute hard GitHub job deadline to each lean Python lane.
- [x] 4.2 Print the slowest fifty tests above one second; include `${{ matrix.python-version }}` in both JUnit XML path and immutable artifact name; upload with `if: always()` and `if-no-files-found: warn` after ordinary pass/failure.
- [x] 4.3 Keep both Python versions and the serial test topology unchanged; document broader sharding/pruning as deferred rather than expanding this change.

## 5. Verification and review

- [x] 5.1 Run the focused E2E-helper, CI-contract, concurrency, Records-matrix, critical-section, and continuation-checkpoint tests.
- [x] 5.2 Run the installed-wheel product loop within its existing 240-second budget and verify both stdio sessions, HTTP refusal/lifecycle, and writer-lease phase.
- [x] 5.3 Run Records product/governance/recall acceptance, Ruff on changed Python/tests, `git diff --check`, and strict OpenSpec change plus canonical-spec validation.
- [x] 5.4 Run the full lean pytest suite once in a trusted writable test-state environment, then obtain an independent diff review and correct every attributable finding. The recorded run completed `8022 passed, 132 skipped` with one unrelated existing governance-overhead p95 failure; its isolated rerun passed (`mixed-search` p95 1.556ms, `structure-reduction` p95 3.027ms). The reviewer approved after all three attributable test-proof findings were corrected.
