# Competitive evaluation audit — 2026-08

Status: **authoritative Phase-0 audit** for the competitive benchmark programme
(OpenSpec change `add-competitive-benchmark-programme`). Every claim below was
verified in code or artifacts during the 2026-08-09 audit, not inherited from
ledgers or summaries. Where a ledger belief was found stale, the correction is
stated explicitly.

Scope: all benchmark code and artifacts in this repository, plus read-only
inspection of three sibling checkouts — upstream Basic Memory
(`../basic-memory`), the Basic Memory fork carrying an Exomem benchmark
provider (`../basic-memory-exomem-provider`), and the Supermemory monorepo
(`../supermemory`).

---

## 1. Asset map

### In this repository (all landed on `main` via PR #390, squash `7a140a2`)

| Asset | Role | State |
|---|---|---|
| `benchmarks/membench/` (~20.9k LOC, 450 tests) | Track B internal instrument: bitemporal oracle, 24 template families (t00–t23), deterministic gates, compile plan, judge backends/blinding/handshake, environment capture, agreement (κ) machinery, Track C hook-activation, Track D journeys | Live, governed by the internal-instrument contract of `rescope-benchmark-instrument-and-public-suite` |
| `benchmarks/lme/` (1.6k LOC, 27 tests) | Exomem-only LongMemEval-S lane (`public-suite-eval`): pinned cleaned dataset, per-question isolated vaults, stub/API readers with metered + full-run founder gates, ceiling/floor bounds, per-ability no-aggregate report, official `evaluate_qa.py` command emission | Live. **Never run on real data** — no LongMemEval file exists on disk; all 27 tests are offline/stub |
| `benchmarks/corpus/` | 10 committed JSON Schemas + the single pinned release `v0.1-seed1` | Frozen under the internal-instrument contract |
| `benchmarks/runs/` (gitignored) | 57 membench run dirs, 2026-07-31 → 2026-08-08 | **All historical-untrusted** (§5) |
| `benchmarks/judge-agreement/` | Blind-sample sheets, judge-vs-gates probes | Partially stale (ledger 4b.45: probe artifact pins a pre-`f15f65f` corpus) |
| `scripts/graph_value_benchmark.py` + `docs/comparison-basic-memory-graph.md` | Graph-value comparison | **WITHDRAWN** both directions (2026-08-09); survives only as an exomem-only regression instrument per rescope task 3b.6 |
| `openspec/changes/add-memory-server-comparison-benchmark/` | Planned CLI-vs-server comparison | **Obsolete** — 0/8 tasks, purpose forbidden by the rescope change |
| `tests/test_membench_*.py`, `tests/test_lme_*.py` | Lean bench suites | Green, offline |
| CI (`.github/workflows/ci.yml`) | `retrieval-eval` job: golden retrieval gate, latency ceiling gate, semantic write-latency gate | **No membench or lme job exists** despite README claims — recorded as a gap, addressed by the programme's new additive `benchmark-protocol` job |

### Sibling checkouts (read-only)

| Asset | Role |
|---|---|
| `../basic-memory/benchmarks/` (`bm-bench`, package `basic_memory_benchmarks`) | Basic Memory's own harness: stages retrieval → qa → rejudge → diagnose → review.html; providers `bm-local`, `bm-cloud`, `mem0-local`, `zep-reference` (stub), `baseline-grep`, `baseline-fullcontext`, and a **Basic-Memory-authored `supermemory-local`**; LongMemEval/LoCoMo(+Penfield corrections)/ConvoMem converters; fixed in-repo answer/judge prompts; `claude -p` (plan-billed) and OpenAI-compatible runners; `--strict-providers` |
| `../basic-memory-exomem-provider` (branch `exomem-provider`) | Fork adding a working Exomem guest provider to bm-bench (warm MCP stdio, +390 LOC provider, +259 LOC tests) with a fair-play bugfix trail. **6 commits ahead, 156 behind upstream** — regenerate, do not rebase |
| `../supermemory` | Supermemory monorepo — **periphery only**: web console, MCP worker (hosted), docs, SDK shims. The engine is closed source; local = precompiled `supermemory-server` binary (port 6767, BYO extraction model); hosted runs proprietary extraction models. MemoryBench is a separate repo (`supermemoryai/memorybench`, MIT, TypeScript), documented here but not vendored |

