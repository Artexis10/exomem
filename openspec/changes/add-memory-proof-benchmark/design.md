# Design — memory-proof benchmark

Full methodology and audit baseline: `docs/memory-proof-benchmark.md`.
Placement rationale: `docs/adr/0001-structured-benchmark-package.md`.

## Package

Top-level `benchmarks/` (excluded from wheel/sdist by existing allowlists):

```
benchmarks/
  run.py                      # thin launcher -> membench.cli
  membench/
    schema.py oracle.py wordbank.py ids.py clock.py generate.py
    templates/                # base + t00_mini_smoke + t01..t16 family modules
    artifacts/                # markdown, tabular, image(Pillow), pdf(optional), transcript
    native/                   # neutral + basic_memory + exomem_kb + graybox (+ parity reports)
    adapters/                 # base(Capability enum) + exomem_local + basic_memory_local
                              #   + graybox_local + track_a_bridge
    scoring/                  # answer_contract + exactness/temporal/claims/citations/
                              #   governance/abstention/graph/retrieval/health/behavior
    judge/                    # blinding + subagent + claude_cli + openai_compat
    runner.py environment.py reporting.py cli.py
  corpus/schema/ (committed)  corpus/releases/ (committed)  corpus/generated/ (ignored)
  runs/ (ignored)             private/ (ignored; founder regression fixtures)
```

Tests are flat `tests/test_membench_*.py` importing via a guarded
`sys.path.insert` in `tests/conftest.py`. No pyproject change; deps are
stdlib + Pillow + pydantic + jsonschema + pyyaml (all present).

## Core decisions

- **Bitemporal oracle as single source of truth.** Templates author explicit
  dual-axis StatusSpans (world validity + knowledge time, 9 states, each span
  justified by an assertion); a pure oracle evaluates current/as-of truth,
  change narratives, required citations (transitive), and audience views.
  Both the generator (expected.jsonl) and every scorer call the same oracle.
  Exomem's absent bitemporality is thereby probed honestly: expectations are
  computable even where the product cannot represent them, and behaviour
  (evolution narrative, hedging) is what gets scored.
- **Determinism by construction.** Child seed per (master, template, variant)
  via sha256 → template isolation; syllable-grammar wordbank (privacy-scan
  clean by test); logical content hash as primary artifact identity, byte
  hash secondary (Pillow version recorded); double-generation CI test.
- **Corpus publication = generator + committed release manifest.** Generated
  trees and run dirs are gitignored (repo privacy gate would fail committed
  binaries); identity is reproducible hashes.
- **Adapter contract is a superset of Track A's.** `MemoryAdapter` with a
  declared `Capability` frozenset; `track_a_bridge` adapts it to the external
  `BenchmarkProvider` (ingest/search/cleanup/version_info + skip semantics).
  Exomem adapter modes: `leaf` (op_* in-process; lean CI), `governed`
  (writer_lease.invoke_command), `wire` (build_server + call_tool; the
  cross-product fairness mode). Isolation via the verified env recipe
  (EXOMEM_VAULT_PATH mandatory-no-fallback + disable/pin knobs); scored
  responses asserting no warming/degraded markers; logs outside the
  disposable vault.
- **Gates are final.** AnswerRecord envelope; the deterministic extractor may
  only add structure; judge (optional, blinded, order-randomized, N-sample
  variance, file-handshake so the runner never holds credentials) can never
  flip leak/date/citation/current-state/abstention/disclosure gates.
- **Health metrics three-tier:** provider-native audit capability → generic
  STATE_EXPORT (shingling + link-graph) → unsupported (never zero).
- **Hosted comparison is a profile axis**, not code: run manifests carry
  `reasoning: client|hosted`; hosted rows stay blocked-until-implemented.
  Candidate hosted jobs + decision thresholds live in
  `docs/hosted-inference-boundary.md` (Milestone-2 deliverable).

## Track designs

- **Track A**: preserved provider (custody `bed652bb`, portability
  `4fe506e8` on the `exomem-provider` branch), offline execution via
  `--bm-local-path`, zero-hits root-cause protocol (prime suspect: reconcile
  swallows lexical-sidecar failures; evidence externalized), 3-doc synthetic
  smoke with `bm-local,exomem-local,baseline-grep`, sanity gate + canary,
  group-reuse parity scheduled, upstream PR drafted only.
- **Track C**: isolated hook homes (EXOMEM_HOOK_HOME / CLAUDE_CONFIG_DIR /
  CODEX_HOME); frozen 14-case control-prompt suite + hard negatives;
  injection ladder (CLI rung offline, REST rung loopback-or-user-run);
  checkpoint round-trips incl. cross-client; two-witness activation ground
  truth (server call-trace × client transcript), mismatch = harness fault;
  natural-prompt `claude -p` driver for user-run execution; subagent
  propensity runs labeled SIMULATION.
- **Track D**: journey runner on the product_flow_benchmark FlowRunner
  pattern; J1 longitudinal evolution, J2 correction propagation, J3 weekly
  review (only judged piece: summary text, predeclared rubric), J4 connection
  discovery with decoys; product-neutral event streams; per-product mapping
  tables published.

## Falsification register

Fourteen predeclared rigging vectors with mitigations (ontology lint,
ingestion parity, blinding + leakage grep, held-out seed, frozen control set,
comparative-vs-introspective metric split, profile labels, distinct
answerer/judge models, fresh hook homes, search-only timing, zero-hit sanity
gate + canary, neutral event language, unique basenames) — normative list in
`docs/memory-proof-benchmark.md`.
