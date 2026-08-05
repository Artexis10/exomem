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

## Addendum — fan-out completion and delegated review (2026-08-01)

The remaining fan-out landed as commit `7ab144c` via a delegated
implementer/reviewer cycle: provider adapters (`graybox-local` live against
the sibling checkout at raw-inbox altitude; `basic-memory-local` over an
injectable `bm` seam, live run user-run; the duck-typed Track-A bridge),
3-tier health scoring and the multi-hop graph family, the committed
`v0.1-seed1` release manifest with a byte-identity regeneration test, the
Track C two-witness join + natural-prompt driver, the Track D J3
weekly-review journey (planted-queue recall/precision, blind rubric, judge
handshake wiring), and the Basic Memory journey mapping doc.

Independent review returned two MAJOR findings — both harness-integrity
holes, both fixed red-first and re-verified by a targeted recheck with
manual reproductions: (1) damaged witnesses (malformed server trace or
transcript) could previously collapse into a `not_activated` *product*
score; either-witness damage is now a `WITNESS_MISMATCH` harness fault
evaluated before any activation branch; (2) a non-zero `claude -p` exit
previously became an empty scoreable answer; failed executions are now
structurally unscorable (`harness_fault=True`, no AnswerRecord).

New product finding from J3 construction: **wall-clock staleness is
unplantable through Exomem's public write surfaces** — `remember` rejects
`created`/`updated` overrides and `edit_memory` re-bumps `updated:` — so
age-dependent behaviour is only testable via the `EXOMEM_STALE_AGE_DAYS`
gate-edge knob. A test-data backdating seam (or acceptance of this as a
deliberate boundary) is a product decision worth recording.

Recorded review debt (non-blocking, from the independent review): bridge
forwards a 200-char excerpt instead of full hit text upstream; graybox
`sys.path.insert(0)` can shadow top-level names process-wide; inconsistent
`.score` access in graybox hit handling; `BASIC_MEMORY_*` env not fully
swept (only HOME/CLOUD_MODE); `bm --version` probe doesn't catch
`TimeoutExpired`; Jaccard 0.8 boundary unpinned by tests (and misworded as
"ceiling"); malformed graybox capture line raises raw instead of
`AdapterEnvironmentError`; J3 `supported` flag hardcoded by mode (a future
lexical contradiction sweep would read as a regression); J3 triage-burden
magic number; attention-queue false-surfaces not gated; live J3 summary not
routed through the judge handshake in tests; graph scorer substring
containment could mask a wrong-hop when values nest; `run_case` doesn't
gate `transcript.malformed_lines` at the driver layer (covered downstream
by the join gate). Release-manifest note: `renderer_versions` pins
`pymupdf: absent` — regenerating with the media extra installed legitimately
changes identity; the committed release pins the deterministic CI
environment.

Validation ledger (group 9): 9.1 `openspec validate --strict` — both new
changes valid, all 31 main specs pass; 9.4 two fresh CLI generations
byte-identical to each other and to the committed release manifest; 9.5
`tests/fixtures/mcp_tool_schemas.json` no-drift, guarded paths
(`tests/golden/`, gate tests, `.github/`, `src/exomem/`) untouched vs
origin/main; 9.2 the full lean suite is sandbox-blocked — in-sandbox it
reports 6097 passed / 738 failed, and every sampled failure reproduces
identically on the untouched primary checkout at v0.36.0 (writer-lease
sqlite store and governance families; the sandbox write allowlist blocks
default state dirs under $HOME), so the 738 are environmental, all 137
membench tests pass, and outside-sandbox `uv run --frozen python -m pytest
-q` is the user-run verification; 9.3 `uvx ruff check . --select F` is
sandbox-blocked (read-only uv cache) — user-run.

## Addendum — retrieval fix delta and recommended-profile results (2026-08-01)

