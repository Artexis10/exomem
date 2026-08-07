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

## Addendum — a run directory cannot reproduce its own result (2026-08-05)

**Blocks deliverable 4.1 (replication kit).** Surfaced by an implementation lane
reporting live retrieval returning nothing, then verified independently.

### What was measured

Same corpus, same profile, same vault, same product source — opposite results.

| | Aug-1 run | today |
|---|---:|---:|
| queries | 236 | 236 |
| **total hits** | **452** | **0** |
| zero-hit rows | 96 | 236 |
| non-empty answers | 140 | 0 |
| abstained | 96 | 236 |

Controls, each checked rather than assumed:

- **Product source is byte-identical.** The Aug-1 run recorded
  `repos.exomem.head = bc6cfac`, which *is* an ancestor of today's HEAD, and
  `git diff bc6cfac HEAD -- src/exomem` is empty. Not a product regression.
- **Profile settings are byte-identical** — all twelve `neutral-lexical` knobs,
  same `exomem_version`, `mode`, `search_style`.
- **The vault is intact.** Pointing *today's* code at the *Aug-1 vault* that
  produced 452 hits returns **0**. A bare entity-name query (`"Project
  Quarrypoint"`) against 200 sources also returns 0 — total retrieval failure,
  not a phrasing effect.
- **Not governance wiring.** The ungoverned search path is unchanged, both vaults
  contain only a `_Governance/README.md`, and the probe took the ungoverned
  branch.
- **Not the deferred index.** The working Aug-1 vault has the identical shape
  (`semantic_upserts` populated, `full_upserts = 0`, no lexical sidecar). This was
  an intermediate hypothesis of mine and it was wrong.
- **Not the machine config** (`~/.exomem/config.json` is `{"schema":1,"mode":"normal"}`).

### The conclusion that matters

Identical source plus identical inputs producing a different result means the
difference lives in the **environment** — installed dependencies — and the venv is
known to have been re-resolved since (Pillow moved 12.2.0 → 12.3.0 in the same
period, recorded in the environment-pinning addendum above).

Which yields the finding: **a run directory does not contain enough to reproduce
its own result.** It captures the corpus, the vault, the profile knobs, the
product commit and the answers — and still cannot be replayed, because the vault
carries no persistent lexical index and retrieval rebuilds it through a
dependency stack the artifacts do not pin. The three sqlite files present
(`.deferred-index`, `.refs`, `.graph`) are identical between the working and
failing vaults.

This is the third reproducibility defect found in this project, after the
environment-pinned release manifest and the blinding fingerprint, and it is the
most serious: the other two produce *misleading* results, this one makes a
published result **unverifiable by anyone, including us**.

### Not root-caused — stated plainly

The specific dependency has not been identified. Investigation stopped
deliberately once the class of cause was established, because the disposition
does not depend on which package it is: the replication kit must pin and record
the full dependency set, not just the product commit, or every published figure
is a claim no one can check. Tracked as 4b.24.

**Consequence for anything published from this profile:** the Aug-1 lexical
numbers remain the best available record, but they are currently
**unreproducible on the machine that produced them**, and must be labelled as
such until 4b.24 lands.

### Root cause — retrieval is process-history dependent

Continued from the addendum above, which stopped at "the cause is environmental."
That was wrong, and the real cause is worse.

**Measured, same vault and same query throughout:**

| context | bm25 lane | `op_ask_memory` |
|---|---:|---:|
| cold process, the failing run's own vault | 10 | **2** |
| cold process, the Aug-1 vault | 10 | **2** |
| process that has just written to the vault | 10 | **0** |

The BM25 lane returns the correct documents ranked 1–2 (`kickoff-brief` 19.078,
`replan-memo` 18.919) in **every** case. What changes is whether they survive the
retention seam (`find.py:2669`, `find_results.stem_tokens_present`) — instrumented
directly: **46 retention checks, 0 passed**.

So a benchmark run — which ingests ~200 documents and then queries **in the same
process** — gets zero hits, while any cold process querying the identical vault
on disk retrieves normally. `bm25.clear_cache()` does not restore it, so the
stale state is not the BM25 corpus cache.

**This supersedes the environmental hypothesis.** Nothing about dependencies had
to change: Aug-1's 452 hits and today's 0 are the same code with different
process histories. Ruled out along the way, each with evidence: product source
(byte-identical), profile knobs (all twelve, and a per-kwarg bisection — 2 hits
in every combination), corpus size, governance wiring, deferred index, machine
config, and `rank_bm25` availability.

**Why this matters beyond one broken run.** A memory system whose retrieval
depends on whether the querying process previously wrote to the vault is
non-deterministic in the dimension the benchmark exists to measure. And a
harness that ingests and queries in one process measures that artefact rather
than the product. Every lexical-profile number this project has published was
produced by an ingest-then-query process.

