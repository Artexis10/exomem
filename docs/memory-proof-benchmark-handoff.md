<!-- authority:implementation-reference -->

# Memory-proof benchmark — full continuation handoff (2026-08-07)

Historical execution context only. OpenSpec is the durable specification
authority; this handoff records implementation state and evidence from that run.

Audience: any successor session — Claude (Fable/Opus orchestrator) or a
GPT-5.6 Sol/Terra implementation lane. This file is self-contained: state,
sources of truth, open queue, operating rules, and routing. Read it before
touching anything.

## Mission (settled)

Build and run the industry-standard benchmark for long-term governed
knowledge stores: maximize coverage of **digitally-writable knowledge**
(the Polanyi/tacit boundary is explicitly out of scope and lives as an
auditable registry row), falsification-first, provider-neutral, no
weighted aggregate, deterministic gates final. Founder decision recorded
2026-08-01/02; conventional memory benchmarks (LoCoMo, LongMemEval,
ConvoMem) measure conversation-recall QA and no public benchmark tests
this niche (see the adjacent-landscape appendix).

## State snapshot (verify with `git log` before relying on it)

- Worktree `…/.claude/worktrees/bench-foundation`, branch
  `worktree-bench-foundation`, HEAD `696a7b0` ("first like-for-like
  head-to-head; guard the embeddings profile"). origin/main has been
  merged in (`75d60b3`), and the retrieval fix landed on main as **PR
  #378** (`888eaab`, from branch `fix/lexical-degraded-retention`
  `91b016f`).
- Proven so far: 17→t23+ seeded templates; three-run delta on the identical
  corpus (factual_qa 0 → 99 → 157 of 180: lexical pre-fix → post-fix →
  embeddings); first valid baseline recorded (`2c877bf`); reproducibility
  confirmed against the August run (`d685990`); Track A integration fixed
  (bm-local non-zero; adapter repaired against the live CLI `3ae3bae`);
  per-contender native renderers registered + a profile-fairness defect
  recorded (`623cc30`); embeddings-profile guard added (a profile that
  asks for the semantic lane must load it or the run is an environment
  fault — see ledger 4b.30's history of three silent mislabels).
- **In-flight hazard:** `benchmarks/membench/scoring/extractive.py` and
  `scoring/gates.py` are modified-uncommitted by another session. Do not
  stage, revert, or edit them without confirming that session is done.
- `infra/secrets/**` and `.env.example` may appear phantom-modified in
  sandboxed sessions (deny-list device masks). Never stage them.

## Sources of truth (in priority order)

1. `openspec/changes/expand-memory-proof-benchmark/tasks.md` — the live
   ledger. 24 boxes done; the open list is the work queue below.
2. `openspec/changes/add-memory-proof-benchmark/` — v0.1 contract
   (capabilities `memory-proof-corpus`, `memory-proof-harness`); its
   fairness requirements bind every new family.
3. `docs/memory-proof-benchmark.md` (methodology, capability matrix,
   family registry table) and `docs/memory-proof-benchmark-v01-findings.md`
   (all measured numbers, root causes, review debt, reproduction
   commands). `docs/handoff-note-timestamps.md` for the product-side
   timestamp seam.
4. KB notes (project `exomem`): "Benchmark direction locked…",
   "Retrieval fix measured…", "Memory-proof benchmark v0.1 ran…",
   "…foundation locked… milestone 4c07db8", plus the adjacent-benchmark
   landscape note filed 2026-08-07.

## Open queue (from the ledger — verify boxes before starting; two look stale)

- **Stale-box verification first:** 4b.27 (merge `91b016f` to main) appears
  DONE via PR #378 — confirm and check it off. 4.5 (sub-day temporality
  family) appears at least partially landed via `1a6363b` (sub-day
  knowledge time + t23 family) — verify the "same-day order decides the
  answer, day-granularity systems must abstain" scenario exists before
  checking off; it gates 4.1/4.2 (release bytes).
- **Judgment-required (do NOT hand to a mechanical lane):**
  4b.28 — SETTLED 2026-08-09 (see
  `openspec/changes/honor-evolution-path-anchor/`): the benchmark's former
  "topic_anchor = oldest path" line (since corrected) was a find-order
  tie-break artifact, not a product contract, and the real defect was
  `review_memory --mode evolution` silently dropping `--path`. The decided
  contract: a requested page stays the anchor (`evolution_for_path` keeps
  `topic_anchor` at the requested page while still reporting the active
  head as `chain_id`). 4b.23 — judge discrimination re-test on
  real multi-document response text (current 19/19 is an upper bound from
  clean one-sentence candidates). Governance wiring (v0.2 group 2) — the
  adapter must drive exomem's real `_Governance/` surfaces; if a public
  seam is missing, STOP for a minimal additive product proposal.
