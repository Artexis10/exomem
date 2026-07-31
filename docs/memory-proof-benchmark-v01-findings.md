# Memory-proof benchmark v0.1 — baseline findings (2026-07-31)

Weaknesses first, per the benchmark's falsification mandate. Everything here
was measured on this machine (WSL2, quiesced), exomem v0.36.0 at the
`worktree-bench-foundation` base, lexical profile (embeddings disabled —
see environment blockers), 240-query public seed-1 corpus unless stated.
Run directories referenced below are local artifacts under
`benchmarks/runs/` (this repo) and the basic-memory worktree's
`benchmarks/benchmarks/runs/`; they are gitignored by design — regenerate
with the reproduction commands at the end.

## Headline weakness: NL questions retrieve nothing in the lexical-degraded profile

**Two independent harnesses agree.**

- Track B (this repo's membench, run `20260731T163452Z-…-baseline-lexical`):
  236/240 queries scored, 0 harness failures, run valid — and
  `factual_qa` **0 pass / 180 fail**, `provenance` 0/180, `temporal`
  28 pass/180 fail. The extractive answerer abstained on nearly every
  answerable natural-language question because retrieval returned nothing.
- Track A (the vendored `basic_memory_benchmarks` harness, run
  `3314add53aff`): `exomem-local` completed cleanly (state ok, artifacts
  validate) with **recall@5 = 0.0, MRR = 0.0** across the synthetic smoke
  queries, vs `baseline-grep` at 0.75 — the grep floor beats the product.
- Wire mode behaves identically to in-process leaf mode (T00 wire run
  `20260731T163740Z-…-t00-wire`): the MCP surface adds no divergence.

**Root cause (reproduced in-process, evidence chain in
[memory-proof-benchmark.md](memory-proof-benchmark.md)):** exomem's hybrid
page path retains a BM25 candidate only when it is corroborated by the
vector/graph/CLIP lanes, has a literal-substring excerpt, or contains ALL
query stems (`find_results.stem_tokens_present` at the retention seam,
`find.py:2669`); the `keyword` lane is conjunctive substring matching
outright. With embeddings unavailable those corroboration lanes are empty,
and any interrogative phrasing ("How many…", "What is…") contributes stems
absent from stored text — so every candidate is vetoed while the BM25 lane
itself ranks the correct document first (verified: raw score 14.7 at rank 1
for a query the product answers with zero hits). This also fully explains the
historical Track-A zero-hits run.

**Product-fix recommendation** (smallest justified change, not implemented by
the benchmark): apply the existing relaxed `_any_stem_present` gate — already
used on the outside-KB widening path — to the in-KB retention seam when the
corroborating lanes are unavailable, or gate retention on a coverage
fraction rather than all-stems. Until then, every published lexical-profile
number for exomem carries this context.

## Second-order findings

1. **`keyword` mode is unusable for question answering by design** (strict
   conjunctive substring). Any integration that selects keyword mode when
   embeddings are off — as both the original Track-A provider and this
   repo's first adapter draft did — manufactures zero-hit results.
2. **State isolation gaps in embedded/CI use:** without
   `EXOMEM_WRITER_LEASE_STATE_DIR`/`EXOMEM_CONFIG_PATH`, exomem CLI writes
   open SQLite state under a machine-default path — shared across runs and
   failing outright where that path is unwritable ("unable to open database
   file" during reconcile). Fixed provider-side (commit `02096da2`).
3. **`exomem index` exits 2 under `EXOMEM_DISABLE_EMBEDDINGS`** ("nothing to
   embed") — correct behaviour, but integrations must know to skip the step
   (provider fix in the follow-up commit).
4. **`reconcile` swallows lexical-sidecar build failures**
   (`reconcile.py:349-354`) — not this incident's cause, but a silent-failure
   robustness gap worth closing.
5. **Scoring nuance, recorded to prevent over-claiming:** with empty answers,
   `governance` no-leak passes (16/16) and some `temporal` passes (28: absent
   forbidden values) are trivially satisfied. These columns are honest but
   weak evidence in this profile; the report generator keeps them separate
   per-dimension so they cannot inflate anything.
6. **Absent-capability families measured as designed** (bitemporal as-of,
   disputed state, calibration, injection-as-data, revocation): in this
   profile they fail via non-retrieval before the capability gap is even
   reached; they become discriminating once retrieval works (embeddings
   profile / product fix), which is exactly why the corpus encodes them.

## Environment blockers (recorded, with user-run commands)

- **Embeddings profile (recommended profile) is sandbox-blocked here:** the
  HF cache is read-only and hub network is blocked; BGE is not cached. The
  soft-fail chain worked exactly as documented (BM25-only fallback). To run
  it outside the sandbox:
  `EXOMEM_VAULT_PATH=… uv run python benchmarks/run.py run --corpus benchmarks/corpus/generated/s1 --provider exomem-local --mode leaf --label baseline-embeddings --top-k 10`
  (unset `EXOMEM_DISABLE_EMBEDDINGS`; first run downloads BAAI/bge-base-en-v1.5).
  Note the previously recorded WSL2 CPU SIGABRT on BGE load may still apply.
- **`bm-local` is sandbox-blocked** (spawns `uv run --project …`, and the uv
  cache is read-only here). User-run from the basic-memory worktree:
  `cd /home/hugoa/projects/basic-memory-exomem-provider/benchmarks && EXOMEM_COMMAND=/home/hugoa/projects/exomem/.venv/bin/exomem .venv/bin/bm-bench run retrieval --dataset-id synthetic --dataset-path benchmarks/synthetic/queries.json --corpus-dir benchmarks/synthetic/docs --queries-path benchmarks/synthetic/queries.json --providers bm-local,exomem-local,baseline-grep --top-k 10 --bm-local-path /home/hugoa/projects/basic-memory-exomem-provider --allow-provider-skip`
- **External datasets** (LoCoMo, LongMemEval-S) need network:
  `cd /home/hugoa/projects/basic-memory-exomem-provider/benchmarks && .venv/bin/bm-bench datasets fetch --dataset locomo --output benchmarks/datasets/locomo/locomo10.json`
- **QA/diagnose stages** use the Claude CLI (network):
  `.venv/bin/bm-bench run qa --run-dir <run> --answerer claude:claude-haiku-4-5 --judge claude:claude-sonnet-4-6` then
  `.venv/bin/bm-bench run diagnose --run-dir <run>`

## What worked (harness integrity, so the zeros are trustworthy)

Ingest 12/12 and 200/200 ok across runs; sentinels survive capture→retrieve
(title-probe witness in CI); run directories immutable with failures kept in
denominators; corpus generation deterministic under 8 seeds with privacy-scan,
span-lint, ontology-lint, and canary-purity gates; per-fact parity reports for
every native renderer including honest binary-artifact degradation; the
Track-A artifact validator passes our runs.

## Track C — activation, injection, continuity (measured in-sandbox)

Frozen 19-case control-prompt suite against isolated hook homes; **observed
matched the predeclared expectation for every case**, and mismatched gate
behaviour is reported as a measured limit, never silently re-labeled.

- Quiet on all 8 short control prompts (cp01–cp08); fires on the three
  substantive prompts including non-English (cp09–cp11: relevant-activation
  1.0, missed 0); per-session and client-wide cooldowns both honoured
  (cp13/cp14 quiet).
- **Measured gate limits (product weaknesses):** the control-prompt skip is
  length-bounded (≤180 chars), so a long control-flavored imperative (cp12)
  fires; and the gate has **no topicality signal**, so two substantive-looking
  hard negatives that need no memory (hn01/hn02) fire — the structural gate
  cannot distinguish "substantive" from "memory-relevant". Unnecessary
  activations: {cp12, hn01, hn02}.
- Retrieval-injection ladder, CLI rung: fires with a stub block citing the
  corpus for a substantive prompt; degrades to the reminder-only floor when
  the CLI is absent from PATH. Contract corrections vs the plan, from
  reading the shipped hook: resolution is PATH-based `shutil.which`
  (no `EXOMEM_COMMAND` in hooks), and the REST rung remains a documented
  user-run command.
- Continuation checkpoints: same-client round-trip recalls **7/7 planted
  structural markers** (workspace, branch, HEAD, artifact paths, dirty path,
  transcript-slice hash) within the 64 KiB bound; the transcript itself is
  hashed, never parsed. **Cross-client restore is impossible by contract**
  (per-client state roots) — scored as isolation-respected plus the second
  client's own 100% recall, not fudged.
- Environmental note: this host's `/tmp` is owned by `nobody`, which the
  hooks' trusted-directory walk rejects (pre-existing; the repo's own
  installer tests fail here for the same reason) — drivers allocate hook
  homes under the repo's gitignored scratch tree instead.

## Track D — J1/J2 journeys (green, checks bite)

- **J1 longitudinal evolution** (8 steps, 11 deterministic checks, 0 manual
  interventions): v1→v2→v3 supersession chain ordered and complete; current
  ask returns v3 first; superseded pages remain readable; exactly 3 note
  pages (no duplicate sprawl).
- **J2 correction propagation** (12 steps, 12 checks, 0 manual): all 5
  paraphrase queries rank the corrected page above the superseded one under
  product defaults; audit reports no contradiction; provenance retained
  (corrected page cites the original source). A wrong-order variant (skip
  the final replace) fails its chain/current-page checks — the checks bite.
- **Product discovery:** a first compiled note citing a source passes only in
  a fresh vault via the automatic bootstrap disposition; in a populated
  vault `remember` raises `RELATION_DISPOSITION_MISSING` (replacements
  qualify via the auto-written `supersedes` relation). Any scripted
  integration must handle that path.

## Judge pipeline smoke (real model, blinded, small-N)

12 blinded judgments (6 T00-wire queries × 2 samples) through the file
handshake with a fixed haiku-class judge: 2/12 semantic matches — exactly the
two abstain-correct queries — in full agreement with the deterministic gates;
`judge-scores.json` merged with per-sample values and the cross-run
comparison report rendered per-dimension with no aggregate. The judge layer
works; scale runs remain desk-side/user-run by design.

## Proposed upstream PR (drafted only — NOT opened; branch cut is user-run)

- Target: `basicmachines-co/basic-memory` (the `benchmarks/` subtree is the
  live upstream; the standalone benchmarks repo is dormant since 2026-06-14).
- Branch: cut fresh off `origin/main`; apply the four benchmark files from
  the `exomem-provider` worktree branch (its `benchmarks/` subtree is
  byte-identical to origin/main, so they apply cleanly).
- Files: `benchmarks/src/basic_memory_benchmarks/providers/exomem_local.py`,
  `benchmarks/tests/providers/test_exomem_local_provider.py`,
  `benchmarks/src/basic_memory_benchmarks/providers/__init__.py`,
  `benchmarks/src/basic_memory_benchmarks/cli.py`. Explicitly excluded:
  any dependency/lock change (install is external via `EXOMEM_COMMAND` /
  `pip install exomem`).
- Title: `feat(benchmarks): add exomem-local provider (warm MCP stdio, neutrality-locked)`
- Body outline: (1) new optional provider at bm-local's warm-stdio altitude;
  (2) neutrality locks (exomem-specific ranking off: graph/rerank/prefer_*
  false; byte-copy ingestion; hybrid mode always — keyword mode is
  conjunctive substring and unsuitable for NL queries); (3) isolation
  (temp vault + benchmark-owned lease/config state; `ProviderSkippedError`
  when the binary is absent, so default CI is unaffected); (4) 12 offline
  unit tests, no live engine needed; (5) limitations
  (`supports_group_reuse=False` pending project-per-group; semantic index
  step skipped when embeddings are disabled); (6) no benchmark numbers
  claimed in the PR.

## Reproduction (single-line commands, dependency order)

- `cd /home/hugoa/projects/exomem/.claude/worktrees/bench-foundation`
- `EXOMEM_DISABLE_EMBEDDINGS=1 PYTHONPATH=src /home/hugoa/projects/exomem/.venv/bin/python -m pytest -q tests/test_membench_privacy.py tests/test_membench_schema.py tests/test_membench_oracle.py tests/test_membench_generate_determinism.py tests/test_membench_artifacts.py tests/test_membench_native_parity.py tests/test_membench_scoring_gates.py tests/test_membench_runner.py tests/test_membench_adapter_exomem.py tests/test_membench_template_suite.py tests/test_membench_private_format.py`
- `EXOMEM_DISABLE_EMBEDDINGS=1 /home/hugoa/projects/exomem/.venv/bin/python benchmarks/run.py generate --seed 1 --out benchmarks/corpus/generated/s1`
- `EXOMEM_DISABLE_EMBEDDINGS=1 /home/hugoa/projects/exomem/.venv/bin/python benchmarks/run.py run --corpus benchmarks/corpus/generated/s1 --provider exomem-local --mode leaf --label baseline-lexical --top-k 10`
- Track A smoke: the `bm-bench run retrieval` line above (providers `exomem-local,baseline-grep` in-sandbox; add `bm-local` outside).
- Determinism check: run `generate` twice into two directories and diff the manifests.
