## 0. Intake And Baseline

- [ ] 0.1 Record the baseline on the live cell before any code lands: thirty novel hybrid recalls, thirty keyword recalls and thirty `projects`-filtered hybrid recalls over the direct transport with timing diagnostics, load average at or below 2.0, p50/p95 per series and per-stage medians; store the content-free artifact under the change and verify the two walk stages (`filter_eligibility`, `outside_kb`) are present in the filtered series.
- [ ] 0.2 Inventory every supported structured-filter field and record, per field, whether today's plan classifies it complete or unsupported and which index (semantic-unit sidecar, lexical catalogue, media sidecar) can answer it; verify the inventory lists every field the registry accepts.
- [ ] 0.3 Prepare one delegation packet per lane (1–5) from this change with base SHA, allowlist, red-first nodes, mutant matrix, exact gates with out-of-tree state roots and basetemp, and a fresh reviewer; verify every gate line executes once before dispatch.

## 1. Lane 1 — Walk Sentinel And Timing Completeness

- [ ] 1.1 Add red tests: a walk sentinel that counts directory enumeration of the knowledge-base scope during one real `find` and fails when it is non-zero, and an attribution test that drives the public leaf with timing enabled and asserts `sum(stage.ms) + unattributed_ms <= total_ms` and `unattributed_ms <= 0.15 * total_ms`; verify both fail on the current tree for a `projects`-filtered hybrid recall.
- [ ] 1.2 Make the stages table write-through from registered spans, add the per-stage `source` value, and span every remaining manual timing region named in the 2026-08-31 plan (`_find_semantic`, admission folded into `recall_projection`, the unit lane embed, the deep copies); verify the attribution test passes and the diagnostics carry a source for every stage.
- [ ] 1.3 Mutation proofs in a harness-owned copy: restore one manual timing write and require the attribution test red; remove the sentinel installation and require the walk test red; restore and verify green, Ruff and `git diff --check` clean.
- [ ] 1.4 Author-independent reviewer attacks the sentinel (can a walk hide behind a cached listing or a subprocess), the bounds, and the source vocabulary; apply corrections and require recheck before acceptance.

## 2. Lane 2 — Index-Backed Structured-Filter Eligibility

- [ ] 2.1 Add red tests: a `projects` filter, a `tags` filter, a `types` filter and an AND of a page clause with a unit clause each resolve with the walk sentinel at zero and return the same path set as the scan oracle on a fixture vault; a stale catalogue generation yields the typed warming outcome; a pending write is evaluated against its committed frontmatter; verify they fail before implementation.
- [ ] 2.2 Implement the page-metadata index in the lexical catalogue store (path, filterable frontmatter fields, content hash, generation), written by the existing catalogue fan-out component under the write receipt and rebuilt by the single-flight repair; extend `plan_index_candidates` to page clauses; route eligibility through the index for managed readers and keep the oracle for offline mode; verify `tests/test_structured_filters.py`, `tests/test_find_filters*.py` and the new tests pass with an out-of-tree state root.
- [ ] 2.3 Add an identity test that evaluates every filter in the inventory from 0.2 against both the index and the oracle on the fixture vault and requires equal sets; verify it passes and is model-free.
- [ ] 2.4 Mutation proofs: drop one field from the index writer and require the identity test red; answer a stale generation from the index and require the warming test red; restore and verify green.
- [ ] 2.5 Reviewer attacks correctness of `$in`, `$exists`, `NOT` and OR composition against the oracle, generation binding, and the pending overlay; apply corrections and require recheck.

## 3. Lane 3 — Exact Custody For Read-Side Caches