**The headline weakness is fixed and measured.** Product fix `91b016f` on
branch `fix/lexical-degraded-retention` (worktree
`~/projects/exomem-lexical-fix`, base v0.36.0): when the semantic lanes are
structurally absent, the in-KB retention seam now retains a BM25 candidate on
a strict majority of query words present PLUS at least one non-function
content word, instead of the all-stems veto. Fable-delegate review chain:
any-stem branch proven unsatisfiable against five pinned precision contracts
→ coverage-fraction implementation → independent review found stopword-
majority false retention (HIGH, demonstrated at paragraph length) →
content-anchor guard → targeted recheck ALL FIXED; 46 retrieval-family tests
green, latency gate green throughout, active-lane behavior byte-identical.

Three runs over the identical seed-1 corpus and 236-query denominator:

| Dimension (pass/fail) | lexical pre-fix | lexical post-fix (91b016f) | embeddings (pre-fix source) |
|---|---|---|---|
| factual_qa | 0 / 180 | **99 / 81** | **157 / 23** |
| provenance | 0 / 180 | 91 / 89 | 142 / 38 |
| temporal | 28 / 180 | 88 / 120 | 131 / 77 |
| abstention | 52 / 184 | 136 / 100 | 180 / 56 |
| contradiction_uncertainty | 0 / 20 | 0 / 20 | 0 / 20 |
| governance (no-leak) | 16 / 16* | 0 / 16 | 0 / 16 |

\* trivially satisfied by empty answers. With real retrieval the 16
governance fails measure an **ungoverned vault**: the adapter does not yet
translate corpus `policies.yaml` into exomem's opt-in `_Governance/` policy
and the runner does not thread persona identity, so this quantifies the
default-open leak surface, not the shipped governance engine. Wiring
policy translation + per-persona principals (declaring `GOVERNED_VIEWS`) is
the named follow-up lane (landed 2026-08-02 — see the governance-wiring
addendum below). The 0/20 contradiction row and the temporal fails
are the ABSENT capability families (disputed state, bitemporal as-of) doing
exactly the discriminating work the corpus was built for. Runs:
`20260731T163452Z…baseline-lexical`, `20260801T115138Z…postfix-lexical-v2`,
`20260801T073130Z…recommended-embeddings-v2`.

**Environment findings that gated these measurements (all root-caused):**

1. **WSL2 "embeddings crash" = CUDA context initialization segfault** in the
   venv's torch cu132 build — a bare `torch.randn(4,4).to('cuda')` segfaults,
   and even `torch.cuda.is_available()` (called by sentence-transformers
   during device resolution) kills nominally-CPU runs, which retroactively
   explains the recorded "SIGABRT on CPU". Workaround that unblocked
   everything: `CUDA_VISIBLE_DEVICES=` (hide the GPU) + CPU inference. GPU
   proper needs a torch/driver alignment on the host.
2. **`~/.cache/huggingface` and the venv console scripts are stale-state
   hazards**: the HF dir was root-owned (sandbox-era remnant; bypassed via
   `HF_HOME`), and the venv's `exomem` entry point + dist-metadata date from
   an old 0.22.0 install while the editable module code is current 0.36.0 —
   version *strings* in provider metadata are wrong until `uv sync` refreshes
   the install.
3. **Exomem disable-flags are string-truthy**: `EXOMEM_DISABLE_EMBEDDINGS=0`
   *disables* embeddings ("0" is a non-empty string). The membench embeddings
   profile therefore sets the EMPTY string, and a factory test pins that
   exactly one knob differs from the lexical profile. Two mislabeled runs
   (`…baseline-embeddings…`, `…recommended-embeddings-29a546`) measured
   lexical again and are superseded; their dimension-identical scores across
   independent processes are retained as a determinism replication.
4. **A/B measurement trap**: `benchmarks/run.py` inserts the repo's own
   `src` at `sys.path[0]`, silently overriding a `PYTHONPATH` source
   override; fixed-source delta runs must bypass the launcher
   (`python -c "from membench.cli import main; …"` with
   `PYTHONPATH=<fix>/src:<bench>` — the first postfix run measured pre-fix
   code this way and was superseded by `postfix-lexical-v2`).