---

## 2. Reusable / suspect / obsolete / missing

**Reusable, high confidence** (imported as libraries; never modified):
`membench/adapters/base.py` (capability contract; `AdapterUnsupported` vs
`AdapterEnvironmentError` — the invalid-vs-loss line; `NativeAnswer` closed
citations), `membench/environment.py` (two-tier BLOCKING/REPORTED capture),
`membench/judge/{backends,blinding,handshake}.py`, `membench/agreement.py`
(built, tested, **never executed**), `membench/scoring/gates.py::states_value`
and `GateStatus`, `membench/scoring/health.py` (three-tier honesty contract),
`membench/clock.py`, `membench/trackd/journeys.py` shapes,
`membench/adapters/track_a_bridge.py` (bridge pattern), the pydantic →
JSON-Schema export + drift-gate mechanism, the immutable-run-dir +
failures-in-denominator discipline, `benchmarks/lme/` wholesale, bm-bench's
LongMemEval loader with SHA-256 provenance, the fork's provider as a
**checklist**, `scripts/codex_task.sh`, and the privacy gate
(`src/exomem/public_artifact_privacy.py`).

**Stale ledger beliefs corrected by this audit** (verified in code):

- **4b.14 (bare-substring gate matching) is FIXED** — `scoring/gates.py`
  routes numeric values through the Decimal path against standalone number
  tokens and everything else through word-boundary literal patterns.
- **4b.8 (citations recall-only) is FIXED** — `gate_citations` measures
  precision and recall and returns `UNSUPPORTED` (never `PASS`) when
  precision is unverifiable.
- **Harness-authored provenance/abstention is half-fixed** — the
  `NativeAnswer` path exists and measured 4b.42's effect; the extractive
  answerer remains the default and the bounds adapters still author answers.
- **Still open and load-bearing: 4b.18** — judge blinding is defeated by
  frontmatter *structure* (key shape identifies the vendor with zero token
  hits). In any multi-product comparison this is fatal; the programme
  hard-gates every judged number behind a structural-blinding fix with a
  structure-swap test.

**Suspect (do not trust without re-derivation):** every historical
cross-contender figure in any document, in either direction; all 57 membench
run dirs (none has a verified environment; the newest was produced from a
dirty tree); the committed judge-probe artifact (4b.45); any claim about
judge quality (4b.18, 4b.23); `ExpectedRecord.gates` fields in published
expected records (4b.10/13/15/16 open).

**Obsolete:** `add-memory-server-comparison-benchmark` (unimplemented,
purpose retired); the comparative half of the graph-value benchmark;
membench's competitor adapters/renderers
(`adapters/basic_memory_local.py`, `native/basic_memory.py`, `graybox*`) —
retired by policy and left retired; sibling worktrees duplicating merged
work.

**Missing entirely:** any Supermemory integration on the Exomem side (none
has ever existed here); a MemoryBench checkout; any LongMemEval dataset on
disk; any executed LME run; LongMemEval-V2 anything; STALE/MemOps anything;
CI coverage for the bench suites.

---

## 3. Validity-failure catalogue

The authoritative long-form record is
`openspec/changes/expand-memory-proof-benchmark/tasks.md` §4b (45 items) and
`docs/memory-proof-benchmark-v01-findings.md`. The classes, with their
sharpest instances:

1. **Index/embedding readiness lies.** The harness never built Basic
   Memory's vector index (4b.41 — voided the first headline result);
   `EXOMEM_DISABLE_CLIP=1` zeroed *text* retrieval (4b.24); a 3.12→3.14
   interpreter change took retrieval 452 hits → 0 and was investigated as a
   product regression; Exomem soft-degrades to keyword when
   `sentence_transformers` is missing (4b.30 — correct product behavior,
   catastrophic benchmark behavior).
2. **Author-configured competitors.** The renderer fed Basic Memory 376
   oracle-normalised fact lines phrased in query vocabulary (voided the
   second headline result, in the opposite direction); the profile contract
   de-tuned Exomem specifically. This class recurred across two full repair
   rounds and flipped direction each time — the structural reason the
   2026-08-08 independent audit returned REJECT and the rescope retired
   self-authored comparison.
