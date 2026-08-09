# Memory-proof benchmark — methodology and audit baseline

> **Rescope (2026-08-09).** An independent adversarial audit (2026-08-08)
> found **every reported dimension of the Track B head-to-head unsafe** — six
> critical and six major findings — and the comparison is **withdrawn in both
> directions**: the original "exomem substantially ahead" (the harness never
> built the competitor's vector index) and the corrected "basic-memory at
> parity or ahead" (the harness's renderer fed basic-memory oracle-normalised
> facts phrased in the query vocabulary). Per OpenSpec change
> `rescope-benchmark-instrument-and-public-suite`: Track B is an **internal
> instrument** (regression + compiled-path correctness, bounded by the oracle
> ceiling and null floor); **no cross-contender comparative table authored in
> this repository is publishable**, and no competitor integration will be
> authored here again; the public number comes from the exomem-only
> `public-suite-eval` lane (LongMemEval-S, cleaned, official judge) placed
> beside figures competitors published for themselves. Latency and
> harness-mode abstention columns are withheld permanently. Cross-contender
> readings anywhere in this page and its findings companion are historical
> and void.

This page defines the four-track benchmark system specified by OpenSpec change
`add-memory-proof-benchmark` and records the verified audit baseline it was
designed against (2026-07-31). It complements — and does not replace —
[Measured retrieval quality](benchmarks.md) (golden-set retrieval + latency)
and [the graph comparison](comparison-basic-memory-graph.md) (probe-level
graph capability, `scripts/graph_value_benchmark.py`; its cross-contender
claims were **withdrawn 2026-08-09** — see that page's banner).

**Design stance: the benchmark exists to falsify Exomem's claims, not to
flatter them.** A result that exposes weaknesses is a successful result.
Founder experience ("Exomem is more useful than Basic Memory during work") is
anecdotal and confounded by explicit invocation habits; this system separates
memory quality from invocation behaviour and from familiarity.

## Tracks

| Track | Question | Where it runs |
|---|---|---|
| A — comparable evaluation | How does `exomem-local` score on the external `basic_memory_benchmarks` harness (recall@k/MRR, QA, diagnosis) next to `bm-local` and `baseline-grep`? | vendored harness in the `basic-memory` checkout (`benchmarks/` subtree) |
| B — epistemic corpus | Does the product maintain evolving truth, provenance, corpus health, identity, numeric/multimodal evidence, and governed disclosure over a seeded 12-week synthetic corpus? | `benchmarks/membench` in this repo |
| C — harness behaviour | Does relevant memory activate *without being named*, stay quiet on control prompts, write back durably, and survive compaction/restart/client switch? | `membench` Track-C drivers + product hooks in isolated homes |
| D — cognitive workflows | Do capture→compile→evolve→review→connect journeys produce a knowledge base that stays current, deduplicated, and inspectable with low manual burden? | `membench` journey runner (flow-registry pattern) |

Results are reported per dimension. **There is no weighted aggregate score.**
Inherited fairness principles (from the `core-product-comparison-benchmark`
spec delta): unsupported/unavailable is never scored as zero; a harness,
setup, adapter, or environment failure invalidates the run rather than
counting as a contender loss; expectations are predeclared before observing
output; native renderers publish per-fact parity reports (represented /
degraded / unsupported — nothing silently dropped); failures stay in
denominators.

## Verified capability matrix (audit of 2026-07-31, exomem v0.36.0)

Classification: **S+T** shipped+tested (test evidence exists) · **PARTIAL** ·
**ABSENT**. Deliberate design refusals are marked *(by design)* — the
benchmark scores those behaviourally (what the product's answers do), never by
requiring the missing structure.

| Capability | Class | Evidence anchor |
|---|---|---|
| Sources/Evidence append-only separation | S+T | `access.py`; `tests/test_access.py`, `test_tier2.py` |
| Provenance (sources:/ingested_into:, provenance_report, derived_from/evidenced_by) | S+T | `note.py`, `provenance.py`; `tests/test_provenance_report.py` |
| Typed semantic units (26 kinds) + 26 core relations + graph sidecar + traversal lenses | S+T | `semantic_units.py`, `core-relations.yaml`, `epistemic_graph.py`; 59+20 tests |
| Supersession lifecycle (demote-not-hide, evolution chains) | S+T | `replace.py`, `evolution.py`; `tests/test_replace.py`, `test_supersession_surface.py` |
| Contradiction *detection* + review queue (cosine band 0.82–0.90) | S+T | `corpus_aware.py`, `audit.py`; `tests/test_audit_corpus_contradictions.py` |
| Review surfaces (review_memory 13 modes, review_item_context, triage fingerprints, Studio) | S+T | `attention.py`, `review_context.py`, `review_state.py`; 40+ tests |
| Corpus maintenance (audit 17 categories, reconcile, dup detection, orphans) | S+T | `audit.py`, `reconcile.py`; `tests/test_reconcile.py` (26) |
| Multimodal pipeline (OCR/ASR/PDF/CLIP/video/datasets) | S+T code; deps are opt-in extras | `extract.py`, `media_processing.py`; 250+ tests, dependency-gated |
| Context packs (`ask_memory(deep=true)`) | S+T | `context_pack.py`; `tests/test_context_pack.py` (28) |
| Knowledge packs | S (tested indirectly) | `knowledge_packs.py`; via `tests/test_adopt.py`, wizard tests |
| Workflow skills (9) | S+T | `workflow_skills.py`; `tests/test_workflow_skills.py` |
| Governance: opt-in policy, disclosure ladder L0–L6, audiences+purpose, withhold notices, credential scrubber, hash-chained receipts | S+T | `governance/` (18 modules); `tests/test_governance_egress.py` (178) et al. |
| Graduated excerpt redaction (L4) | PARTIAL — declared, renders as L3 | `governance/egress.py` ("until add-redaction-levels") |
| Hooks: retrieve nudge (UserPromptSubmit), capture nudge (Stop), continuation checkpoints (PreCompact/SessionEnd/SessionStart) | S+T | `_hooks/`; `tests/test_continuation_checkpoint.py` (112), `test_install_hook.py` (75) |
| Cross-client (Claude Code, Codex, claude.ai OAuth, ChatGPT contract) | S+T | `client_config.py`, `server_auth.py`; `tests/test_connector_guardrails.py` |
| Durable write-back (writer lease, atomic batches, content_hash concurrency, idempotency, mutation journal) | S+T | `writer_lease.py`, `vault.py`; `tests/test_transactional_writes.py` et al. |
| Temporal: created/updated/captured + recency lane + evolution narrative | PARTIAL | `find_policy.py`, `evolution.py` |
| Bitemporal valid-from/valid-to; as-of queries; event-time vs ingestion-time | **ABSENT** | no such fields anywhere in `src/exomem/` |
| First-class disputed/contested state | **ABSENT** (detection exists, state does not) | lifecycle = draft/active/superseded/archived |
| Numeric confidence/uncertainty fields | **ABSENT** *(by design)* | `_Schema/references/frontmatter.md` "No confidence floats" |
| Prompt-injection defenses on ingested content | **ABSENT** | one prose principle; no detector/quarantine/trust tier; no tests |
| Belief-level revocation primitive | **ABSENT** *(by design — supersede/archive instead)* | `frontmatter.md` "deletion happens by archive" |

The five ABSENT rows are first-class benchmark targets, not footnotes: Track B
contains as-of, disputed-state, calibration, injection-as-data, and
revocation scenario families precisely because Exomem currently has nothing
structural to answer them with.

## Sibling audit record (pinned states at design time)

| Repo | State | Notes |
|---|---|---|
| exomem | main @ 84f1f63 (v0.36.0) | benchmark work branches from origin/main in an isolated worktree |
| basic-memory | main @ 7b4cbed1 (2026-07-30), clean, AGPL-3.0 | external harness vendored at `benchmarks/` (package `basic_memory_benchmarks`, CLI `bm-bench`) |
| basic-memory worktree `exomem-provider` | custody commit `bed652bb` + portability fix `4fe506e8` (local, never pushed) | carries the `exomem-local` provider (372 lines + 12 offline unit tests); `benchmarks/` subtree byte-identical to basic-memory origin/main |
| standalone basicmachines-co/basic-memory-benchmarks | dormant since 2026-06-14 | vendored into basic-memory 2026-06-25 → the in-tree copy is the current upstream; PR target is basicmachines-co/basic-memory |
| graybox | main @ 645df5b (2026-07-30), clean, MIT | lightweight baseline: importable sync API (`capture`, `search_all`, `ask`); keyword F1 coverage scorer + 1-hop graph; `search()` is wiki-only (post-LLM organize) so the benchmark drives `search_all()` with an explicit `min_score` override, recorded as its documented profile |

Known environment facts recorded up front: loading BGE embeddings on this
WSL2 stack has SIGABRTed before (see [benchmarks.md](benchmarks.md)); all
default profiles are therefore model-free/lexical, with embeddings as an
opt-in profile that may fail and is recorded either way.

**Zero-hits incident: root-caused (2026-07-31, in-process reproduction).**
The historical `exomem-local` run that returned zero hits for every query is
an engine-side behaviour of the lexical-degraded configuration, not a sidecar
or harness fault. Evidence chain: ingest 12/12 ok; the BM25 lane returns the
correct document at rank 1 (raw score 14.7) for the failing natural-language
question; `find()` still emits 0 hits. Mechanism: the hybrid page path keeps a
BM25 candidate only if it is corroborated by the vector/graph/CLIP lanes, has
a literal-substring excerpt, or passes the ALL-stems gate
(`find_results.stem_tokens_present`, applied at the hybrid retention seam);
exomem's `keyword` lane is likewise conjunctive (every token a substring).
With embeddings disabled/unavailable and graph off, an interrogative query
("How many…", "What is…") always contains stems absent from the stored text,
so every retention path fails and hybrid returns nothing — strictly fewer
results than its own BM25 lane. Consequences, all honest: (a) lexical-profile
NL-question retrieval scores ≈0 for exomem and is published as such with the
profile label; (b) the recommended profile is embeddings-enabled and is
attempted only under subprocess isolation on this machine (recorded env
failure if BGE aborts); (c) statement-form/title probes verify harness
integrity (sentinels survive capture→retrieve). Product-fix recommendation
(report scope, not benchmark scope): the relaxed `_any_stem_present` gate
already used on the outside-KB widening path could serve the in-KB retention
seam in lexical-degraded mode. Separately, `reconcile` swallowing
lexical-sidecar build failures remains a robustness gap, but it was not this
incident's cause.

## Honest v0.1 run matrix

| Class | What |
|---|---|
| Runs now, offline | Track A provider unit tests; zero-hits protocol; 3-doc synthetic smoke (bm-local, exomem-local, baseline-grep; lexical; retrieval stage); Track B corpus generation + exomem adapter (leaf/wire) + deterministic scoring + extractive answerer; Track C modes 1/7/8/9-CLI-rung/10/11/12; Track D journeys J1–J4; judge contract tests |
| Runs via harness subagents (labeled, small-N) | fixed-model QA smoke; activation-propensity simulation; J3 rubric judging; blinded judge samples |
| User-run (exact commands provided) | dataset fetches (LoCoMo, LongMemEval-S); `bm-bench run qa`/`diagnose` (Claude CLI); natural-prompt `claude -p` and Codex suites; REST injection rung if loopback is denied; real `/compact` continuity |
| Blocked, documented | embeddings profile on this machine (BGE SIGABRT); grouped datasets until group-reuse parity; mode-13 mobile/hosted automation; judge–human agreement panel |

Simulation-class results (harness-subagent) are always labeled and never
merged with organic-session numbers.

## Falsification / neutrality register

Fourteen ways this benchmark could accidentally rig itself, each with a v0.1
mitigation (implemented as a test or lint wherever possible):

1. Corpus phrasing mirrors Exomem ontology → banned-vocabulary lint generated
   from Exomem op/type/relation names; paraphrase seeds.
2. Ingestion altitude asymmetry between providers → ingested-doc-count parity
   check joins the fairness gate.
3. `supports_group_reuse` asymmetry taxes Exomem per group → v0.1 runs
   ungrouped datasets only; parity is a scheduled fix.
4. Judge contamination by product-identifying strings → sanitizer maps
   `exomem://`/permalink/path shapes to neutral tokens; a leakage grep fails
   the run.
5. Seed-variant overfitting → multi-seed variance reporting; one held-out
   release seed; product SHAs pinned in the manifest.
6. Control-prompt selection bias → the activation control set is predeclared
   and frozen before first measurement and includes hard negatives; known gate
   limits are documented, not hidden.
7. Health metrics only Exomem can produce → metrics are split into
   comparative (observable on any provider) vs introspective (labeled,
   excluded from cross-product tables).
8. Lexical-machine bias (embeddings broken here) → single-profile runs; every
   published number carries its profile label.
9. Same model answering and judging → distinct models by default; N-sample
   judge variance.
10. Cooldown pollution of activation rates → fresh hook home per case;
    cooldown behaviour gets dedicated cases.
11. Warm/cold latency conflation → only `search` is timed for latency;
    ingest/setup duration reported separately.
12. Silent-failure zeros published as product results → validate-artifacts
    gains a sanity gate (a provider with zero hits on >50% of queries and no
    skip fails the run) plus a canary query that must hit for every
    non-skipped provider.
13. Journey scripts written to Exomem's happy path → journeys are defined in
    product-neutral event language; each product's mapping table is published
    for review.
14. Basename-collision credit in the Track A scorer → generated corpora
    guarantee globally unique basenames; strict-mode assertion.

## Publication contract

The personal-vault publication rules in [benchmarks.md](benchmarks.md)
(aggregate-only, counts rounded down, no query text/paths/excerpts) continue
to bind any report measured against a real vault. The memory-proof corpus is
synthetic and public by construction, so its runs MAY publish per-query
artifacts, full query text, and per-item traces; the run manifest must state
which regime applies. Private founder regression fixtures
(`benchmarks/private/`) are excluded from git, CI, telemetry, and uploads and
are never aggregated into published numbers.

## Reproduction (skeleton — exact commands land with the implementation)

- Generate: `uv run python benchmarks/run.py generate --seed <seed> --out benchmarks/corpus/generated/<seed>`
- Verify determinism: run generate twice; compare manifests.
- Exomem Track B run: `uv run python benchmarks/run.py run --provider exomem-local --mode wire --profile lexical --corpus benchmarks/corpus/generated/<seed>`
- Track A smoke (from the basic-memory worktree): `uv run bm-bench run retrieval --providers bm-local,exomem-local,baseline-grep --top-k 10 --bm-local-path <worktree> ...`
- Every run writes a fresh `benchmarks/runs/<id>/` (never overwritten) whose
  `manifest.json` + `environment.json` pin commits, versions, profiles,
  prompts, seeds, and failures.

## Scenario-family registry

The scenario-family registry (`benchmarks/membench/families.py`) is the
auditable coverage claim required by OpenSpec change
`expand-memory-proof-benchmark`: every family is classified as
deterministic-oracle (expected records fully computable), rubric-track
(writable but only human/blind-judge assessable, routed to predeclared
rubrics), or out-of-scope (not digitally writable — the declared Polanyi
boundary). A template may only register under an **active** family;
generation fails with a named error for any template whose family is
unregistered or still `planned`, and out-of-scope families are permanently
`excluded`. This table is the verbatim output of
`membench.families.coverage_table_markdown()`;
`tests/test_membench_families.py` fails if it drifts from the module.

| Family | Classification | Status | Rationale |
|---|---|---|---|
| `temporal` | deterministic-oracle | active | Bitemporal ground truth (event vs ingestion time, supersession, expiry, as-of views) is fully computable from the seeded claim timeline. |
| `epistemics` | deterministic-oracle | active | Authority, dispute, tentative lifecycle, retraction, provenance, and absence-vs-unsupported states are derived by the oracle from recorded assertions. |
| `query_behavior` | deterministic-oracle | active | Recall, abstention, and clarification behaviour is checked against oracle-derived expected records for every query kind. |
| `maintenance` | deterministic-oracle | active | Duplicate, contradiction, stale, and orphan pressure is injected by the generation schedule, so corpus-health ground truth is computable. |
| `identity` | deterministic-oracle | active | Alias and entity-graph resolution targets are declared at generation time; the oracle knows every coreference. |
| `multimodal` | deterministic-oracle | active | Numeric and table/PDF evidence carries generated sentinel values; retrieval and answer identity are exact-checkable. |
| `governance` | deterministic-oracle | active | Audience policy and disclosure expectations come from the generated PolicySet; leak vs no-leak is deterministic. |
| `procedural` | deterministic-oracle | active | Ordered how-to chains where step order, preconditions, and revisions over time are the ground truth, computable from the authored chain. |
| `quantitative` | deterministic-oracle | active | Arithmetic over two or more stored values; the oracle computes the expected value, unit, and tolerance with both contributing sources as required citations. |
| `negation_counterfactual` | deterministic-oracle | active | Recorded-as-false vs not-recorded and considered-then-rejected plans score against the existing abstention and current-state gates. |
| `cross_lingual` | deterministic-oracle | active | Synthetic non-Latin-script sources queried in English; sentinel citation and value identity are exact-checkable, and profiles declaring no support report unsupported, never zero. |
| `preference_attribution` | deterministic-oracle | active | Holder and as-of time of an opinion are the ground truth; an unattributed restatement as objective fact fails the calibration gate. |
| `source_reliability` | deterministic-oracle | active | A recurring source's correction track record is derivable from the corpus; weighting is scored behaviourally via required citations and hedging expectations, never numeric confidence. |
| `sub_day_temporality` | deterministic-oracle | active | Which of several same-day records the corpus learned last is computed from captured intra-day instants, so a store that keeps knowledge time only to the day cannot order them; where the instants coincide the oracle returns indeterminate and the expected answer is abstention, never a guess. |
| `relational_inference` | deterministic-oracle | planned | A conclusion no single source states, reached by composing relations across sources: each hop is recorded separately and the terminal fact is named nowhere, so answering requires retrieving a source that never mentions the subject. Every active family asks whether a stored fact survives and returns; none asks whether the store can compose one. The compile plan already derives conclusions deterministically from oracle-held relations, so the expectation needs no model. |
| `long_horizon_entropy` | deterministic-oracle | planned | A 52-week ingestion schedule with recurring duplication, correction, and deletion pressure; health metrics at quarterly snapshots are computable from the schedule. |
| `multimodal_depth` | deterministic-oracle | planned | Facts existing only inside real PDF, OCR-image, or audio-transcript artifacts; sentinel retrieval under the media profile, degrading with recorded reasons without the extras. |
| `tacit_polanyi` | out-of-scope | excluded | Tacit knowledge (the Polanyi boundary): skills and know-how that cannot be written down digitally cannot be seeded into a corpus or checked by any oracle or rubric; declared here so the coverage claim states its own limit. |
