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
| `membench/utility/` | seeded paired action episodes, isolated native actors, downstream utility/harm and lifecycle cost accounting |
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

## Downstream utility (`membench/utility/`)

The utility instrument asks whether memory changes an agent's resulting
actions. Its first world has three sessions (experience, change, action),
three variants (helpful history, a self-contained task, stale distractors),
and matched no-memory/Exomem arms. Both arms retain ordinary workspace notes
and can inspect current authoritative project facts. Only the Exomem arm
receives shipped memory tools and guidance; its capture and maintenance are
agent-authored. Grading checks the actual configuration, a final-session write and complete
history, including wrong-project damage after later repair.

This reuses the native actor, cell and metered backend from PR #1126, pinned
at `512d4ff6fd76b19847ea7fd6e6259dc8b61e2aad`, at their existing `lme/` paths.
It does not run the LongMemEval replay scheduler or a model judge.

Development checks are model-free. Paid execution is opt-in and gated by the
acknowledged f32 protocol receipt at both execution and comparative report
loading. A pending receipt permits instrument tests, not comparative scores.
The repository uses squash merges, so acknowledgment must pin the merged
contract revision in a subsequent commit; a feature-branch hash cannot stand
in for that ancestry.

```sh
uv run python benchmarks/run.py utility run --help
uv run python -m pytest -q tests/test_membench_utility_scenarios.py tests/test_membench_utility_world.py tests/test_membench_utility_scoring.py
```

After f32 is acknowledged, run from the clean recorded product revision.
Supply an OpenRouter credential through `OPENROUTER_API_KEY` using the local
credential helper. The approval marker below is an audit label, not a secret.
The two model-cache paths must be local Hugging Face model directories with
their frozen `refs/main` and snapshots. Keep output outside the product tree.

```sh
uv run python benchmarks/run.py utility run \
  --output /tmp/exomem-utility-seed-11 --seed 11 --product-root "$PWD" \
  --python /path/to/semantic-runtime/bin/python \
  --tokenizer-path /path/to/verified-tokenizer.json \
  --model-cache /path/to/models--BAAI--bge-base-en-v1.5 \
  --clip-model-cache /path/to/models--sentence-transformers--clip-ViT-B-32 \
  --paid --approval-token utility-pilot --cap-usd 2
uv run python benchmarks/run.py utility read \
  --run /tmp/exomem-utility-seed-11 --product-root "$PWD"
```

The reader checks the protocol identity, artifact digests, recorded revision
and guidance, then regrades observed actions and phase failures. Reports split
usage by variant, arm and phase. The JSON manifest and opaque session traces
remain evaluator artifacts; they are never inputs to an actor.

The first smoke uses seed 11, six episodes and eighteen fresh sessions with
a $2 hard cap. The pinned GLM endpoint's conservative model reservation is
$0.3936 per complete pair, or $1.1808 for all three. Requests allow at most
eight model calls per phase, 48k input and 2k billed output tokens per call,
and a 180-second phase deadline. Failed work consumes its measured budget;
unknown charges stay reserved and stop further spending. The tokenizer must
match the digest in `lme/metered_profiles.py`. Semantic cells need an
interpreter with the embedding dependencies and frozen local model caches;
the lexical fixture profile is an instrument configuration, not evidence of
the default semantic product.

Read results by variant: utility lift, wins/losses/both-pass/both-fail,
conditional harm with its denominator, unconditional destructive effects,
coverage and phase costs. Zero retrieval, missed capture and declared actor
budget exhaustion remain task failures when the environment is evaluable.
Infrastructure invalidity is separate, with its spend retained. Unknown
measurements remain unknown, and billed reasoning is not counted twice.

The architecture supports coding and conversational workloads. This small
configuration world validates the instrument; it does not establish real
coding performance or open-ended conversational quality. Reference/candidate
monitoring, JIT/oracle controls and representative workflow canaries follow
in separate changes. There is no single weighted leaderboard score.

This fixture makes authoritative facts available through one common tool
call, so it offers little opportunity for memory to reduce research work.
It can expose extra context cost and stale transfer; neutrality here cannot
establish neutrality on knowledge-intensive work. The native API actor also
removes duplicate text only after proving it equals the structured payload,
and retains raw and delivered results in separate trace fields. Distinct
content and metadata survive. Its measured context cost belongs to that
declared renderer, not automatically to Claude Code or Codex.

## Epistemic State Bench (`epistemic/`)

Families and assertions are frozen in
[`epistemic/PREREGISTRATION.md`](epistemic/PREREGISTRATION.md) before any run.
The document is amended only through a receipt under `epistemic/contracts/`,
each binding the previous document's digest to its own, and a family a receipt
introduces stays **registered but withheld** until the founder acknowledges the
receipt — the scenario loader refuses to load it, so an unratified family cannot
reach a score by accident.

**f27 `lifecycle_routing_replay`** is the family added by
`amendment-2026-08-lifecycle-replay.v1.json` (sequence 3, acknowledged
2026-08-30). It replays an
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

Sequence 3 was acknowledged on 2026-08-30, so a run may back a comparative
claim; the family is declared expected-partial on the current runtime — the
next slice's falsification target. Runs recorded while the receipt was pending
remain evidence about the harness and that runtime, as their manifests say.

## Degradation modes (recorded, never silent)

PDF artifacts require pymupdf (else emitted as `pdf_unavailable` markdown and
listed in the manifest); binary artifacts are never faked as text (title-only
capture, parity `degraded`); embeddings profiles are opt-in and may fail on
this machine (recorded env failure); model-backed answer/judge backends are
default-off and emit verbatim user-run commands when unavailable. An
environment fault marks a run INVALID — never a contender loss.