**Harness mitigation (benchmark-side, implementable now):** query from a process
that did not perform the ingest — split the phases, or re-exec between them. This
is verified by construction: the cold-process probes above are exactly that
arrangement and they retrieve.

**Product question (not ours to answer here):** why write-side state suppresses
retention for the writing process. Recorded as a finding, not a diagnosis.

**Loose end, stated:** a search against a freshly-created empty vault returned 2
results, which should be impossible and suggests a vault-path fallback somewhere.
Not chased. It does not affect the above — every comparison in the table uses a
populated vault at an explicit path — but it wants its own look.

### Correction — it is the interpreter, not process history

The "process-history dependent" root cause above is **withdrawn**. It did not
survive its own test: performing `op_capture_source` writes in the same process
and re-querying returns **2 hits before and 2 hits after** — writing does not
break retrieval. That conclusion rested on an earlier probe whose vault was
structurally incomplete (it returned 2 results while *empty*, which should be
impossible — the loose end flagged at the time was in fact invalidating the
experiment, not incidental to it).

**What actually happened, measured:** replaying the Aug-1 run's own queries
against the Aug-1 run's own untouched vault, today:

| query | Aug-1 hits | today |
|---|---:|---:|
| "What is the current delivery deadline for Project Quarrypoint?" | 8 | **0** |
| "How many points did the yield score for Project Quarrypoint measure…?" | 4 | **0** |
| "What is the current delivery deadline for Project Cinderrun?" | 8 | **0** |

Same vault on disk, same query text, byte-identical `src/exomem`. The one
difference the run artifacts record:

```
environment.json  python:  3.12.3   (Aug-1)
today                      3.14.6
```

The worktree venv was rebuilt on a new interpreter, re-resolving every dependency
with it. That is sufficient to explain the change and nothing else survives as a
candidate — product source, profile knobs (all twelve, plus per-kwarg bisection),
corpus, vault, governance wiring, deferred index, machine config, `rank_bm25`
availability, and the stemmer (snowball, pure-Python, verified producing
identical stems) are all ruled out by direct test.

**The precise mechanism inside the retention seam is not isolated.** Doing so
needs a side-by-side 3.12 environment, which this worktree cannot build. Recorded
as a product question, not diagnosed.

**Why this is still the reproducibility finding, and a sharper one.**
`environment.json` *recorded the interpreter version all along*. The artifact was
sufficient to detect this; nothing ever compared it. A replication kit that pins
the product commit and the corpus seed, but not the interpreter and dependency
set, produces exactly this: a published number that silently stops reproducing
and looks like a product regression when it is an environment change. That is
what 4b.24 must fix — pin and *verify* the full environment, and fail loudly on
mismatch rather than reporting a plausible-looking zero.

**Two wrong root causes were published before this one** (a version gap between
worktree and main; process-history dependence). Both were stated with evidence
and both were wrong. They are left in the record above rather than deleted,
because the pattern matters more than the tidiness: each died to a control, and
the correct answer was in the run artifacts from the beginning.

## Addendum — the environment gate, and the vacuous passes it found (2026-08-05)

4b.24 implemented (`d29ec42`). Two guards, and the second immediately found a
defect in this project's own test suite.

### Guards

**Environment pin-and-verify.** Full distribution capture (81 here) plus the
product's runtime closure. Two tiers on one principle: *block only where nothing
else in the artifacts can independently establish that the difference did not
matter.* Blocking — interpreter version and implementation, `exomem_version`,
repo head and dirty flag, every `EXOMEM_*` knob, and distributions inside the
runtime closure. Reported — everything else, `platform` included, since it
embeds the WSL kernel patch level and blocking on it would manufacture invalid
runs from an OS update.

This is the same rule as the release manifest (4b.7) reaching the opposite
verdict, and the asymmetry is the point: corpus identity has an independent check
— dual artifact hashes proved 200/200 identical across a Pillow bump — so
renderer versions are provenance. Retrieval behaviour has no such check, so the
interpreter blocks.

Two safeguards against a guard so strict it gets switched off: blocking only
applies to a run that *claims a reproduction* (`reference_environment` is opt-in),
and distribution blocking follows extras only when requested — following every
extra everywhere pulls `pyjwt`'s dev extra through to pytest and blocks 81 of 81
installed distributions, i.e. the whole venv. The scoped rule blocks 75.

**Retrieval floor.** Zero queries with hits, over at least 10 attempted, is
INVALID. Exactly zero is the only threshold requiring no assumption about a
contender's competence: it is the absence of signal rather than a degree of
badness. A contender managing 1 hit in 236 is dreadful *and measured* — scored and
flagged, never dismissed as broken.

Verified on the real artifacts: Aug-1 versus today reports `blocking_mismatch` on
`python_version`, repo head and dirty flag; the zero-hit s1 run
(236 queries, 0 hits) becomes `floor_violation → INVALID`. Neither is a contender
loss — `invalid=True`, `run_failures == 0`, dimensions withheld, the fault
recorded in `failures.jsonl` but uncounted.