**Track A integration state:** provider-side fixes committed on the
`exomem-provider` branch (`0b11fab4` hybrid-always, `02096da2` state
isolation, `72e01eb5` skip semantic index when embeddings disabled,
`463191d0` let BM pick search_type and fail loudly on search guidance
strings). The bm-local zeros in runs `ee278ae61cde` (semantic path stalling
~42 s/query) and `66bb0ab040f4` (forced hybrid with semantic disabled) are
integration artifacts, not Basic Memory retrieval quality; the empty-index
diagnosis and verified three-way rerun are in flight and no comparative
Basic Memory number is publishable until that lands green.

## Addendum — governance wiring: wired vs default-open (2026-08-02)

The v0.1 governance gap is closed adapter-side, through public product
surfaces only: the exomem adapter (leaf mode, `governance="wired"`)
translates the corpus `policies.yaml` into the vault's opt-in
`_Governance/` schema-v1 YAML (validated by exomem's own policy compiler),
the runner threads each query's persona to the adapter as a canonical
principal (`owner` = the vault operator; every other persona a
`normalize_audience` principal bound per call with `request_scope`), and
`GOVERNED_VIEWS` is declared only while that wiring is active. Enforcement
is exomem's untouched egress release plane (translated ceiling-0 rules =
silent L0 withhold). Every run now records a three-state
`governance_state` — `wired` / `default_open` / `unsupported` — in
`manifest.json` and `deterministic-scores.json`, and comparative reporting
excludes non-wired runs' governance-family rows (label instead of numbers).

**Wired vs default-open, t16-only seed-1 corpus, leaf mode, lexical
profile** (runs `20260802T103947Z-…-t23-wired-bd2831` and
`20260802T103953Z-…-t23-default-open-bd2efc`, 24 queries, 0 harness
failures, both valid):

| Dimension | wired (pass/fail/n.a./unsupported) | default-open (pass/fail/n.a./unsupported) |
|---|---|---|
| governance (no-leak) | 12 / 0 / 8 / **4** | 16 / 0 / 8 / 0 — labelled, excluded |
| abstention | 8 / 12 / 0 / **4** | 12 / 12 / 0 / 0 — labelled, excluded |
| factual_qa | 0 / 12 / 12 / 0 | 0 / 12 / 12 / 0 — labelled, excluded |
| temporal | 4 / 12 / 8 / 0 | 4 / 12 / 8 / 0 — labelled, excluded |
| provenance | 0 / 12 / 12 / 0 | 0 / 12 / 12 / 0 — labelled, excluded |
| contradiction_uncertainty | 0 / 0 / 24 / 0 | 0 / 0 / 24 / 0 — labelled, excluded |
| behavior | 0 / 0 / 24 / 0 | 0 / 0 / 24 / 0 — labelled, excluded |

**Load-bearing caveat: these tables are near-identical because lexical NL
retrieval is vacuous for every persona.** The headline retention finding
means the corpus's natural-language prompts retrieve zero hits wired or
not, so every query abstains and the default-open run's 16/0 governance
column is the empty-answer artifact already flagged above — which is
exactly why non-wired governance-family rows are now labelled and excluded
from comparative tables instead of rendering as numbers. The only
deliberate deltas are the four `unsupported` markings (next subsection).
Enforcement itself is real and isolable the moment retrieval surfaces
pages — same title-probe query, only the persona varying:

| governed source (variant 0) | persona | default-open | wired |
| --- | --- | --- | --- |
| exec-only compensation memo | owner | retrieved | retrieved |
| exec-only compensation memo | assistant | retrieved | **withheld** |
| board digest (declassified wk8) | owner | retrieved | retrieved |
| board digest (declassified wk8) | assistant | retrieved | retrieved |
| tombstoned standby memo (wk6) | owner | retrieved | **withheld** |
| tombstoned standby memo (wk6) | assistant | retrieved | **withheld** |
| open facilities circular | owner | retrieved | retrieved |
| open facilities circular | assistant | retrieved | retrieved |

