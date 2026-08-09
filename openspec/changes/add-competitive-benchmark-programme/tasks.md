# Tasks — add-competitive-benchmark-programme

Ledger discipline: check a box only with evidence (test output, artifact
path, or commit). Founder checkpoints are marked ⛳ and block what follows
them. Mission acceptance criteria (§14) close only from this ledger.

## 0. Governance & truth (W0)

- [x] 0.1 Phase-0 audit published — `docs/benchmarks/competitive-eval-audit-2026-08.md`
      (asset map; reusable/suspect/obsolete; validity-failure catalogue incl.
      new findings N1 leak / N2 dataset divergence / N3 supermemory-documents
      row; data-flow diagram; historical-untrusted migration; fairness
      exposure table)
- [x] 0.2 ADR accepted — `docs/adr/0002-competitive-eval-architecture.md`
- [x] 0.3 This change's proposal/design/spec deltas written (MODIFIED
      `public-suite-eval` + five ADDED capabilities)
- [x] 0.4 `openspec validate add-competitive-benchmark-programme --strict` green
- [x] 0.5 Fairness contract published — `docs/benchmark-fairness-contract.md`
- [x] 0.6 Pre-registration artifact committed —
      `benchmarks/epistemic/PREREGISTRATION.md` (14 families, assertion
      registry, acceptance predicates, decision gates G1–G5 with proposed
      thresholds) + sha256 recorded there
- [ ] 0.7 ⛳ Founder ratifies pre-registration thresholds (gates freeze at the
      first competitor run; later edits are dated amendments)
- [ ] 0.8 KB updated: 2026-08-08 REJECT audit + this programme's supersession
      filed as a research note (the KB thread currently ends at the
      2026-08-06 direction-lock)
- [ ] 0.9 Housekeeping surfaced to founder (stray root `test/` scaffold;
      stale merged bench worktrees) — propose, never auto-delete

## 1. Protocol substrate (W1 — lane: codex)

- [ ] 1.1 Red leak test observed FAILING against current code
      (`benchmarks/lme/fixtures/leaky.json` +
      `tests/test_lme_normalize.py::test_ingestion_payloads_carry_no_gold_labels`)
- [ ] 1.2 `benchmarks/protocol/models.py` + committed drift-gated schemas
      (event, case-gold, run-manifest, case-trace, readiness-report,
      probe-result, budget-ledger, equivalence-diff/-exception, gap-report)
- [ ] 1.3 `events.py` normalizer (NFC, content_sha256, ordinal identity,
      hashed upstream ids, timestamp_semantics) — turns 1.1 green with
      `lme/normalize.py`
- [ ] 1.4 `leakage.py` three-scope transport-level scanner (red/green fixtures)
- [ ] 1.5 `namespace.py` + `canary.py` (presence / cross-case / never-ingested)
- [ ] 1.6 `readiness.py` + `probes.py` (lexical / zero-overlap semantic /
      update-current-state; `inconclusive-by-design` outcome)
- [ ] 1.7 `budget.py` + `pricing.yaml` (reservation ledger, STOP sentinel,
      unknown-model refusal, approval records; property tests)
- [ ] 1.8 `manifest.py` + `trace.py` + `validity.py` (manifest before first
      provider call; report refuses non-terminal)
- [ ] 1.9 `snapshot.py` + projectors (exomem_vault, basic_memory_vault,
      supermemory_api via recorded fixtures/MockTransport) with evidence-cited
      FieldDeclarations
- [ ] 1.10 Privacy tests extended over all new trees (before content lands)

## 2. Direct lane + equivalence (W2+W3 — lane: codex)

- [ ] 2.1 `lme/providers/` package; exomem_direct from adapter.py (leak-free:
      neutral titles, no question_type tags)
- [ ] 2.2 `hybrid_rag_direct.py` (RRF k=60, bge-base, frozen config hash;
      fixture-embedder path for CI)
- [ ] 2.3 `basic_memory_direct.py` via sidecar; `supermemory_direct.py`
      (local first; sequential ingest, instant-dreaming disclosed,
      reaped_404, settings capture, verified cleanup); `null_direct.py`
- [ ] 2.4 Runner wired to manifests/traces/readiness/canaries/budget;
      report variant axis + `--offline` socket guard
- [ ] 2.5 `equivalence/subsets/lme-s-25.json` + rationale committed BEFORE
      any result exists (hash-ordered selection; 3×6 answerable + 7 abstention)