### What the floor guard caught in our own suite

Five previously-green tests now fail, correctly. They are real-adapter runs
asserting `not result.invalid` over runs that retrieve **nothing**:

- `test_membench_adapter_exomem.py::test_leaf_run_end_to_end_produces_valid_run_dir`
  — 0 hits on all 16 retrieval queries
- four tests in `test_membench_governance_wiring.py` — 0 hits on all 24

**The governance ones are the serious case.** A run that retrieves zero documents
trivially satisfies "the withheld content was not returned". Independently
confirmed on the zero-hit s1 run recorded earlier in this document, which
published `governance: {pass: 16, fail: 0}` — sixteen governance passes earned by
returning nothing at all.

This is a new shape of this project's recurring defect. The previous eight
instances were the harness scoring *correct product behaviour as failure*. This
is the inverse: the harness scoring *a non-measurement as success*. Same
underlying disease — the harness measuring itself rather than the product — and
the inverse form is more dangerous, because a false failure gets investigated and
a false pass does not.

**Disposition: left red deliberately.** They are currently the loudest available
signal that this machine cannot retrieve. Converting them to
`pytest.skip(invalid_reason)` would be one line each and would risk masking a
genuine product regression later. They go green when a 3.12 environment exists,
which is the remaining half of 4b.24.

### Correction 2 — it is not the interpreter either; the Aug-1 run is simply not reproducible

A Python **3.12.3** environment was built (the exact version `environment.json`
records for the Aug-1 run, from `/usr/bin/python3.12`) and synced from the
committed `uv.lock` with `uv sync --frozen`. The lock is unchanged since Jul 31,
before the Aug-1 run.

Result: **0 hits**, on all three replayed Aug-1 queries. The interpreter root
cause published above is **withdrawn**.

Everything reconstructible has now been ruled out by direct test, each replaying
the Aug-1 run's own queries against the Aug-1 run's own vault:

| variable | control | result |
|---|---|---|
| interpreter | 3.12.3, exact match to the record | 0 hits |
| dependency set | `uv sync --frozen`, lock unchanged since Jul 31 | 0 hits |
| product source | `git diff bc6cfac HEAD -- src/exomem` empty | 0 hits |
| **adapter** | the literal `bc6cfac` version, run from a scratch copy | **0 hits** |
| search kwargs | `_search_kwargs` / `_NEUTRAL_SEARCH_KWARGS` byte-identical at both commits | 0 hits |
| query text | `query.prompt_text`, exactly what the runner passes | 0 hits |
| vault | index files untouched since Aug 1 14:52 | 0 hits |
| profile knobs | all twelve, plus a per-kwarg bisection | 0 hits |

The Aug-1 hits are unambiguously real: rank 1, genuine excerpt, a
`provider_path` that still exists in the vault.

**Conclusion: the Aug-1 run cannot be reproduced, and we cannot determine why.**
That is the finding — not a placeholder for a better one. Some state that no
artifact captured determined whether retrieval worked, and it is gone.

**Four root causes were published for this failure before this one, all wrong:**
a worktree/main version gap; process-history dependence; the interpreter; each
stated with evidence, each killed by a control. They are left in the record
above. The pattern is the lesson — every one of them was a plausible story that
explained the symptom, and the discipline that mattered was building the control
rather than believing the story.

**Disposition: retire the Aug-1 numbers rather than explain them.** They are
unreproducible on the machine that produced them, by any reconstruction available,
so they cannot be published regardless of cause. The correct next step is not more
archaeology — it is a **fresh baseline produced under the now-verified
environment gate**, which records the full distribution set and fails loudly on
mismatch. That run will be reproducible by construction; this one never can be.

A Python 3.12 environment now exists at `.venv-312` (gitignored) if a future
bisection wants it.

**What this episode actually establishes**, and the reason the guards matter more
than the diagnosis: a benchmark run captured its corpus, vault, product commit,
profile, answers and interpreter — and still could not be replayed six days later
on the same machine. Everything the replication kit was going to promise was
already false, silently, and nothing detected it. The environment gate and the
retrieval floor exist so the next occurrence announces itself instead of
publishing 236 plausible zeros.

### Cause found — `EXOMEM_DISABLE_CLIP=1` zeroes text retrieval

The fifth hypothesis is the right one, and it was in the benchmark's own profile
the whole time.

**Measured**, 20 real corpus queries, one fixed vault:

| | hits |
|---|---:|
| the original run recorded | 52 |
| today, **without** `EXOMEM_DISABLE_CLIP` | **40** |
| today, **with** it | **0** |

A flag that reads as an image-search switch takes *text* retrieval to zero. The
benchmark's `lexical_profile()` set it on every run.