Audience restriction, horizon declassification, tombstone-for-everyone
(owner included), and untouched open content each show distinctly. A
wired-vs-default-open rerun with working retrieval (embeddings profile or
the retention fix) is the next measurement of interest.

### Wired governance — known divergence: no time-conditioned rules

**Product finding:** exomem's governance policy v1 has no time-conditioned
rules — nothing in the public `_Governance/` schema (or `govern_memory`)
can express "restricted until date D, open after". The corpus's
`declassify_at` embargo is therefore untranslatable: the wiring snapshots
the policy at the corpus knowledge horizon (the same "now" the fully
ingested vault state represents), which drops t16's board-digest rule
(declassifies week 8 < horizon week 12) and leaves the week-7
pre-declassification withhold expectation with no faithful vault to
measure against.

**Disposition (never a silent lossy snapshot):** every wired run emits
`governance-translation.json` (run root and `provider/`) listing the
documents authored and each dropped rule with `rule_id`, `declassify_at`,
targets, and the reason; the runner joins dropped rules onto the queries
whose forbidden expectations trace to their targets and marks those
queries' `no_leak`/`abstention` gate items **unsupported** with evidence
naming the rule — unsupported-never-zero, never scored pass or fail. That
is the `4` in the wired table's unsupported columns (one week-7 board
query per variant). The week-9 post-declassification queries remain
measured: at the horizon the embargo has lapsed, so the open vault is the
faithful final state. Making the week-7 class measurable requires a
time-conditioned rule (or as-of policy evaluation) through a public exomem
surface — a product decision, recorded here, not a harness simulation.

**Reproduction (single-line commands):**

- `EXOMEM_DISABLE_EMBEDDINGS=1 /home/hugoa/projects/exomem/.venv/bin/python benchmarks/run.py generate --seed 1 --out benchmarks/corpus/generated/s1-t16 --template t16_governance_audiences`
- `EXOMEM_DISABLE_EMBEDDINGS=1 /home/hugoa/projects/exomem/.venv/bin/python benchmarks/run.py run --corpus benchmarks/corpus/generated/s1-t16 --governance wired --label t23-wired --top-k 10`
- `EXOMEM_DISABLE_EMBEDDINGS=1 /home/hugoa/projects/exomem/.venv/bin/python benchmarks/run.py run --corpus benchmarks/corpus/generated/s1-t16 --label t23-default-open --top-k 10`
- Wiring gate suite: `EXOMEM_DISABLE_EMBEDDINGS=1 PYTHONPATH=src /home/hugoa/projects/exomem/.venv/bin/python -m pytest -q tests/test_membench_governance_wiring.py`

## Addendum — reproducibility defect: the release manifest is environment-pinned (2026-08-04)

**Severity: latent — blocks deliverable 4.1 (replication kit) the moment v0.2
release bytes are pinned.** Found while triaging an incidental environment change
during the wave-3 follow-up lanes, not by a test.

**Scope correction (applied after independent review).** An earlier revision of
this addendum claimed the reproduction test currently fails *solely* because the
recorded Pillow version moved. That was wrong, and the error was self-inflicted:
the reproduction command below intersects the two artifact sets (`set(R) & set(N)`),
which structurally hides a set-size mismatch. The committed v0.1 release covers
**17 templates / 200 artifacts / 240 expected records**; the suite now registers
**23 templates**. So today's red is dominated by that count difference — expected,
by design, until the packaging lane cuts a v0.2 release. The environment-pinning
defect below is real and independently measured, but it is **latent**: it bites
the moment a v0.2 manifest is pinned with a renderer version string inside it,
which is precisely why it must be settled before packaging rather than after.