3. **Ingestion-lifecycle mismatch.** Track B ingested at raw-source altitude
   while scoring provenance chains that could not exist (4b.35: 205 sources,
   zero compiled notes, `ingested_into: []` on 204/204); the compile plan
   emitted conclusions in claim-id order so 46/88 supersession edges could
   not resolve (4b.44); knowledge time was never transmitted to providers.
4. **Corpus integrity.** Entity-name collisions produced byte-identical
   prompts with mutually exclusive expected answers (4b.32); four queries
   named the forbidden answer in the prompt (4b.21); the committed judge
   probe pins a superseded corpus (4b.45).
5. **Scoring-gate defects.** Substring firing (4b.14, now fixed), recall-only
   citations (4b.8, now fixed), gates scoring records they were never asked
   to judge (4b.36), declared-but-fictional gate names in published records
   (4b.10/13/15/16), provenance/abstention authored by the harness rather
   than the product.
6. **Judge drift/blinding.** Structural fingerprint defeats blinding (4b.18,
   open); 19/19 discrimination is an upper bound from clean one-sentence
   candidates (4b.23); judge disposition reversed three times before being
   scoped to gate-UNSUPPORTED rows only.
7. **Environment/reproducibility.** "A run directory cannot reproduce its own
   result" (resolved into `environment.py`); a Pillow patch bump flagged as
   corpus drift took down CI (4b.7, fixed by splitting corpus identity from
   environment provenance); GPU unusable under WSL2 → no valid latency claim
   from the dev machine (4b.40 withholds cross-contender latency
   permanently); Track D journeys changed verdict at midnight (4b.34).

### Prior bm-bench comparisons (fork, 2026-07-31 → 08-01) — all invalid

Eight runs, all on a **3-document / 4-query synthetic corpus** (never
LongMemEval), QA/judge stage never executed. Forensics from leaked run homes
in the fork: semantic search off in 3 of 5 Basic Memory homes (its default is
an implicit function of installed packages); the embedding model failed to
download in a 4th (43 s/query of retry sleeps, scored 0.000 with state
"ok"); `BASIC_MEMORY_CLOUD_MODE` merely unset while unknown projects
fall back to cloud routing; and Basic Memory's MCP returns error-guidance
*strings* with `isError=false`, which the adapter parsed as zero hits. Basic
Memory's honest range on that toy corpus was 0.500 (FTS-only) to 0.750
(hybrid); every published 0.000 was a misconfiguration artifact. Exomem was
never measured against a correctly configured Basic Memory.

### New findings from this audit (not in any ledger)

- **N1 — The direct LongMemEval lane leaks gold labels.**
  `benchmarks/lme/dataset.py` (`render_session`) ingests the raw session ID
  into provider-visible text, and on real LongMemEval-S evidence sessions
  carry an `answer_`-prefixed ID — Basic Memory's converter
  (`converters/longmemeval_to_corpus.py`) documents and neutralizes exactly
  this leak; ours does not. `benchmarks/lme/adapter.py` additionally writes
  `question_id` into note titles and `tags=[question_type]` (the category
  label) into frontmatter. No committed fixture can detect it because
  fixtures use neutral IDs. The lane has never run on real data, so no
  published number is tainted — but the first build item of the programme is
  a red test (`fixtures/leaky.json`) that fails against today's code.
- **N2 — Dataset identity diverges across harnesses.** The direct lane pins
  `longmemeval-cleaned`; bm-bench pins the *original* `longmemeval`;
  MemoryBench uses cleaned. Any cross-harness comparison without a variant
  repoint fails dataset-identity equivalence on day one.
- **N3 — bm-bench's Supermemory row is not a memory row.** Its provider
  posts to `/v3/search` (document/SuperRAG chunk search) and never passes
  `dreaming`, so it measures document-chunk RAG under default dynamic
  dreaming whose memory extraction is explicitly unfinished at
  `status:"done"`. It is a legitimate but distinct variant — named
  `supermemory-local-documents-v3` in this programme — and its defects are
  filed upstream as an issue with evidence, never fixed by us
  (fixing a competitor's configuration of another competitor repeats the
  retired defect class in reverse).

---

## 4. Data flow (per case)