- [ ] 2.6 `equivalence/differ.py` (12 keys; null never equals) +
      `exceptions.yaml` (weaker-predicate-only, evidence, expiry) + fixture-mode
      gate green vs `perturbed-twin.json`

## 3. Guest lane: bm-bench (W4 — lane: codex + claude review)

- [ ] 3.1 Fork of upstream Basic Memory + branch `exomem-provider-v2`;
      provider single-sourced from `benchmarks/bmbench/provider/` with hash
      sync
- [ ] 3.2 Fix list red-first (no de-tuning; `exomem_flag_enabled()`;
      CLIP loaded; positive semantic verification; error-string→raise;
      config isolation; `BM_BENCH_PROTOCOL_TRACE_DIR`)
- [ ] 3.3 `sidecar.py` (their env, 3 endpoints) + `export.py` → protocol
      artifacts
- [ ] 3.4 `audit/bm-bench-audit.md` (their harness, adversarially)
- [ ] 3.5 Upstream PR-1 (fairness hardening, zero exomem content) + PR-2
      (cleaned-variant pin) opened; supermemory-provider defects filed as an
      upstream ISSUE with evidence
- [ ] 3.6 Upstream PR-3 (exomem provider) after 3.5 lands or stalls
      (fork-pinned fallback recorded)

## 4. Guest lane: MemoryBench (W5 — lane: claude executor)

- [ ] 4.1 Clone + `LOCKFILE.json` (commit/tree sha, bun pin, license hash);
      `setup --verify` refuses drift
- [x] 4.2 `audit/supermemory-provider-audit.md` (incl. ProviderPrompts diff)
      + `audit/harness-audit.md` — committed. Headline: their provider
      hardcodes limit:30 vs shared limit:10 + include.chunks (commit
      dad6d5d, vendor-authored); questionDate never plumbed; _abs
      abstention unroutable; failed questions excluded from accuracy.
      Upstream filing = founder decision (list at harness-audit.md end)
- [ ] 4.3 Recording proxy capturing their provider's actual traffic
- [ ] 4.4 TS providers (exomem via isolated REST service on ephemeral port;
      basic-memory via shared sidecar) + registration patch with verified
      diff hash
- [ ] 4.5 `export.py` (stages ingest,search only; missing_fields explicit)
- [ ] 4.6 25-case Exomem direct-vs-MemoryBench equivalence gate GREEN
      (mode=blocking) — prerequisite for every comparative run
- [ ] 4.7 Supermemory direct-SDK vs MemoryBench-provider spot-check
      (mode=report)

## 5. Epistemic State Bench (W6 — lane: claude executor + external review)

- [ ] 5.1 `epistemic/{schema,registry,assertions,catastrophic}.py`
      (unknown-assertion load error; discrimination tests on synthetic
      snapshots)
- [ ] 5.2 Families 1–9 fixtures + runs vs exomem + grep-markdown + no-memory
      (controls score non-trivially)
- [ ] 5.3 4b.18 structural-blinding fix + structure-swap test (hard-gates all
      judge use)
- [ ] 5.4 Competitor drivers (basic-memory, supermemory) + ⛳ independent
      adversarial review of every fairness packet and the glue diff BEFORE
      competitor runs
- [ ] 5.5 Operational families 10–14 (external edit, engine-off 13a/13b,
      cross-agent continuation, triage invalidation, downstream impact);
      cross-case residue clean
- [ ] 5.6 Judge–human κ on a blind sample (agreement machinery) before any
      judged number

## 6. Native + operational lanes (W7+W8 — lanes: claude + codex)

- [ ] 6.1 `meter_proxy.py` + local extraction endpoint (model pulled) —
      prerequisite for any cost row
- [ ] 6.2 8 journey fixtures; B1 controlled rows with fairness-matrix
      asymmetry entries; B2 native rows (verbatim shipped skills; capped
      neutral prompt; future-blind write agent with n-gram test; fresh
      answer agent)
