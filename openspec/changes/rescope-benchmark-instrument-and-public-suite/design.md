# Design — rescope: internal instrument + borrowed-neutrality public suite

## Decisions

- **Retirement is permanent, not deferred.** The recurring defect class is
  "the author configures the competitor" — it survived two full repair
  rounds and flipped direction each time. No engineering fixes
  self-certification; external governance has no owner. Therefore: no
  competitor renderer, adapter, or profile is ever authored in this
  repository again, for any suite. Single-product tracks stay; cross-product
  tables produced here do not return. (A future external owner would be a
  new change with its own governance design — not assumed here.)

- **Suite choice: LongMemEval-S, cleaned variant (Sep 2025).** Grounds:
  MIT license; actively maintained (ICLR 2025, cleaned refresh, V2 spinoff);
  the de-facto standard with published integrations (Zep 71.2% gpt-4o,
  Mastra 84.23%, Supermemory ~85.9%, Memoria 88.78%, Mem0 94.4% own
  harness); ships type-specific judge prompts (reproducible protocol); and
  two of its five abilities — knowledge updates and curated abstention —
  are the closest public proxies for exomem's supersession/calibration
  claims. LoCoMo rejected: dormant since 2024-08, CC BY-NC (blocks
  commercial number publishing), no knowledge-update category, unshipped
  eval code, and rerun disputes (same system 84%→58%) exceeding
  between-system differences. MemoryAgentBench (ICLR 2026, MIT; incremental
  document ingestion, conflict-resolution track) is the natural second
  adapter — watch item, not built now.

- **Exomem-only, beside owners' published figures.** We author only our own
  integration — best foot forward, identical posture to every vendor on the
  suite. Published tables cite competitor rows to their publishers with
  reader-model caveats; saturation honesty requires per-ability reporting
  and no aggregate (carries forward the existing no-aggregate rule).

- **New namespace `benchmarks/lme/`, membench untouched.** The lane reuses
  seams, not the corpus: the `MemoryAdapter` contract and the
  `exomem_local` product-surface core (setup env pinning, embeddings-load
  refusal, `op_capture_source` ingest, `op_ask_memory` search, native
  answer pack) minus the 223-line governance translation;
  `judge/backends.py` (`OpenAICompatBackend`/`ClaudeCliBackend`) as the
  reader seam; `environment.py` capture; the immutable-run-directory and
  failures-in-denominator discipline from `runner.py`. The
  `track_a_bridge.py` pattern (synthesize the ingest stream from a foreign
  corpus, then drive setup/ingest/search unchanged) is the ingester
  template. The corpus generator and oracle do NOT transfer — LongMemEval
  ships its own ground truth — and stay frozen under the
  internal-instrument contract.

- **Ingestion shape.** One isolated vault per question (LongMemEval's
  per-question haystacks make cross-contamination impossible by
  construction); one governed source per session via `op_capture_source`
  with the session timestamp in frontmatter and body text, so the temporal
  signal is where retrieval can see it. Product defaults ON: graph,
  compiled preference, active-state ranking (the audit found de-tuning
  these was a fairness defect against ourselves). Determinism pins
  (warmup/watchers/caches) stay; capability amputations do not.

- **Reader and judge.** Reader = gpt-4o over retrieved context, matching
  Zep's published configuration for comparability; exact model IDs
  recorded in the run environment. Judge = the official `evaluate_qa.py`
  protocol (GPT-4o, shipped prompts), unmodified — any local judge edit
  makes the run unpublishable. Judge-verdict spot-check reuses the
  `agreement.py` κ machinery on a blind sample.

- **Bounds, re-derived (~250 LOC), not ported.** A gold-evidence ceiling
  adapter (LongMemEval ships `answer_session_ids`) and a null-abstain
  floor, run under the same reader, so every published figure has a
  denominator. This is the oracle-ceiling lesson applied to the new lane:
  a dimension without a measured ceiling cannot distinguish product
  failure from harness failure.

- **Pilot before spend, spend before full run — two founder gates.**
  Stage 1: build + dry-run the pipeline with a no-network stub reader on a
  stratified 20-question pilot (all five abilities represented), measuring
  ingest wall-time (500 haystacks × ~40–50 sessions is the real cost — the
  pilot extrapolates it). Gate 1: founder approves metered API use
  (standing policy is subscription-only; batch eval is the recorded
  exception requiring explicit opt-in; estimated single-digit to low-tens
  of dollars for reader+judge over 500 questions). Stage 2: pilot with
  real reader+judge. Gate 2: founder reviews pilot scores/costs and
  approves the full 500.

- **Internal-instrument contract for Track B.** Purpose: product
  regression (retrieval floor, degraded-profile honesty) and compiled-path
  correctness (supersession chain, citation basis) against real product
  surfaces, always bounded by the oracle ceiling and null floor. The
  ledger keeps 4b.26/4b.30 (truthful labelling), fixes 4b.44 (compile plan
  in supersession order — release-byte stability is moot post-rescope) and
  4b.34 (Track D clock injection), and settles 4b.28 as a deliberate
  product-contract decision. Comparison-only items (4b.31, 4b.33, 4b.40,
  cross-contender half of 4b.29) close as retired-with-rationale.
  Latency and harness-mode abstention are withheld from any cross-contender
  surface permanently.

## Execution

fable-delegate lanes: Phase A (this change + docs + ledger + KB) is
Claude-side; Phase B (`benchmarks/lme/`) is a Codex implementer lane with a
fresh independent review; Phase C (4b.44, reporting withholdings, Track D
clock) is a cheap Codex lane. Benchmark runs only on a quiesced machine,
`CUDA_VISIBLE_DEVICES=""`, writable `HF_HOME`; no latency claims from this
machine; nothing pushes or goes ready-for-review without the founder's word.
