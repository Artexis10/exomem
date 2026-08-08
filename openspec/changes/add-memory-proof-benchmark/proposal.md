# Add memory-proof benchmark (corpus + four-track harness)

## Why

Exomem's competitive claims rest on founder experience, a probe-level graph
comparison (`graph-value-benchmark`, done), and a golden-set retrieval eval —
none of which measures evolving truth, epistemics, corpus health over time,
seamless activation, or workflow utility, and none of which can run the
external `basic_memory_benchmarks` harness. The benchmark must be designed to
falsify Exomem's claims: the verified capability audit (2026-07-31,
`docs/memory-proof-benchmark.md`) shows bitemporal/as-of queries, a
first-class disputed state, calibrated-uncertainty fields, prompt-injection
defenses, and belief-level revocation are ABSENT — a credible benchmark must
expose exactly these, with per-dimension reporting and no compensating
aggregate.

Relation to existing changes: this change **supersedes the comparison scope of
`add-memory-server-comparison-benchmark`** (active, 0/7 tasks, stale — its
untouched perf lanes such as RSS/startup comparison remain out of scope here);
it **inherits and does not modify** the fairness requirements of the
`core-product-comparison-benchmark` delta in `add-first-class-semantic-language`
(unsupported never scored as zero, no weighted aggregate, harness failure
invalidates the run, predeclared expectations, native-renderer parity
reports); and it **generalizes the patterns** of the completed
`add-graph-value-benchmark` (persistent public sessions, isolated state,
corpus hashing, raw-envelope preservation) into a structured package per
`docs/adr/0001-structured-benchmark-package.md`.

## What Changes

- New top-level `benchmarks/` directory holding the neutral `membench`
  package: typed corpus schema + bitemporal oracle, deterministic seeded
  generator (16+ scenario templates, 4+ variants, 200+ questions, 12-week
  ingestion schedule), artifact renderers (markdown/CSV/PNG, optional PDF with
  recorded degradation), per-product native renderers with per-fact parity
  reports, capability-declaring provider adapters (exomem-local,
  basic-memory-local, graybox-local, Track-A bridge), ten deterministic scorer
  families, an immutable run-artifact format, per-dimension reporting, and
  pluggable desk-side answerer/judge backends (default off).
- Track C harness drivers: hook-gate/control-prompt suites, retrieval-
  injection ladder tests, continuation-checkpoint round-trips — all against
  isolated hook homes; a natural-prompt `claude -p` driver for user-run
  execution; two-witness activation ground truth.
- Track D journey runner (flow-registry pattern) with four scripted
  knowledge-work journeys and predeclared rubrics.
- Track A integration: the `exomem-local` provider work in the sibling
  `basic-memory` worktree is preserved, made portable, root-caused (zero-hits
  incident), smoke-run offline, and packaged as an upstream PR draft (not
  opened).
- Documentation: `docs/memory-proof-benchmark.md` (methodology, capability
  matrix, falsification register, run matrix), ADR 0001, hosted-inference
  boundary doc, private founder-regression format.
- `.gitignore` gains `benchmarks/corpus/generated/` and `benchmarks/runs/`;
  `tests/conftest.py` gains a guarded `sys.path` insert; ~12 lean CI smoke
  tests land as ordinary `tests/test_membench_*.py`.

## Capabilities

### New Capabilities
- `memory-proof-corpus`: deterministic seeded synthetic corpus — typed
  schema, bitemporal oracle, scenario families, artifact + native renderers,
  parity reports, release manifests.
- `memory-proof-harness`: provider adapters, runner, deterministic scoring
  with final gates, blind pluggable judge, run artifacts, activation and
  journey measurement, per-dimension reporting.

### Modified Capabilities
None.

## Impact

- Runtime code: none. `benchmarks/` is excluded from wheel and sdist by the
  existing packaging allowlists; no new required dependency; no change to the
  MCP/CLI/REST surface; competitor names never appear under `src/exomem/`.
- Pure-substrate justification: model-backed answering/judging exists only as
  desk-side benchmark backends in `benchmarks/membench/judge/`, is **off by
  default**, soft-fails to the deterministic extractive answerer, and is never
  reachable from the product runtime. The default evaluation path is entirely
  model-free.
- Optional/heavy behaviour is default-off with recorded degradation: PDF
  artifacts require the `media` extra (else `pdf_unavailable`), embeddings
  profiles are opt-in and may fail on this machine (recorded), full corpus
  runs are desk-side commands, and CI runs only lean model-free smoke tests
  with no credentials and no `.github/` edits.
- Sibling repositories: read-only, except deliberate local commits on the
  dedicated `exomem-provider` branch of the basic-memory worktree (custody +
  portability; never pushed without approval).
- The existing aggregate-only publication requirement
  (`find-recall-efficiency`) is untouched: it continues to bind real-vault
  reports, while the synthetic public corpus declares its own per-query
  publication regime in each run manifest.
