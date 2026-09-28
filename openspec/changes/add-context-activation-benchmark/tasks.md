# Tasks: add-context-activation-benchmark

Checked tasks record the delivered instrument, not proof that the corpus exercised the real compiler. The required completion path now includes product-shaped fixture/publication tasks and observed ordinary use. Paid comparative runs are explicitly deferred; they require a separate future authorization and do not block delivery.

## 1. Fixtures and corpus

- [x] 1.1 Red: `tests/test_context_activation_fixtures.py` — the fixture manifest
      lists nine cases and nine twins with gold, poison, roles, must-include,
      must-exclude, expected status and a reminder turn; digests are stable; editing a
      gold list changes the digest; no fixture turn appears verbatim in any corpus page.
- [x] 1.2 Author the fixture manifest (C1–C9, T1–T9) and the seeded synthetic corpus
      generator beside `benchmarks/epistemic/corpora/` (entities with aliases, hubs,
      a resource profile with a Records collection whose latest item makes it
      unavailable, a supersession chain, an ambiguous domain triple, ~200 distractor
      evidence/transcript pages for C9).
- [x] 1.3 Author the private snapshot builder (local only): digest-pinned copy of a
      vault with meta-note exclusion listed in the snapshot manifest; never committed;
      privacy gate passes on the repository.
- [x] 1.4 Rebuild fixtures through normal supported writers with canonical entity/hub/Records/Planning structure; verify structural prerequisites, isolated state and new corpus digests before retrieval. Delivered in PR #1312 with independent review and required CI; this establishes corpus construction, not complete topology or compiler acceptance.
- [x] 1.5 Publish the real graph/index and call the actual compiler for all eighteen fixtures; verify existing recall/precision/poison/budget thresholds and a failing mechanism-removal run, keeping oracle-packet tests separate. Delivered in `membench.utility.context_activation_product` and `tests/test_context_activation_real_compiler.py`: both trees publish the activation index, lexical catalogue and graph before a frozen binding and one `op_activate_context` call per fixture, scored by the unchanged scorer. The audit is red today (seven of eighteen pass); the kill switch fails every positive case and each passing, load-bearing fixture fails without its pre-registered mechanism. C6, T1, T2 and T9 are recorded as not load-bearing on this corpus. Round 2 on corpus v4 (amendment A1): five of eighteen pass raw and five amended; C6, T1, T2 and T9 each fail with the naming gate removed and are load-bearing.
- [ ] 1.6 Add capture-to-activation integration with same-origin fan-out and interrupted resume from `close-memory-loop`; verify canonical readback and later fresh-session context without fixture-specific product rules.
      Later fresh-session context is measured by the pre-registered keyed continuity group (amendment A6, `membench.utility.context_activation_continuity`): four of four pass on corpus v4. Capture fan-out and interrupted resume remain.
- [x] 1.7 Freeze and digest-bind canonical current-state projection eligibility and reference identities; verify precision retains every surfaced reference, poison cannot be hidden, fabricated projections earn no credit, and stale/missing bindings void product runs. Delivered in PR #1314 with independent review, adversarial regression tests and required CI.
- [x] 1.8 Audit the distinct Planning collection/item identity mismatch and missing topology/continuity prerequisites before interpreting the eighteen-case report as compiler quality evidence. Recorded as executable findings in `tests/test_context_activation_real_compiler.py` and the runbook: non-anchor gold notes on a corpus the retrieval carry refuses (37 indexed pages, minimum 100), unregistered observation categories that no role lane serves, unit fragments that recall no gold note, the collection-versus-item Planning identity, turn-independent recent context, and cold-start runs with no continuity path exercised. The corpus repair and the product rulings these imply are not made here. Corpus v4 (amendment A1) clears the carry floor (147 indexed pages) and registers the gold notes' categories; the Planning identity, T4 and T7 stay pinned as product reds (A3, A5).

## 2. Deterministic scorer

- [x] 2.1 Red: `tests/test_context_activation_audit.py` — per-case × anchor-kind
      recall/precision with poison; twin false activation split by status; abstention;
      supersession marking; token and latency bounds; duals present; no aggregate
      field; mechanism-removal test red when the compiler is disabled; void run when a
      manifest lacks any digest.
- [x] 2.2 Implement the scorer over packets (`activate_context` output or an oracle
      packet file) and the report writer (per case, per class, duals, no aggregate).

- [x] 2.3 Credit a superseded ancestor that the packet carries with `lifecycle:
      superseded` and its successor named as non-poison in the C8 scorer (the compiler
      contract allows marking or omission; the v0 scorer credits both paths).

## 3. Agent arms under `f32`

- [x] 3.1 Red: existing `f32` variants byte-identical (seeds, oracles, paired
      outcomes) after the tuple extension; new variants bind cases to seeded action
      worlds; five arms pair on one episode identity; harness faults → blocked.
- [x] 3.2 Implement the context-activation variants and arms (A1 control, A2 raw, A3
      compiler via injected packet or mandated call, A4 nudged, A5 oracle packet from
      the fixture's hand-written packet) reusing `f27_replay.py`; rotated arm order;
      manifest digests; cost cap and per-episode reservation.
- [x] 3.3 Implement the reminder-turn test, the blind extraction prompt with the
      model-free gold/poison intersection, and the blind rubric input; document the
      grader blinding.
- [ ] 3.4 Add subscription Codex execution and usage telemetry through the existing agent-arm seams; verify configured model/effort, bounded execution, token-counter provenance, incomplete attempts, unknown charges and no metered fallback, then perform a bounded authorized smoke run.

## 4. Baseline runbook and measurements

- [x] 4.1 Write `docs/benchmarks/context-activation.md`: order of measurement
      (naive latency → A5 → A1 → A2/A4 → deterministic baseline), quiesced-cell and
      nonce rules, n = 1 baselines, n = 5 comparison, stopping criteria verbatim.
- [x] 4.2 Record the corrected product-path deterministic report with fixture/corpus/threshold digests and per-case duals; verify no oracle packet substitutes for compiler output and no earlier broken-corpus result is presented as current acceptance. Recorded in `docs/benchmarks/context-activation-product-2026-09.json`, bound to the fixture-set, threshold and logical corpus digests; the test compares it with a fresh product-mechanism run and fails when it is stale. It is a red result, not acceptance. Round 2 is recorded in `docs/benchmarks/context-activation-product-2026-09-v4.json` with the raw and amended columns side by side; the v3 report stays as history.
- [ ] 4.3 Record ordinary own-use or authorized replay evidence for first-response usefulness and no user reminder, with private evidence retained outside the repository; verify forced-call transport success is distinguished from agent initiation.
- [x] 4.4 Update the existing runbook to label paid A1–A5 measurements deferred and optional; verify any later comparative report still uses `c6_win_for_a3` and `effective_bar_reading`, all controls, cost reservations and the original stopping criteria, without making that future run a delivery prerequisite. Delivered in PR #1314; no paid comparison is claimed.

## 5. Delivery

- [ ] 5.1 `openspec validate --all --strict`; scoped suites green; privacy gate green;
      PR with actual compiler and ordinary-use evidence pointers; synchronize/archive only when required tasks are evidenced as delivered.
