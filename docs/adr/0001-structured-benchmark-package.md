# ADR 0001 — Structured in-repo benchmark package (`benchmarks/membench`)

- Status: accepted (2026-07-31)
- Deciders: founder + benchmark architecture review
- Related: OpenSpec `add-memory-proof-benchmark`; supersedes the comparison
  scope of `add-memory-server-comparison-benchmark`

## Context

The memory-proof benchmark (four tracks: external comparable evaluation,
synthetic epistemic corpus, harness/seamless-use behaviour, cognitive
workflows) needs a corpus generator, typed schemas, a bitemporal oracle,
provider adapters for three products, ten scorer families, a runner, and
pluggable answerer/judge backends. Two placement questions had to be settled:
in this repository or a separate one, and single-file scripts or a structured
package.

Constraints that decided it:

- `tests/test_scaffold_no_leak.py` bans competitor tokens under `src/exomem/`
  and `scripts/validate-public-artifacts.py --repository` privacy-scans every
  tracked or non-ignored file. Benchmark code must live outside `src/exomem/`
  but *inside* the scanned tree so synthetic corpora inherit the privacy gate.
- The wheel ships only `src/exomem` + `src/kb_mcp` and the sdist is an
  explicit allowlist, so a top-level `benchmarks/` directory is excluded from
  every shipped artifact with zero packaging changes.
- The repository's evaluation precedent is a single `scripts/*.py` file per
  harness. The largest, `scripts/graph_value_benchmark.py`, reached 6,653
  lines in one module — evidence the pattern does not scale to a multi-track
  system with shared schema/oracle/scoring code.
- A separate repository would carry none of the repo's gates (privacy scan,
  lean CI, OpenSpec validation, ruff), would split the OpenSpec change from
  the code it governs, and could not even be pushed from this environment.

## Decision

Create a top-level `benchmarks/` directory owning everything benchmark-shaped:

- `benchmarks/membench/` — a neutral, importable Python package (schema,
  oracle, generator, templates, artifact + native renderers, adapters,
  scoring, judge, runner, reporting, CLI). The name carries no product token;
  competitor names appear only here and in docs, never under `src/exomem/`.
- `benchmarks/corpus/schema/` (committed JSON-Schemas) and
  `benchmarks/corpus/releases/` (committed text manifests with per-artifact
  hashes). `benchmarks/corpus/generated/` and `benchmarks/runs/` are
  gitignored — generated corpora and run artifacts are never committed;
  public corpus identity = deterministic generator + release manifest.
- Tests remain flat `tests/test_membench_*.py` (repo `testpaths`), importing
  the package via a guarded `sys.path.insert` in `tests/conftest.py`. No
  pyproject change, no new required dependency, `uv lock` untouched.
- Desk-side entry point: `uv run python benchmarks/run.py <subcommand>`.

## Consequences

- Lean CI gains ~12 model-free smoke tests (determinism, schema, oracle,
  scorer gates, adapter e2e on a micro-template) with no credentials and no
  `.github/` changes.
- The privacy gate now also protects generated synthetic content by
  construction (wordbank and generated text are scan-asserted in tests).
- `scripts/` stays the home of single-file perf harnesses; multi-module
  evaluation systems belong in `benchmarks/`.
- Extraction path (if third-party credibility later demands an independent
  repo): `membench` imports exomem only inside `adapters/exomem_local.py`;
  moving `benchmarks/` wholesale plus that one adapter boundary into a new
  repo is mechanical, and the release manifests keep corpus identity stable
  across the move.