**Why four root causes came and went first.** The adapter applies profile
settings *temporarily* — `_set_env` (`exomem_local.py:193`), restored in
`cleanup()` — while `capture_environment` reads `os.environ` **outside** that
window. Every run artifact therefore recorded only `EXOMEM_DISABLE_EMBEDDINGS`,
and the flags actually in force were never written down.

And the trap that cost the most: the profile was reconstructed *faithfully from
the run manifest* in every probe, which meant setting the very flag that broke
it. Every control — interpreter, locked dependencies, product source, the
original adapter — correctly answered "not this". All true, all useless: the bug
was riding inside the control.

**Two defects.** Product: a CLIP/image switch suppressing text retrieval is wrong
independent of this benchmark. Harness: we set it. Removed, with a comment
forbidding its return without a test proving retrieval survives it.

### What removing it revealed

The verification run came back:

```
invalid=True  reason=environment: scored response carries degraded marker: ['clip']
```

Without the flag, exomem reports the CLIP lane degraded (no
`sentence-transformers`) and the adapter refuses to score a degraded response —
correctly. So the flag was suppressing the *symptom* of a missing dependency
while destroying text retrieval as a side effect.

Both states are unscoreable, but they are not equally bad:

- **with the flag**: 236 plausible zeros, `invalid: false`, silently wrong
- **without it**: INVALID with a named cause, loudly wrong

That is the correct trade and the change stands. This machine still cannot
produce a valid lexical-profile run until `sentence-transformers` is installed
(or the product stops degrading text retrieval when CLIP is absent) — but it now
*says so* instead of publishing zeros.

**Gap in the environment gate, stated:** it captures `env_knobs` from ambient
`os.environ`, outside the adapter's apply/restore window, so it would miss this
exact class again. Effective profile settings must be captured where they are
applied.

### Resolution — the product fix exists and is unmerged

`EXOMEM_DISABLE_CLIP=1` is **not** a benchmark mistake. It is a correct
determinism pin that requires product fix **`91b016f`**, which is written,
reviewed, and sitting unmerged on `fix/lexical-degraded-retention` (authored
2026-08-01, **not** on `origin/main`).

Its own commit message describes this failure, citing this benchmark:

> "When the semantic lanes are structurally absent (embeddings disabled, index
> empty, or lane degraded), the in-KB retention seam previously vetoed every
> BM25 candidate lacking ALL query stems — natural-language questions retrieved
> nothing (benchmark: factual_qa 0/180 while BM25 ranked the right document
> first)."

**Verified by overlaying that commit's `find.py` onto a scratch copy**, CLIP
disabled, the same 20 queries and the same vault:

| | hits |
|---|---:|
| without `91b016f` | **0** |
| with `91b016f` | **52** |
| the August baseline recorded | **52** |

An exact reproduction of the original numbers. The August run was executed
against a checkout carrying that fix; this worktree branched from
`84f1f63` (release 0.36.0, 2026-07-31) and never had it.

**Mechanism.** A *disabled* lane never fails, so the BM25-only fallback at
`find_candidates.py:242` never triggers, and the strict retention seam vetoes
every candidate lacking ALL query stems — which no interrogative phrasing
satisfies. A lane that *fails* is rescued by the fallback; a lane that is
switched off is not. `91b016f` replaces all-stems with majority-coverage
retention for exactly this case.

**Disposition.** The flag is restored, with the dependency recorded in the code.
Dropping it is not the workaround: without it CLIP reports `degraded` and the run
is refused for a misleading reason. Until `91b016f` reaches `origin/main`, the
retrieval floor makes the failure loud instead of silent — which is the whole
point of that guard.

**The action is a merge, not a patch.** Nothing needs writing.

**And the honest reading of this whole episode:** the benchmark did its job. It
detected a real product defect, produced the number that appears verbatim in the
fix's commit message (`factual_qa 0/180`), and then — six days later, on a
checkout without the fix — detected it *again* and refused to publish. The five
withdrawn root causes were mine, not the harness's.

## First valid baseline (2026-08-05)

Run `20260805T135132Z-exomem-local-baseline-postfix-lexical-109d39`, seed-1 corpus,
lexical profile, default-open governance, `invalid=False`, 236 queries scored,
0 harness failures.