- **Well-specified execution (Sol/Terra-safe with the ledger as brief):**
  4b.26 (capture effective profile settings where applied, not from
  ambient env); 1.3 provider onboarding doc + conformance suite; 3.6
  52-week entropy release; 3.7 media-profile multimodal depth; 4.1/4.2
  replication kit + versioned releases (AFTER 4.5 settles bytes); 4.3
  publication-gate reporting; 4.4 judge–human agreement protocol + first
  measurement (protocol design is judgment; running it is mechanical).
- **Runs pending:** ~~full head-to-head follow-ups per findings docs~~ —
  **retired 2026-08-09**: the 2026-08-08 audit voided the head-to-head in
  both directions and `rescope-benchmark-instrument-and-public-suite`
  retires cross-contender runs permanently; the public number now comes
  from the exomem-only LongMemEval-S lane (`public-suite-eval`). GPU
  embeddings after a host torch/driver alignment (CUDA context-init
  segfault in WSL2 — workaround `CUDA_VISIBLE_DEVICES=` + CPU) — still no
  latency claims from this machine.

## Operating rules (non-negotiable, inherited)

- Fable-delegate discipline for nontrivial implementation: packet →
  red-first implementer lane → fresh independent reviewer over the actual
  diff → orchestrator acceptance quoting verdict + diff stat. It has
  caught two MAJOR defects reviews alone would have shipped.
- Fairness invariants: oracle-derived expectations only; unsupported never
  scored as zero; harness failure invalidates the run, never a contender
  loss; per-fact parity reports; no aggregate; deterministic gates final
  over any judge; ontology lint; held-out seed for published numbers.
- Guarded paths: `tests/golden/`, `tests/test_latency_gate.py`,
  `tests/test_retrieval_golden.py`, `.github/`, `src/exomem/**` (benchmark
  code must never mention competitors under src/). Sibling repos read-only
  except the `exomem-provider` branch's `benchmarks/` subtree. Never push
  or open PRs without the founder's word.
- Measurement traps (all root-caused; do not rediscover): `benchmarks/
  run.py` inserts repo src at `sys.path[0]` — A/B fixed-source runs must
  bypass it (`python -c "from membench.cli import main; …"` with
  PYTHONPATH); exomem disable flags are string-truthy (`"0"` DISABLES;
  empty string enables); stale venv console-script/dist-metadata can lie
  about versions until `uv sync`; lean tests <60 s each, offline, no
  credentials.
- Test command: `EXOMEM_DISABLE_EMBEDDINGS=1 PYTHONPATH=src
  <repo>/.venv/bin/python -m pytest
  tests/test_membench_*.py -q` from the worktree root.

## Routing: what a GPT-5.6 Sol session can own

The genuinely hard reasoning is done and externalized: architecture,
bitemporal oracle semantics, fairness/falsification design, retention-gate
root cause and fix, environment root-causes — all locked into specs, docs,
tests, and this queue. Per the repo's own routing table
(`CLAUDE.md`): Sol xhigh suits the design-sensitive/adversarial items
(4b.28 contract settlement, judge-agreement protocol design, governance
wiring design, architecture critique); Terra suits the well-specified
execution list; **Claude-only remains:** KB writes, merges/shared-primary
operations, and anything needing MCP connectors. A Sol lane should treat
the ledger + this file as its brief, work red-first, and leave commits on
this branch, never pushing.

## Adjacent benchmark landscape (learn-from; none covers this niche)

Conversation memory: LoCoMo, LongMemEval, ConvoMem (retrieval near-parity,
answerer-bound — their own findings). Evolving world facts: FreshQA
(closest in spirit to evolving truth; no supersession model). Knowledge
editing/correction: MQuAKE, CounterFact (multi-hop edit propagation —
directly relevant phrasing for supersession/correction families). Temporal
KGQA: CronQuestions, TimeQuestions (as-of question shapes). Long-context:
RULER, InfiniteBench (context length, not durable stores). RAG robustness:
RGB, RAGTruth (noise/faithfulness, no longitudinal state). Bitemporal DB
testing: TPC-BiH (schema ideas, not NL). Borrow question SHAPES and
protocol rigor from these; the governed, longitudinal, bitemporal,
activation-aware knowledge-store niche remains unclaimed — which is the
opportunity.

## One-command orientation for a fresh session

`git -C <worktree> log --oneline -15 && grep -n '^\- \[ \]'
openspec/changes/expand-memory-proof-benchmark/tasks.md` — then read the
findings doc's last two addenda. Trust the ledger over any summary,
including this one.