```mermaid
flowchart LR
  DS[dataset row\npinned sha256] --> NORM[normalize + NEUTRALIZE\nsession_ordinal, content_sha256,\ntimestamp_semantics]
  NORM -->|ProtocolEvent stream\n(gold structurally quarantined)| SCAN{leakage scan\ningest scope: strict}
  SCAN -->|clean| ING[provider ingest\nisolated namespace + canaries]
  SCAN -->|hit| INVALID1[case INVALID]
  ING --> RDY{readiness\npositive verification\nfail closed}
  RDY -->|verified| PROBE[known-answer probes\nlexical / semantic / update]
  RDY -->|unproven| INVALID2[run INVALID]
  PROBE --> Q[exact search query\ntop-k pinned]
  Q --> HITS[raw response +\nnormalized hits]
  HITS --> PACK[packed answer context\nsize recorded]
  PACK --> ANS[frozen reader]
  ANS --> JUDGE[official / pinned judge]
  JUDGE --> TRACE[(case trace + manifest\nregenerable report)]
```

Every arrow persists an artifact; reports regenerate from artifacts with a
socket guard proving zero provider calls.

---

## 5. Migration of historical artifacts

- All 57 `benchmarks/runs/` membench dirs and all 8 fork bm-bench runs:
  **`historical-untrusted`** — none reproduces under a verified environment;
  the programme's report renderer refuses to read them.
- `docs/memory-proof-benchmark*.md` cross-contender figures and
  `docs/comparison-basic-memory-graph.md`: already marked WITHDRAWN on
  `main`; they stay as history with their withdrawal banners.
- Single-product findings that STAND (used as regression baselines, not
  comparisons): factual_qa 0 → 99 → 157/180 across the retrieval fix
  (PR #378); native provenance 0/272 → 50/238 (4b.42); compiled
  supersession 0 → 37 with a one-row score move (4b.43 — the proof that
  text-reading gates are blind to lifecycle state, and the design driver
  for the new state-snapshot assertions).
- KB research notes (`Benchmark direction locked…`, `Memory-proof benchmark
  v0.1 ran…`, `Retrieval fix measured…`, `…foundation locked…`, July's
  Basic Memory comparisons) remain accurate as *history*; the 2026-08-08
  REJECT audit and this programme's supersession are filed as a new KB note
  so the KB thread does not end on the direction-lock.

---

## 6. Fairness questions the implementation must expose (not hide)

| Provider | Open question | How the programme exposes it |
|---|---|---|
| Exomem | Does compiled/native mode earn its cost over source-only? | `exomem-source-only` vs `exomem-native` are separate rows; write-agent budget envelope published |
| Exomem | Does the soft-degrade honesty problem (4b.30) bite users? | Lane D failure-transparency injects the fault and reports what the user sees |
| Basic Memory | Is semantic search actually on? | Fail-closed readiness: `search_vector_chunks` count > 0 + config + log line; `reindex` exit codes explicitly distrusted |
| Basic Memory | Is git history a fair substitute for a revision store? | Two rows (`native-git` / `native-nogit`); split assertions (retention vs linkage), disclosure not penalty |
| Supermemory | When are memories actually ready? | `status:"done"` never treated as extraction-complete; memories-canary polling; `readiness-unverifiable` as a first-class disclosed status for dynamic dreaming |
| Supermemory | Hosted vs local — same product? | Never collapsed; hosted runs proprietary extraction and is always a separate row |
| Supermemory | What org-level config shaped the run? | `GET /v3/settings` captured verbatim into every manifest |
| All | Who authored each configuration value? | Config-provenance table per provider (file:line or docs URL); a knob without provenance refuses to run |
| All | How much glue did Exomem author? | Fairness matrix publishes LOC + endpoint counts per projector/driver; gross asymmetry is itself a finding |

---

## 7. Architecture decision

The minimal change that makes comparison trustworthy is **not a rewrite** —
it is a provider-neutral protocol substrate (events, manifests, traces,
leakage scanning, isolation canaries, readiness, budget) plus
**inversion of configuration authorship**: competitor rows come from
competitor-authored harnesses and providers (bm-bench, MemoryBench), Exomem
enters those harnesses as a guest, and residual Exomem-authored glue is
provenance-cited, disclosed, and adversarially reviewed. Full rationale and
the supersession of the rescope change's retirement clause:
`docs/adr/0002-competitive-eval-architecture.md` and OpenSpec change
`add-competitive-benchmark-programme`.