`test_full_suite_seed1_reproduces_committed_manifest`
(`tests/test_membench_release_manifest.py:31`) asserts byte equality of the
*whole* generated `manifest.json` against the committed release file. That file
carries a `renderer_versions` block (`benchmarks/membench/artifacts/__init__.py:52`,
recorded at `generate.py:246`) holding the installed third-party renderer
versions. The committed v0.1 release records `"pillow": "12.2.0"`.

**Measured:** with Pillow **12.3.0** installed, seed-1 generation of
`t15_numeric_multimodal` produced **28 artifacts — markdown, csv and png —
whose `bytes_sha256` and `logical_sha256` are all identical to the committed
release, 0 differing on either axis.** Across the full committed release, all
**200 artifacts common to both manifests match byte-for-byte on both hashes**.
The corpus is bit-for-bit reproducible across the Pillow bump; only the recorded
version string moved. Once v0.2 is pinned and the template-count difference is
gone, that version string becomes the *only* remaining source of drift — a
reproduction failure with no corpus difference behind it.

**Why this matters more than it looks.** The published claim is that a third
party can regenerate our corpus and verify it. Under the current check they run
one command, get `manifest drifted from the committed release identity`, and
have no way to tell a genuine corpus change from a patch-level bump in a
transitive image library. A reviewer looking to dismiss the benchmark does not
need to find a scoring flaw — they can just report that the replication kit
fails on a clean machine. The check is *stricter than the property it defends*,
which makes it worse than a weaker check: it manufactures false drift.

**Second defect, same area — asymmetric hard dependency.** `pillow_version()`
(`artifacts/image.py:18`) performs a bare `import PIL` with no fallback, while
`pymupdf_version() or "absent"` degrades gracefully. Because `renderer_versions()`
is called unconditionally by `generate_corpus`, **every** corpus generation —
including text-only templates that render no image — hard-fails with
`ModuleNotFoundError: No module named 'PIL'` when the media extra is absent.
The lean-suite premise ("no extras required") does not actually hold for
generation; it held only because the development venv happened to carry Pillow.

**Disposition (to implement in the packaging lane, before release bytes are pinned):**

1. Split the reproduction check into two verdicts. **Corpus identity** —
   templates, master seed, counts, and every artifact's `logical_sha256` and
   `bytes_sha256` — must match exactly and remains a hard failure.
   **Environment provenance** — `renderer_versions`, `generator_version` — is
   recorded, diffed, and reported as an *environment difference*, never as
   corpus drift. The dual-hash design already present in the manifest is what
   makes this split honest: `logical_sha256` is renderer-independent by
   construction, so a renderer swap that preserves logical content is
   distinguishable from one that does not.
2. Make `pillow_version()` degrade to `"absent"` like pymupdf, and have
   generation route image artifacts through the existing `degradations`
   machinery when the media extra is missing — a replicator without extras must
   get a labelled, degraded-but-valid corpus plus an explicit
   `N artifacts degraded (media extra absent)` line, not a stack trace.
3. The replication kit must state the extras required for **byte-identical**
   reproduction, and the publication gate must label any figure produced from a
   degraded-artifact corpus.

**Falsification value.** This is a case where the harness was wrong in the
direction that makes *us* look bad rather than good, so it would never have been
caught by checking whether results flatter Exomem. It was caught only because a
lane reported an environment workaround instead of silently installing the
dependency and moving on.

**Reproduction (single-line commands):**

