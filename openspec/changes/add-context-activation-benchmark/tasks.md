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
- [x] 1.5 Publish the real graph/index and call the actual compiler for all eighteen fixtures; verify existing recall/precision/poison/budget thresholds and a failing mechanism-removal run, keeping oracle-packet tests separate. Delivered in `membench.utility.context_activation_product` and `tests/test_context_activation_real_compiler.py`: both trees publish the activation index, lexical catalogue and graph before a frozen binding and one `op_activate_context` call per fixture, scored by the unchanged scorer. Historical initial and Round 2 reports passed seven and five cases respectively, with their mechanism findings retained in the runbook. The corpus-v4 report passed nine of eighteen raw and ten under A8; the current corpus-v5 report (task 1.9) passes ten raw, eleven under A8 and eleven under v5; the audit remains red. Fresh current-runtime/report-equality and mechanism-removal checks reproduce that result, including the kill switch failing every positive case. Current per-case findings are in the executable audit and recorded report, not the earlier topology snapshot.
- [ ] 1.6 Add capture-to-activation integration with same-origin fan-out and interrupted resume from `close-memory-loop`; verify canonical readback and later fresh-session context without fixture-specific product rules.
      Later fresh-session context is measured by the pre-registered keyed continuity group (amendment A6, `membench.utility.context_activation_continuity`): four of four pass on corpus v4 under the v3 scorer (everything served, including anchors of every status, ambiguity, units, pointers and current state, must belong to a referent page on canonical identity; the v1 and v2 results are kept as history). Capture fan-out and ordinary-agent acceptance remain open.
      The interrupted-resume test now joins original receipt reuse and canonical readback to fresh activation using the complete committed title and its actual fact/unit provenance. This slice passes locally with independent review; delivery, same-origin fan-out and ordinary-agent initiation remain open. Scripted supported writes do not establish no-nudge success.
- [x] 1.7 Freeze and digest-bind canonical current-state projection eligibility and reference identities; verify precision retains every surfaced reference, poison cannot be hidden, fabricated projections earn no credit, and stale/missing bindings void product runs. Delivered in PR #1314 with independent review, adversarial regression tests and required CI.
- [x] 1.8 Audit the distinct Planning collection/item identity mismatch and missing topology/continuity prerequisites before interpreting the eighteen-case report as compiler quality evidence. Historical findings in `tests/test_context_activation_real_compiler.py` and the runbook included the 37-page corpus below the carry floor, unregistered categories, unit fragments, collection-versus-item Planning identity, turn-independent recent context and cold-start runs without continuity. Corpus v4 (amendment A1) clears the carry floor (147 indexed pages) and registers the gold notes' categories. Later product corrections resolve the item-identity defect and make T3, T4 and T7 pass; the current executable report retains the other case-specific reds. Those historical defects are not current blockers, and the separate four-case continuity group now exercises fresh-session context.
- [ ] 1.9 Land the v5 instrument (Hugo, 2026-10-05; design.md D9, A2, A4, A8, A10): the v5 scorer column beside raw v4 (a bound page's units count as that page in every channel; a partial-only abstaining twin is the run's one hedge), T1 re-authored as a valid negative twin on corpus v5 with its A10 exclusion removed, and the v5 report recorded beside the v4 one, which stays historical. Raw v4 stays computable. `must_include` stays case-sensitive (S2 not approved). Check this task only once the change is merged.

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
- [x] 4.2 Record the corrected product-path deterministic report with fixture/corpus/threshold digests and per-case duals; verify no oracle packet substitutes for compiler output and no earlier broken-corpus result is presented as current acceptance. The current corpus-v5 report is `docs/benchmarks/context-activation-product-2026-10-v5.json`, with raw and amended columns side by side (the corpus-v4 report `docs/benchmarks/context-activation-product-2026-09-v4.json` stays as history); fresh real-compiler report equality passes. It remains a red quality result, not acceptance. Earlier rounds and `docs/benchmarks/context-activation-product-2026-09.json` remain historical evidence, not the current baseline.
- [ ] 4.3 Next user-visible gate: reuse memory-loop observation for a rich-turn → capture/readback → publication → fresh-session useful answer journey through the installed public interface, without private harness hints. Retain actual activation arguments/packet, subsequent retrieval and first answer; include multiple relevant topics and a negative/ambiguous control. Attribute failures to delivery, selection, arguments, compiler or agent use; forced calls do not establish ordinary initiation. Label reconstructed dogfood replays separately from missing original packets. Preserve corpus/gold/scorer/thresholds and private evidence boundaries; paid multi-arm comparison remains separately opt-in.
- [x] 4.4 Update the existing runbook to label paid A1–A5 measurements deferred and optional; verify any later comparative report still uses `c6_win_for_a3` and `effective_bar_reading`, all controls, cost reservations and the original stopping criteria, without making that future run a delivery prerequisite. Delivered in PR #1314; no paid comparison is claimed.

- [ ] 4.5 Extend existing usage/observation accounting only where absent: calls/retries, publication latency, attributable background/storage work and correction/nudge/repeat-error burden. Retain phase/window/source/units/denominators, incomplete and unknown values, and separate simulated versus human effort. Reuse current ledgers; no new runner or aggregate score.
- [ ] 4.6 Add the first separately versioned native multimodal/correction journey from `close-memory-loop` 3.14, with original artifacts and model-free state/provenance checks. Prove stale replay and local-correction counterexamples; retain ordinary-agent trace evidence separately from scripted instrument checks. Freeze new scenario/metric identities without rewriting the eighteen-case corpus, gold, thresholds or historical reports.
- [ ] 4.7 Build out document and audio/video plus temporal/contradiction/authority failure coverage proportionally, reusing existing preservation and utility cases. Record genuinely exercised modality, storage and adapter coverage; synthetic held-out variations prevent fixture-specific product rules. Obtain baseline quality-cost vectors before preregistering new comparative trade-offs; paid/frontier runs remain separately selected and bounded, public suites optional except for their own comparative claims.

- [ ] 4.8 Extend 4.6 with the accepted substrate case: exact observation plus compiled interpretation, two corrections and product-readable history, current/historical fresh-session answers, unchanged-source replay and an out-of-scope counterexample. Add the generic creator-product and operation-authority/tenant-isolation variant only where it catches distinct boundary failures; distinguish synthetic interface proof from actual consumer integration. If a worker/model-switch arm is selected, retain source-independence and incorrect/stale proposal controls. Reuse 4.5's full quality-cost vector and existing native runner; no extra framework, automatic paid trial or gate on unrelated releases.

## 5. Delivery

- [ ] 5.1 `openspec validate --all --strict`; scoped suites green; privacy gate green;
      PR with actual compiler and ordinary-use evidence pointers; synchronize/archive only when required tasks are evidenced as delivered.
