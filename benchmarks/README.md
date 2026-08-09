# benchmarks/ — the memory-proof benchmark (membench)

Falsification-oriented, four-track benchmark for memory/knowledge systems.
Neutral by construction: the `membench` package carries no product ontology,
generated corpora are never committed (identity = deterministic generator +
release manifest hashes), and nothing here ships in the wheel or sdist.

- Methodology, capability matrix, falsification register:
  [`docs/memory-proof-benchmark.md`](../docs/memory-proof-benchmark.md)
- v0.1 baseline findings (weaknesses first):
  [`docs/memory-proof-benchmark-v01-findings.md`](../docs/memory-proof-benchmark-v01-findings.md)
- Governing OpenSpec change: `openspec/changes/add-memory-proof-benchmark/`
- Placement rationale: `docs/adr/0001-structured-benchmark-package.md`

## Layout

| Path | What |
|---|---|
| `membench/schema.py`, `oracle.py` | strict corpus records; pure bitemporal truth oracle (single source of expected answers) |
| `membench/templates/` | 17 scenario templates → 240 oracle-derived queries across 7 families |
| `membench/generate.py`, `artifacts/`, `native/` | seeded deterministic generation; md/csv/png(/pdf-degradable) artifacts; per-product native renderers with per-fact parity reports |
| `membench/adapters/` | capability-declaring provider adapters (exomem leaf/wire; Track-A bridge) |
| `membench/scoring/`, `judge/`, `reporting.py` | deterministic gates (final), model-free extractive answerer, blinded optional judge via credential-free file handshake, per-dimension reports (no aggregate) |
| `membench/trackc/`, `trackd/` | activation/injection/continuity drivers; workflow journeys |
| `membench/runner.py`, `cli.py`, `run.py` | immutable run dirs, failures kept in denominators; CLI launcher |
| `corpus/schema/` | committed JSON-Schemas (drift-gated) |
| `corpus/generated/`, `runs/`, `private/` | **gitignored**: generated corpora, run artifacts, local founder-regression fixtures |

## Quick start (desk-side; lean/model-free by default)

```
uv run python benchmarks/run.py generate --seed 1 --out benchmarks/corpus/generated/s1
uv run python benchmarks/run.py run --corpus benchmarks/corpus/generated/s1 --provider exomem-local --mode leaf --label baseline-lexical --top-k 10
uv run python benchmarks/run.py catalog
```

Determinism check: generate the same seed twice into two directories and
compare manifests. CI smoke = the `tests/test_membench_*.py` files in the
lean suite (no credentials, no extras, each test <60s).

## Degradation modes (recorded, never silent)

PDF artifacts require pymupdf (else emitted as `pdf_unavailable` markdown and
listed in the manifest); binary artifacts are never faked as text (title-only
capture, parity `degraded`); embeddings profiles are opt-in and may fail on
this machine (recorded env failure); model-backed answer/judge backends are
default-off and emit verbatim user-run commands when unavailable. An
environment fault marks a run INVALID — never a contender loss.