- `uv run python -c "import PIL; print(PIL.__version__)"`
- `uv run python -c "import sys; sys.path.insert(0,'benchmarks'); from membench.generate import generate_corpus; m=generate_corpus(1,'/tmp/probe_p1',template_ids=['t15_numeric_multimodal']); print(m.model_dump()['renderer_versions'])"`
- `uv run python -c "import json; r=json.load(open('benchmarks/corpus/releases/v0.1-seed1.manifest.json')); n=json.load(open('/tmp/probe_p1/manifest.json')); R={a['source_id']:a for a in r['artifacts']}; N={a['source_id']:a for a in n['artifacts']}; c=sorted(set(R)&set(N)); print('committed',len(R),'regenerated',len(N),'common',len(c),'only-committed',len(set(R)-set(N)),'only-regenerated',len(set(N)-set(R)),'bytes-differ',sum(R[s]['bytes_sha256']!=N[s]['bytes_sha256'] for s in c),'logical-differ',sum(R[s]['logical_sha256']!=N[s]['logical_sha256'] for s in c))"`
  — note the set-size terms: an intersection-only comparison silently hides a
  template-count difference, which is the mistake the scope correction above fixes.

## Addendum — the LLM judge does not earn its place (2026-08-05)

**Disposition: `semantic_match` should not be a scored dimension.** Measured, not
argued. Raised by the human labeller mid-pass ("I'm sure Sonnet 5 would do a
great job — I don't see how it would fail"), which turned out to be the right
question asked of the wrong thing: the issue is not whether the judge is *good*,
it is whether it is *doing anything a deterministic gate does not already do*.

**Setup.** All 240 seed-1 queries from the `postfix-lexical-v2` run, blinded
through the judge's own `normalize_for_judge`, graded by Claude Sonnet 5 across
six lanes, 240/240 verdicts, 0 errors. Verdicts and the comparison script are
committed under `benchmarks/judge-agreement/judge-vs-gates/`.

**Scope caveat, stated up front:** one model, and the lanes graded in batches of
40 rather than one call per item, which makes the judge *more* self-consistent
than production would be. Every number below is therefore the judge's
best case.

### 1. It reproduces a free deterministic check

| comparison | n | raw agreement | Cohen's κ |
|---|---:|---:|---:|
| `semantic_match` vs `gate_value` | 180 | 0.994 | **+0.989** |

179 of 180 identical. `gate_value` is a substring check that costs nothing, is
deterministic, and cannot hallucinate. At κ = 0.989 the judge is an expensive,
non-deterministic reimplementation of it.

### 2. Its single deviation is a fabrication

The one disagreement, `QRY-E5122D20` (temporal, as-of): *"What was the pilot
budget for Project Sablereach as of week 3, before the amendment?"*, expected
`48000`. `gate_value` failed it. The judge passed it, reasoning:

> "Steering decision doc gives 48000 as the original Sablereach pilot budget
> clause, matching the expected pre-amendment value."

**The string `48000` does not occur anywhere in the response.** The text says
the Sablereach pilot budget "is now 51000 credits" — the *amended* value — beside
budgets for two unrelated projects. The judge invented the expected figure,
attributed it to a document, and confirmed itself. n = 1, so this is an
existence proof rather than a rate: the judge's only independent contribution
across 180 comparable rows was a confident false positive on a temporal query.

### 3. It is blind to supersession, which is the benchmark's core subject

44 responses contain a **retired** value. The state gate fails all 44. The judge
returned `semantic_match: true` on **23 of them** — it is passing answers the
benchmark scores as wrong.

This is structural, not a model defect, and no stronger model fixes it: the
prompt asks *"does the candidate convey the expected answer"*, which is a
**presence** question. A response containing both the current and the superseded
value satisfies presence while asserting something false. Temporal correctness is
not expressible in the judge's framing.

### 4. Its apparent unique coverage is not coverage

60 rows have `gate_value` not-applicable and a judged verdict — the judge's only
exclusive territory. All 60 have expected `kind: none` (no answer recorded); 52
expect abstention, and `gate_abstention` already decides 56 of the 60
deterministically. The 28 `match` verdicts there mean "correctly said nothing",
which the abstention gate scores directly. No information is added.

### Disposition

Drop `semantic_match` from scored output. It is redundant where it agrees
(κ = 0.989 with a free gate), fabricates where it deviates (1 of 1), wrong in the
one direction the benchmark exists to measure (23 of 44 supersession rows), and
adds no coverage where it is alone (60 of 60 already gated). Removing it also
removes the benchmark's softest published claim and the entire judge–human
agreement dependency.

**Consequence for the human labelling pass:** stood down at 17 of 50 labels.
Those labels are retained (`benchmarks/judge-agreement/labels-human.json`) as
evidence for the record, not as an agreement statistic — measuring human
agreement with a dimension we are removing would be spending a person's attention
to validate something already disqualified on structural grounds.

Cost note, since it bears on any future judged dimension: the supported
`ClaudeCliBackend` path pays ~32k cache-creation tokens per invocation with no
warming across calls — a flat ~$0.20 to grade an ~855-character prompt, ~$48 for
one pass over 240 queries, ~96% of it bootstrap. A judged dimension has to earn
that; this one did not.

## Correction to the 2026-08-05 judge addendum — claim 3 is withdrawn

Prompted by the operator asking whether better prompting would fix the judge.
Testing that question falsified one of my own published claims, so the
correction comes first and the disposition is reopened.

### The experiment

The judge prompt was rewritten with an explicit supersession clause — *"the
candidate may contain several conflicting values … presence is not enough … set
`semantic_match` true only if the candidate asserts the expected value as the
answer and does not also assert a conflicting value"* — giving the judge no
oracle knowledge, only a change of question from **presence** to **assertion**.
Re-run over the same 44 rows: **44/44 identical verdicts, zero flips.**

That looked like confirmation. It was not. It prompted a check of *who was
right*, which had been assumed rather than tested.

### What the disputed rows actually contain

`QRY-1CCF65F0`, identity, current-truth:

> **Question:** "What is the current official name of the project once called
> **Project Driftreach**?"
> **Response:** "Project Driftreach is renamed to Project Emberfall. All
> artifacts now use the Project Emberfall name."
> **`gate_state`:** FAIL — *forbidden value 'Project Driftreach' present*

The response is correct. It failed for echoing a name **the question itself
supplies**. Four queries have this shape (all `identity`): the prompt names the
old project, the gate forbids that name, so **no possible correct answer can
pass**. Guaranteed failure for every contender, in perpetuity.

The other 19 are a different defect. Example, `QRY-20481782`: the response
returns the original hosting decision (*"…is Petra Group"*) **and** the reversal
memo (*"the earlier hosting decision is fully reversed … is now Lumo Group"*).
`gate_state` fails it because the retired value appears anywhere in the text.
But this run's answerer is **extractive** — it returns retrieved documents, and
returning a record together with its supersession is reasonable retrieval
behaviour, arguably better than hiding the history.

**Measured across the whole run: 23 of 120 state-gate failures (19%) are
responses containing every required current value, failed solely because a
superseded value is also present** — `mini_smoke` 4, `temporal` 8,
`maintenance` 4, `identity` 4, `multimodal` 3.

### Two harness defects, one of them the worst class again

1. **Self-defeating forbidden values** (4 queries): the forbidden value occurs in
   the query text. Unwinnable by construction. This is the sixth instance in
   this project of the harness scoring correct product behaviour as failure.
2. **Mode mismatch**: `gate_state`'s "forbidden value absent from answer text"
   rule presumes an *assertive QA* answer. Applied to an *extractive/retrieval*
   answer it punishes returning provenance. The gate is applied uniformly across
   modes today, so every `current_state`/`as_of` figure in the lexical profile
   inherits this.

### Consequences for the judge disposition

Of the four disqualifying results in the addendum above:

- **Claim 1 (redundant, κ = +0.989 vs `gate_value`) — stands.**
- **Claim 2 (its one independent deviation was a fabrication) — stands.**
- **Claim 3 (blind to supersession; passed 23 answers the benchmark scores as
  wrong) — WITHDRAWN.** Those answers were not wrong. The judge was
  distinguishing *resolved* from *unresolved* conflict, which is real signal
  `gate_state` cannot currently express. I asserted the gate was correct without
  checking it, which is precisely the error this benchmark exists to catch.
- **Claim 4 (its exclusive coverage is already gated) — stands.**

**Disposition reopened.** Dropping `semantic_match` (4b.20) is on hold. The
honest order is: fix the two `gate_state` defects, re-run, and re-measure the
judge against a gate that is itself correct. A dimension must not be removed on
evidence that turned out to be partly a defect in what it was compared against.

The strategic point survives intact and is now better supported: a judge earns
its place only where the oracle is silent — and part of what looked like judge
error was the *gate* being silent about a distinction the oracle can express.

## Resolution — the judge does earn its place, scoped (2026-08-05)

Third and final position on this question. The first two were wrong in opposite
directions; this one is measured against a gate that is itself now correct.

### The controlled probe

With `gate_state` fixed (`ae0a6ff`), 19 rows report **UNSUPPORTED** — the harness
stating plainly that it cannot prove those answers right or wrong. That is the
only territory where a judge can add anything, so the question became: does the
judge actually *discriminate* there, or does it say "match" whenever the expected
value appears?

One variable changed, nothing else:

```
correct  : "It was previously 2025-03-14, and it is now 2025-03-28."
backwards: "It was previously 2025-03-28, and it is now 2025-03-14."
expected : 2025-03-28
```

Both candidates contain both values. Both are UNSUPPORTED to the gate. Each of
the 19 rows was graded in both directions under two prompts — plain, and one
carrying an explicit supersession clause. 76 items, 0 errors.

| direction | prompt | n | said match |
|---|---|---:|---:|
| correct | plain | 19 | **19** |
| correct | strict | 19 | **19** |
| backwards | plain | 19 | **0** |
| backwards | strict | 19 | **0** |

**19/19 discriminated correctly under both prompts.** Perfect separation, no
false positives on the reversed direction.

### What this changes

The judge reads supersession *direction* — which value is asserted as current —
and no deterministic rule available here can. The gate lane examined and rejected
assert-position for exactly this reason: the corpus's own `expect_change` family
requires both values present, so the same text is correct under one framing and
wrong under another with nothing textual to key on. That is a genuine capability
gap, and the judge fills it.

**Prompt framing is not the variable.** Plain and strict were identical here, as
they were on the earlier 44-row rerun (44/44, zero flips). The judge was doing
this all along; two of my three positions on it were wrong because I was
measuring it against a broken gate, not because the judge changed.

### Revised disposition

**Keep `semantic_match`, scoped to rows where a deterministic gate reports
UNSUPPORTED.** This is non-redundant by construction:

- Where `gate_value` decides (180 rows) it stays unscored — κ = +0.989 there, an
  expensive reimplementation of a free check.
- Where `gate_state` reports UNSUPPORTED (19 rows) it is the only thing that can
  rule, and it rules correctly.
- Deterministic gates remain final. The judge never overrides one — the
  architecture already forbids it, and the one measured fabrication
  (`QRY-E5122D20`, inventing `48000`) is exactly why that constraint must hold.

Supersedes 4b.20 as originally written: the dimension is scoped, not removed.

### Stated limit

This probe used **clean one-sentence candidates** where direction is unambiguous.
The real 19 rows are multi-document dumps. Perfect discrimination on synthetic
sentences is an **upper bound**, not proof the judge holds up on the messy case.
The honest next measurement is the same swap applied to the actual response text.
Until that runs, the scoped dimension should be reported with this caveat
attached, not as a settled capability.

### Record of the reversal

Position 1 (f86a37e): drop the judge — four disqualifying findings.
Position 2 (1dde2d4): finding 3 withdrawn, disposition reopened, gate found
defective.
Position 3 (this): judge validated for a narrow job against a corrected gate.

The operator drove all three corrections by pushing back on conclusions that
looked settled. Recorded because the process point outlives the result: two of my
three positions were confidently argued and wrong, and both were wrong because I
had assumed the deterministic baseline was correct without testing it.