- [ ] 3.1 Add red tests: after one governed write the lexical corpus, eligibility catalogue and frontmatter cache report an exact update of that page's rows only (no rebuild counter increment); a receipt-less external edit still invalidates the scope on reconciliation; a burst of writes to distinct pages leaves every cache row equal to the page's current frontmatter; verify they fail before implementation.
- [ ] 3.2 Implement per-path invalidation seams on the substrate caches and apply each committed receipt's path set to them; keep whole-scope invalidation for receipt-less drift; verify the tests pass and `tests/test_read_after_write_visibility.py`, `tests/test_pending_recall_delta.py` and `tests/test_lexical_deferred_upsert.py` stay green.
- [ ] 3.3 Add an audit that compares cache rows against frontmatter after a burst and fails closed to a scope invalidation on any mismatch; verify the audit test passes and a seeded mismatch is caught.
- [ ] 3.4 Mutation proofs: skip a moved path's old row and require the burst test red; let a whole-scope key evict a receipt-covered cache and require the exact-update test red; restore and verify green.
- [ ] 3.5 Reviewer attacks moves, deletes, restart hydration, and an in-flight graph rebuild's ability to invalidate read caches; apply corrections and require recheck.

## 4. Lane 4 — Opt-In Widening And Surface Regeneration

- [ ] 4.1 Add red tests: default `scope="kb"` runs no widening stage and reports it skipped; the widening option runs one catalogue query over the index-resolved out-of-KB set with the sentinel at zero; a stale catalogue declines; the reserve never exceeds `limit - 1`; verify they fail before implementation.
- [ ] 4.2 Add the widening option to the `find` leaf and `ask_memory` (default off), implement the catalogue-backed reserve, and regenerate the MCP schema fixtures, the tool-surface contract, the hosted candidates and both Claude directory channels, the ChatGPT pending digest and the v1 release identities; verify `tests/test_mcp_schema_fidelity.py`, `tests/test_connector_guardrails.py`, `tests/test_hosted_epistemic_profile.py`, `tests/test_hosted_marketplace_release.py`, `tests/test_hosted_plugin_rendering.py` and `tests/test_lazy_imports.py` pass.
- [ ] 4.3 Mutation proofs: re-enable widening by default and require the default test red; let a stale catalogue fall through to the scan and require the decline test red; restore and verify green.
- [ ] 4.4 Reviewer attacks the surface change, the reserve cap, and the documentation of the behaviour change; apply corrections and require recheck.

## 5. Lane 5 — Integration, Gate And Live Evidence

- [ ] 5.1 Merge the accepted lanes into one integration worktree, rerun each lane's focused gate, and verify the aggregate diff stays inside the union of the lane allowlists.
- [ ] 5.2 Add `scripts/recall_latency_gate.py` with the three series from the contract, novel queries, warm caches, quiescence refusal above load 2.0, load recorded per percentile, content-free output, and `--check` against the fixed ceilings; add `tests/test_recall_latency_gate.py` pinning the ceilings, the refusal, and the series shape; verify the tests pass.
- [ ] 5.3 Run the full guard→mutant→test matrix of lanes 1–4 against the aggregate source copy and verify every node turns red under its mutant and green after restore; preserve the matrix results.
- [ ] 5.4 Run the full corpus on the merged tree with an out-of-tree basetemp and `--timeout=300`, Ruff, the privacy gate, `git diff --check` and `openspec validate --all --strict`; record commands, exit codes and SHAs, and attribute every failure.
- [ ] 5.5 Fresh integration reviewer over the aggregate diff with adversarial probes for result identity, stale-answer paths, and the surface change; apply corrections and require recheck.
- [ ] 5.6 After release and upgrade of the live cell, run the gate on a quiescent cell and record before/after against 0.1: hybrid p50 at or below 300 ms and p95 at or below 600 ms, keyword p50 at or below 120 ms, filtered hybrid p50 at or below 400 ms, zero walks; verify the artifact is stored under the change.
- [ ] 5.7 Update task checkboxes from evidence, sync delta specs into the canonical specs, run `openspec validate --all --strict`, archive with `openspec archive accelerate-governed-recall`, and validate again.