**Retrieval reproduces the August figures exactly** — 452 total hits over 236
queries, 140 with hits — confirming that `888eaab` (PR #378) restores the original
behaviour rather than merely changing it.

| dimension | pass | fail | n/a | unsupported |
|---|---:|---:|---:|---:|
| factual_qa | 99 | 81 | 56 | 0 |
| temporal | 92 | 97 | 28 | **19** |
| abstention | 136 | 100 | 0 | 0 |
| provenance | 47 | 149 | 40 | 0 |
| contradiction_uncertainty | 0 | 20 | 216 | 0 |
| governance | 0 | 16 | 220 | 0 |

**Read the governance row correctly.** It moved 16 pass → 16 fail, and that is
not a regression. This is a *default-open* run: the previous 16 passes were
vacuous, earned by retrieving nothing (a run returning zero documents trivially
satisfies "the withheld content was not returned"), and the 16 failures now
merely state that an ungoverned vault is open. Neither number measures the
product, which is why default-open governance rows are excluded from
cross-product comparison. Governance is measured under `--governance wired`.

**Weakest real dimension: provenance at 47/196.** Citation precision is now
scored (4b.8), so this is the first figure that reflects both recall and
precision. `contradiction_uncertainty` at 0/20 is the other honest zero.

**The 19 temporal UNSUPPORTED** are the rows where `gate_state` cannot decide —
exactly the set the scoped judged dimension resolves, and the reason it earns its
place.

**Why this baseline is different from every prior one.** It carries a full
environment capture (81 distributions plus interpreter version), it passed the
retrieval floor rather than publishing zeros, and its predecessors were all
produced under a configuration that silently zeroed retrieval. Every number this
project reported before today came from a broken run.

**Not yet solved: run artifacts are gitignored.** `benchmarks/runs/` is untracked,
so this baseline is not version-controlled and cannot be diffed against a future
one. The operator asked for run reports to be tracked repo files; that publishing
step (a `publish` subcommand writing `report.md` + `manifest.json` +
`deterministic-scores.json` under a committed path) is still owed and belongs with
the packaging lane.

**Caveat on status:** this is a pre-4.5 baseline. The sub-day temporality family
changes generated corpus bytes, so if it lands this must be regenerated before
being published as the v0.2 reference.

### Reproducibility confirmed against the August run

Today's baseline diffed against `20260801T115138Z-…-postfix-lexical-v2`, across
four days, two Python interpreters, a rebuilt venv, and a merged product fix:

| dimension | Aug-1 | today | |
|---|---|---|---|
| factual_qa | p=99 f=81 n/a=56 | p=99 f=81 n/a=56 | **identical** |
| abstention | p=136 f=100 | p=136 f=100 | **identical** |
| contradiction_uncertainty | p=0 f=20 n/a=216 | p=0 f=20 n/a=216 | **identical** |
| governance | p=0 f=16 n/a=220 | p=0 f=16 n/a=220 | **identical** |
| behavior, _run | — | — | **identical** |
| provenance | p=91 f=89 n/a=56 | p=47 f=149 n/a=40 | changed |
| temporal | p=88 f=120 n/a=28 | p=92 f=97 n/a=28 u=19 | changed |

Retrieval also reproduces exactly: 452 hits over 236 queries, 140 with hits.

**Both deltas are attributable to deliberate scorer changes, and the arithmetic
closes.** Temporal: 88+4 = 92 pass (the four identity queries no correct answer
could previously pass), 120−19−4 = 97 fail, 19 → unsupported (co-presence with a
documented predecessor). Provenance: citation precision is now scored, and
`n/a` 56→40 is the 16 rows that became measurable when precision began being
computed wherever a claim basis exists.

Nothing moved that should not have.

**Scope of the claim, stated precisely:** this is *same-machine* reproducibility.
Cross-machine is untested and is exactly what 4.1's replication kit must prove.
The environment gate now records 81 distributions plus the interpreter, so a
mismatch announces itself rather than silently producing different numbers — but
announcing is not the same as proving, and the kit is still owed.

## Profiles degrade only exomem — cross-product comparison under them is invalid (2026-08-05)

**Found on the first real head-to-head.** A `neutral-lexical` run of exomem and a
`neutral-lexical` run of basic-memory are not comparable, because the profile is
defined as a bag of **exomem-specific environment variables**:

```
EXOMEM_DISABLE_EMBEDDINGS=1, EXOMEM_VEC_BACKEND=numpy,
EXOMEM_LEXICAL_BACKEND=python, EXOMEM_DISABLE_CLIP=1, …
```

None of those mean anything to a competitor. So "lexical profile" actually means
**exomem with its semantic lane switched off, and every contender at full
strength.** Confirmed from the run artifacts: basic-memory's provider workdir
contains `fastembed_cache/models--qdrant--bge-small-en-v1.5-onnx-q` — it
downloaded and used an embedding model — while exomem ran under
`EXOMEM_DISABLE_EMBEDDINGS=1`.

The numbers from that run, recorded for the record and **not as a comparison**:

| dimension | exomem (no semantic lane) | basic-memory (with embeddings) |
|---|---|---|
| factual_qa | 99 / 81 | 122 / 58 |
| temporal | 92 / 97 (u=19) | 130 / 71 (u=7) |
| abstention | 136 / 100 | 156 / 80 |
| provenance | 47 / 149 | 24 / 172 |
| contradiction_uncertainty | 0 / 20 | 0 / 20 |
| governance (default-open) | 0 / 16 | 4 / 12 |

Quoting any row of that table as a product comparison would be wrong in either
direction.

**This is the mirror image of the renderer defect found hours earlier.** That one
rigged the harness in our favour by handing contenders an empty corpus; this one
rigs it against us by degrading only our own retrieval. Same underlying disease —
the harness measuring its own configuration rather than the products — and the
fact that the two errors point in opposite directions is the clearest possible
evidence that neither was motivated reasoning. Both were simply unexamined.

**Disposition.** A profile must express a **capability tier that each adapter
implements for itself** — "semantic lane off" as a declared intent — with adapters
reporting whether they can honour it. A run that cannot apply the tier to a
contender must say so and refuse the comparison, rather than silently comparing
unequal configurations. Until that exists, only same-tier runs are comparable, and
the only tier both products can currently reach together is *with* their semantic
lanes on.

Tracked as 4b.29. An exomem embeddings-profile run is underway to produce the
first genuinely like-for-like pair.

## First like-for-like comparison (2026-08-05)

Both contenders with their semantic lanes active — exomem under
`recommended-embeddings` (sentence-transformers 5.6.1, model loaded and verified),
basic-memory with its ONNX `bge-small-en-v1.5`. Same seed-1 corpus, 236 queries,
both `invalid=False`, 0 harness failures.

| dimension | exomem | basic-memory | reading |
|---|---:|---:|---|
| factual_qa | **148** / 32 | 122 / 58 | real signal |
| abstention | **180** / 56 | 156 / 80 | real signal |
| temporal | **131** / 57 (u=20) | 130 / 71 (u=7) | effectively tied |
| contradiction_uncertainty | 0 / 20 | 0 / 20 | shared capability gap |
| provenance | 0 / 208 | 24 / 172 | **NOT COMPARABLE — see below** |
| governance (default-open) | 0 / 16 | 4 / 12 | excluded by design |

### Provenance is measuring our answerer, and it penalises good retrieval

exomem scored **0/208**. That is not a product result.

`scoring/extractive.py` is the *shared* deterministic answerer both contenders
are scored through, and it cites the sentinels of its top-3 hits
**unconditionally** (`_TOP_HITS = 3`). Measured on these two runs:

| | mean citations/answer | queries with hits |
|---|---:|---:|
| exomem | 2.98 | **236 / 236** |
| basic-memory | 2.14 | 188 / 236 |

exomem retrieved something for *every* query; basic-memory for 188. The answerer
therefore emitted 704 citations for exomem against 290 in its own lexical run,
and with the citation-precision gate (4b.8) admitting a permitted set averaging
~1.6–2.7 sources, every answer carried an unpermitted citation.

**So retrieving better makes a contender score worse on provenance.** The
dimension currently rewards retrieving less. This was predicted verbatim when the
precision gate was built — "the deterministic extractive baseline is a shotgunner
by construction… its citation verdicts will move from near-uniform PASS to FAIL
whenever an unrelated source lands in its top 3" — and it has now arrived.

Neither the 0 nor the 24 is a statement about either product's provenance
behaviour. Both are statements about `extractive.py`'s citation policy under a
precision gate. Tracked as 4b.31.

### What can honestly be said

- **factual_qa and abstention are real and favour exomem** (148 vs 122; 180 vs
  156) under matched capability.
- **temporal is a tie** (131 vs 130) — and exomem carries 20 UNSUPPORTED against
  basic-memory's 7, i.e. more rows where our own gate cannot decide.
- **`contradiction_uncertainty` is 0/20 for both.** Neither system does disputed
  state. It is the corpus's hardest family and currently discriminates nothing,
  because nobody passes it.
- **Provenance and governance say nothing yet** — one measures the harness, the
  other is excluded under default-open.

Two dimensions of genuine signal, one tie, one shared zero, and two that are not
yet measuring the products. That is the honest state of the first real
head-to-head, and it is a long way from a publishable comparison.

## Reference contenders: the suite finally has a scale (2026-08-07)

Two adapters were added that are not products — they are instruments. Together
they bound the axis every published figure is read on, and their first run
changed what several existing figures mean.

- **`oracle-retrieval` (ceiling)** returns exactly the sources the oracle admits
  for each query (`required_citations` closed over the evidence neighbourhood),
  ranked required-first, through the *same* shared extractive answerer as every
  contender. It never reads `ExpectedRecord.answer` — that line is what keeps it
  a retrieval ceiling rather than an oracle answerer that would score 100% and
  measure nothing, and it is enforced behaviourally: mutate every expected value
  in the corpus and the hits must be byte-identical.
- **`null-abstain` (floor)** ingests everything and retrieves nothing, so the
  answerer abstains on every query.

Seed-1, 236 queries, both `invalid=False`, 0 failures:

| dimension | floor | ceiling | usable range | exomem | basic-memory |
|---|---:|---:|---:|---|---|
| factual_qa | 0 | **172** | 172 | 148 (86%) | 122 (71%) |
| abstention | **52** | **208** | 156 | 180 (82%) | 156 (67%) |
| temporal | 28 | 152 | 124 | 131 (83%) | 130 (82%) |
| provenance | 0 | **198** | 198 | 0 (at/below floor) | 24 (12%) |
| contradiction_uncertainty | 0 | **0** | **VOID** | not measurable | not measurable |

### What this confirms, and what it overturns

**The abstention headline survives — this was the one at risk.** A gate that
rewards declining to answer is trivially gamed by declining to answer, and
nothing in the suite had established what pure abstention earns. It earns
52/236. exomem's 180 sits at 82% of the floor-to-ceiling range against
basic-memory's 67%, so the gap is real and now has a floor under it. Had the
floor come back near 180 the headline would have needed withdrawing.

**Provenance is satisfiable: 198/204.** 4b.31 argued from mechanism that
exomem's 0/208 measured the answerer rather than the product. This measures it:
a perfect retriever scores 198 through the same answerer, so the 0 is not a
product statement and never was.

**`contradiction_uncertainty` is not a shared capability gap — it is an
unpassable gate (new task 4b.33).** This document previously recorded the row as
"Neither system does disputed state… it discriminates nothing, because nobody
passes it." The ceiling shows nobody *can*: `gate_calibration` requires hedged
language in the answer text, and the extractive answerer only quotes stored
source text verbatim, with no generative step that could hedge. Floor and
ceiling are both 0. That reading was wrong in the direction that matters — it
attributed a harness defect to both products — and it is corrected here rather
than silently amended above.

**factual_qa's denominator is 172, not 180.** Eight queries cannot be passed by
any retriever, so exomem is at 86% of achievable rather than 82% of nominal.
Those eight are unexamined and are the next thing the ceiling should be pointed
at.

**Temporal's ceiling carries 24 UNSUPPORTED.** Even perfect retrieval leaves 24
rows the deterministic gate cannot decide — more than exomem's own 20, because
the ceiling retrieves more of the evidence neighbourhood and therefore trips the
4b.22 co-presence rule more often. Retrieving better produces more undecidable
rows, which is worth stating plainly: it is the same shape as the provenance
defect, one dimension over.

### Why this is worth more than the defects it found

Every harness defect this project has found — 4b.21's unwinnable queries,
4b.22's co-presence failures, 4b.31's shotgun provenance, and now 4b.33 — was
found by hand, one incident at a time, usually after a run had already produced
numbers someone nearly believed. A ceiling run surfaces the whole class in one
pass and keeps doing it as the suite grows: **any dimension whose ceiling is
below its query count has a harness defect, by construction.** That is a
standing invariant rather than a review habit.

### The declared-null seam, and why it is narrow

Zero hits everywhere is byte-indistinguishable from the broken harness that
published 236 plausible zeros (4b.24), so exempting the floor from the retrieval
guard is dangerous and is deliberately hard to reach: the exemption is a class
attribute (`retrieves_nothing_by_design`) only a purpose-built reference adapter
can set — never a flag or environment variable a real run could pass — it is
recorded as its own manifest status (`declared_null`) so a declared zero can
never be read as an observed one, and it is held in both directions, because an
adapter that declares null retrieval and then returns hits is
`declaration_broken` and INVALID. A declaration nobody checks is just a switch
for turning the guard off.

### Also found while building this

**Entity names collide suite-wide (4b.32).** Seed-1 draws 108 entities under 89
distinct canonical names: 18 names are shared by 37 entities, and 74 of 240
queries name a colliding one. Three `t07_authority_conflict` prompts are
byte-identical across two scenario instances whose expected values are mutually
exclusive (173 vs 149) — the corpus asks one question and grades two different
answers. It is the 4b.2 class, fixed for t20 only.

It is currently *masked*, not harmless: colliding queries score the same as
clean ones (factual_qa 81% vs 83%, temporal 68% vs 70%, abstention 78% vs 75%)
and 4 of the 6 colliding queries pass, because the answerer dumps whole
documents and `gate_value` matches by substring (4b.14), so both values reach
the text and each query finds its own. Two known weaknesses are cancelling a
third. That cancellation fails in exactly the direction the suite is heading:
any narrowing of the answerer turns these into real failures.

**Corpus generation could not run on a clean checkout.** `artifacts/image.py`
imported Pillow with no fallback while `artifacts/pdf.py` had always degraded
honestly, so `generate_corpus()` raised `ModuleNotFoundError` without the media
extra — which falsified task 4.1's promise ("one-command regeneration proven on
a clean checkout") before it was written. PNG now degrades like PDF.

**The first candidate fix for 4b.31 was measured and rejected.** An uncommitted
max-query-term-coverage `select_hits` (preserved at
`.task/4b31-max-coverage-select-hits.patch`) breaks 12 judge-wiring tests and,
on the t00 fixture, converts the four honestly-undecidable `current_state` rows
into four false FAILs — `required CLM-5EFCEBA2 value '2025-03-28' absent`, one
citation each, the superseded source every time. The query asks for the
*current* deadline; the stale document repeats more query nouns than the terse
revision memo, so a lexical argmax prefers it. The generalisable lesson: the old
`_TOP_HITS = 3` was recovering the current value at rank 2–3 purely by casting a
wider net, so **narrowing for citation precision necessarily costs answer
recall** — one shared extractive answerer cannot serve both. That is a
structural argument for scoring provenance against each contender's own
citations, which is 4b.31's other option.

## Full-strength A/B, and the first cross-environment reproduction (2026-08-07/08)

Two questions were open: does the native-answer seam change any number, and can
this benchmark reproduce itself at all. Both are now answered, and the second
answer is the more valuable one.

### The environment was rebuilt underneath us — and nothing moved

The venv that produced the 2026-08-05 headline no longer existed. It had been
rebuilt on a different interpreter with different dependencies, and with the
`embeddings` extra absent entirely, so the semantic lane could not load and the
4b.30 guard refused to score a lexical run wearing an embeddings label. (Model
downloads were separately blocked: `~/.cache/huggingface` is owned by
`root:root` on this box, so `HF_HOME` now points at a user-owned cache.)

| | 2026-08-05 | 2026-08-07 |
|---|---|---|
| Python | 3.14.6 | 3.12 |
| torch | 2.13.0 | 2.12.0+cu132 |
| sentence-transformers | 5.6.1 | 5.5.1 |
| transformers | 5.14.1 | 5.9.0 |

Re-running the *same* configuration (embeddings profile, harness answers) in
that materially different environment:

| dimension | Aug-5 | Aug-7 | |
|---|---|---|---|
| factual_qa | 148 / 32 | 148 / 32 | **identical** |
| abstention | 180 / 56 | 180 / 56 | **identical** |
| temporal | 131 / 57 (u=20) | 131 / 57 (u=20) | **identical** |
| contradiction_uncertainty | 0 / 20 | 0 / 20 | **identical** |
| governance | 0 / 16 | 0 / 16 | **identical** |
| provenance | 0 / 208 | 0 / 208 (u=28) | changed — see below |

Mean citations per answer reproduced exactly too: 2.98 both times.

The single delta is provenance's `not_applicable` → `unsupported` split, which
is the deliberate `gate_citations` change made in this session (an attribution
the oracle cannot check is unmeasurable, not inapplicable) and not an
environment effect.

**This is the first cross-environment reproduction the project has, and it is
stronger evidence than the same-machine reproducibility recorded on 2026-08-05.**
Those numbers survived a Python minor-version change, a torch change and a
sentence-transformers change. It does not discharge 4.1 — cross-*machine* is
still untested, and the environment gate still correctly refuses to call these
two runs verified against each other — but the deterministic design is holding
where it was most likely to break.

### Native answers: +8 factual_qa, and nothing else

Same environment, same corpus, same profile, only answer mode differing:

| dimension | harness | native | floor | ceiling |
|---|---|---|---|---|
| factual_qa | 148 (86%) | **156 (91%)** | 0 | 172 |
| temporal | 131 (83%) | 133 (85%) | 28 | 152 |
| abstention | 180 (82%) | 180 (82%) | 52 | 208 |
| provenance | 0 | 0 | 0 | 198 |
| contradiction_uncertainty | 0 | 0 | 0 | **0 (VOID)** |

Letting exomem answer from its own context pack finds the required value on
**eight more queries** than the harness's top-3 cut — 86% → 91% of what a
perfect retriever can reach. That is a real, attributable gain, and it is
attributable *only* because answer mode was made a run-level variable first.
The first full-strength run changed environment and answer mode together and
could credit neither; that is now fixed (`--answer-mode`), and the lesson is
general: **a benchmark cannot attribute an effect it cannot toggle.**

### Three things native answers did NOT fix

- **Provenance stayed at 0/208**, and the reason inverts the original
  hypothesis. The context pack *widens* the citation claim rather than
  narrowing it: 4.97 citations per answer against the harness's 2.98. A pack is
  a reasoning context and is inclusive by design, so it was never an attribution
  claim. Citing "what the pack selected" could not have worked, and the fix has
  to be exomem's real attribution surface — `derived_from`, `evidenced_by`,
  `provenance_report`.
- **Abstention stayed at 180/56, with 0 abstentions in 236 queries.** This is
  now a product finding rather than a harness artifact: at full strength,
  answering for itself, exomem declines on nothing while 52 queries require it.
  Exomem does not know when to say "I don't know."
- **The contradiction detector surfaced nothing** — 0 contradictions across 236
  packed answers with embeddings enabled, including all 20
  `contradiction_uncertainty` queries. The earlier explanation (a lexical
  profile disabling an embedding-based detector) is spent. The dimension is VOID
  from both ends: floor 0, ceiling 0, and the product's own detector silent.