- [ ] 6.3 `opsq/` measurement modules + manifests; fault-injection
      transparency (incl. exomem's own 4b.30 soft-degrade); heuristics
      labeled; no-aggregate/no-dashboard tests

## 7. Runs (W9) — every metered step ⛳-gated

- [ ] 7.1 Fixture tier across all lanes, zero spend
- [ ] 7.2 LongMemEval-S fetched + sha pinned; direct stub pilot
- [ ] 7.3 Supermemory local binary installed (isolated data dir, version
      pinned per bm-bench's documented pin)
- [ ] 7.4 ⛳ Metered 25-case tier under the recorded session cap (≤$25):
      exomem, hybrid-rag, bm-local (both harnesses),
      supermemory-local-controlled (+hosted iff key provided)
- [ ] 7.5 Official judging via pinned LongMemEval V1 checkout; labels
      ingested
- [ ] 7.6 Gap reports for everything not run (blocked rows with one-command
      paths)

## 8. Suites (W10)

- [ ] 8.1 `suites/registry.py` (LOCKFILE-or-GAP invariant) + `lme_v1`
      LOCKFILE (verifies `judge_io` UNVERIFIED flags → tested string)
- [ ] 8.2 LongMemEval-V2: pin official harness; 3-case offline fixture
      through THEIR harness with stub model; ≤25-case small tier only if
      budget allows
- [x] 8.3 STALE + MemOps licence/code recon → **both LOCKFILE** (full public
      releases verified: STALE MIT+CC-BY-4.0 incl. runnable CUP-Mem, found
      in paper Appendix G; MemOps MIT with no-adapter-interface caveat) +
      bonus pins: MemoryAgentBench (MIT, ICLR 2026, CR split only 8 rows)
      and the OIDA CC-BY-4.0 epistemic corpora. Clones at sibling
      `suite-*` checkouts; pins in `benchmarks/suites/*/LOCKFILE.json`

## 9. Reports + CI (W11)

- [ ] 9.1 `reports/` renderer (per-ability × variant; no aggregate; INVALID
      renders INVALID; refuses cross-provider latency from this host)
- [ ] 9.2 Fairness matrix + compat matrix (declared-vs-observed divergence a
      first-class finding)
- [ ] 9.3 Adversarial packet generator (assumptions, confounds,
      suspicious-win flags, challenge paths, pre-registration hash)
- [ ] 9.4 `consolidate.py` + offline guard (import-closure test)
- [ ] 9.5 Additive `benchmark-protocol` CI job (offline only; guarded
      `retrieval-eval` untouched); README lane map updated
- [ ] 9.6 One-command smoke / per-lane run / report-regeneration documented
      and exercised from a clean checkout

## 10. Strategy + external review (W12)

- [ ] 10.1 Market-map research (Supermemory, Basic Memory, Kumiho,
      Eywa/GEM/OIDA/STALE/MemOps) with shipped/documented/prototype/unverified
      vocabulary
- [ ] 10.2 `docs/strategy/exomem-competitive-strategy-2026-08.md` — thesis
      table, narrow market, flagship journeys, parity floor, what-not-to-build,
      Hosted role, derived-media policy, gates G1–G5 evaluated against
      pre-registered thresholds
- [ ] 10.3 ⛳ External adversarial review of the packet (independent
      reviewer); every material objection fixed or documented
- [ ] 10.4 Final owner report: the seven mission questions answered plainly

## Mission acceptance criteria (tracked; close only from evidence above)

| # | Criterion | Closes with |
|---|---|---|
| 1 | Existing code audited; old results labelled | 0.1 |
| 2 | Event/manifest/trace schemas tested | 1.2–1.8 |
| 3 | Gold-leakage tests pass | 1.1, 1.3, 1.4 |
| 4 | Contamination + cleanup tests pass | 1.5 |
| 5 | Exomem/BM readiness fails closed | 1.6, 2.3, 2.4 |
| 6 | SM readiness separates documents from memories/profile | 2.3 |
| 7 | 25-case direct-vs-MemoryBench equivalence green | 4.6 |
| 8 | Direct LongMemEval small subset end-to-end, official outputs | 7.2, 7.5 |
| 9 | MemoryBench runs exomem + BM + their SM on the subset | 4.4–4.6, 7.4 |
| 10 | SM SDK vs MemoryBench equivalence checked | 4.7 |
| 11 | Hybrid-RAG control on the same subset | 2.2, 7.4 |
| 12 | LMEv2 official harness calls exomem on fixtures/small tier | 8.2 |
| 13 | ≥14 ESB families specced+mapped+asserted; exomem runs; competitor runs where possible | 5.1–5.5 |
| 14 | Reports regenerate from artifacts only | 9.1, 9.4 |
| 15 | One-command smoke/run/report from clean checkout | 9.6 |
| 16 | CI runs unit + no-paid fixture suite | 9.5 |
| 17 | No secrets in artifacts/logs | 1.10 + privacy gate |
| 18 | No unrelated user work overwritten | worktree discipline (standing) |
