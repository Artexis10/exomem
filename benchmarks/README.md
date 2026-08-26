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
| `epistemic/` | the pre-registered Epistemic State Bench: frozen family/assertion registry, neutral snapshot schema, projectors, scenario fixtures, and the agent-driven journeys |
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

## Epistemic State Bench (`epistemic/`)

Families and assertions are frozen in
[`epistemic/PREREGISTRATION.md`](epistemic/PREREGISTRATION.md) before any run.
The document is amended only through a receipt under `epistemic/contracts/`,
each binding the previous document's digest to its own, and a family a receipt
introduces stays **registered but withheld** until the founder acknowledges the
receipt — the scenario loader refuses to load it, so an unratified family cannot
reach a score by accident.

**f27 `lifecycle_routing_replay`** is the family added by the pending
`amendment-2026-08-lifecycle-replay.v1.json` (sequence 3). It replays an
authored ten-turn episode of ordinary working language — every store-bearing
utterance removed by a gate that refuses one at corpus construction and again at
scenario load — through the installed agent CLI, and diffs the durable state
against the fold of the corpus's own annotations. Coverage is reported in three
tiers that are never summed, always beside its false-write dual, because a
product that writes something for every sentence would otherwise look perfect.
f26 is its state-free sibling: same client-surface discipline, but about what a
response carried rather than what a session left.

Its driver is `epistemic/journeys/f27_replay.py`:

```
uv run python -m epistemic.journeys.f27_replay --arm both --out /tmp/f27-run --dry-run
```

**`--out` must be outside every repository**, and the driver refuses one that is
not. Memory-file discovery is not governed by `--setting-sources`: measured
2026-08-23, a turn run from a cwd inside this checkout carried this repository's
`CLAUDE.md` *and* the operator's `~/.claude/CLAUDE.md` into the agent's context,
and this repository's own `CLAUDE.md` opens by naming the store. Both arms would
have been told the answer, and they would still have agreed with each other.

The dry run prints, per arm, every turn's exact argv, the whole environment
delta against the parent process, and the prominence it would set; it seeds no
vault, writes nothing under `--out`, and executes no agent turn. A real run
costs subscription turns.

`HOME` is deliberately not moved — the agent's OAuth credentials live under
`$HOME/.claude/`, and a run that relocated it answered "Not logged in" in 91 ms.
One consequence is stated rather than hidden: the operator's real
`~/.claude/projects/<cwd>/` gains a transcript directory for each arm's working
directory. The manifest records this alongside the run.

While sequence 3 is unacknowledged, a run is evidence about the harness and the
current runtime, recorded as the family's finding; the manifest it writes says
so, and it is not a comparative claim.

## Degradation modes (recorded, never silent)

PDF artifacts require pymupdf (else emitted as `pdf_unavailable` markdown and
listed in the manifest); binary artifacts are never faked as text (title-only
capture, parity `degraded`); embeddings profiles are opt-in and may fail on
this machine (recorded env failure); model-backed answer/judge backends are
default-off and emit verbatim user-run commands when unavailable. An
environment fault marks a run INVALID — never a contender loss.
