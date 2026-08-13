# Competitive benchmark programme — full continuation handoff (2026-08-09)

Audience: any successor orchestrator session (GPT-5.6 Sol via Codex CLI, or
Claude). Self-contained: state, sources of truth, operating rules, work
queue, routing. Read this before touching anything. Trust the ledger over
any summary, including this one.

## Mission (settled — do not re-litigate)

Build the trustworthy 4-lane capability map for Exomem vs Supermemory vs
Basic Memory (protocol validity → standard memory → native lifecycle →
epistemic/vault integrity → operational quality) plus the strategy report
with pre-registered continue/narrow/stop gates. Fairness architecture:
**inverted configuration authorship** — competitor rows come from
competitor-authored harnesses/configs; Exomem enters their harnesses as a
guest; residual glue is provenance-cited, disclosed, and externally
reviewed. A result showing a competitor ahead is valid output. Full
rationale: `docs/adr/0002-competitive-eval-architecture.md`.

## State snapshot (verify with `git log` before relying on it)

Branch `feat/competitive-benchmark-programme` (worktree
`<projects-dir>/exomem-competitive-eval`), pushed to origin. All merges
below passed: implementer → fresh independent review → correction round(s)
→ scoped recheck → orchestrator acceptance, with red-first evidence in each
lane's git-excluded `.task/RESULT.md`.

- `cb87dc7` W0 governance: audit
  (`docs/benchmarks/competitive-eval-audit-2026-08.md`), ADR, OpenSpec
  change `add-competitive-benchmark-programme` (validates `--strict`),
  `docs/benchmark-fairness-contract.md`,
  `benchmarks/epistemic/PREREGISTRATION.md` (+ §7 amendment).
- `09e3fb4` W1 protocol substrate (`benchmarks/protocol/`): gold quarantine
  (adapters take ProtocolEvent streams, structurally cannot receive gold),
  three-way leakage scanner (dataset content / authored literals /
  mechanical interpolations), HMAC canaries, fail-closed readiness (closed
  method allowlist), reservation budget ledger + STOP sentinel, typed
  manifests/traces, drift-gated schemas.
- `bc51890` W2+W3 direct lane + equivalence: provider registry
  (exomem-source-only, hybrid-rag control on rank-bm25, no-memory),
  runner protocol wiring (manifest-before-provider, per-session traces,
  canary as harness-authored filler event, three known-answer probes, real
  ledger-derived budget summaries), honest reporting (single banner source,
  three agreeing renderers, INVALID renders INVALID), `protocol.cli
  validate --strict` refuses manifests whose invariant fields contradict
  their status, equivalence differ (12 keys, per-key normalizers, tier
  table, wired exceptions register, 12-key perturbed twin + null case).
- `aff5a12` W6a epistemic engine (`benchmarks/epistemic/`): 18
  pre-registered deterministic assertions hardened under executed
  adversarial review (lineage-scoped retention, primary-field capability
  gating, unobservable-subject guards), neutral snapshots with
  evidence-cited FieldDeclarations, catastrophic suppression,
  N/A-poisons-family, exomem vault projector.
- Integration suite on the merged branch: **365 passed** (epistemic +
  protocol + lme + equivalence + report + privacy) + full membench suite
  462 passed; schema drift green.
- **W4 accepted outside this repo**: Exomem guest provider in Basic
  Memory's bm-bench — fork `Artexis10/basic-memory`, branch
  `exomem-provider-v2` (pushed), +961 lines, 22 tests, live smoke green
  (product defaults, fail-loud on degraded/warming envelopes, sentinel
  removed from measured corpus, exact top-k pass-through).
- Committed evidence: `benchmarks/memorybench/audit/{supermemory-provider-audit,harness-audit}.md`
  (headline: their provider hardcodes limit:30 + include.chunks — commit
  dad6d5d, vendor-authored; questionDate never plumbed; `_abs` abstention
  unroutable; failed questions excluded from accuracy),
  `docs/strategy/evidence/market-map-2026-08.md` (OKF commoditization; the
  open ground is the full epistemic contract; Supermemory's "~85.9%" is
  aggregator-derived — primary source claims 95% Recall@15, different
  metric), suite pins in `benchmarks/{memorybench,bmbench,suites/*}/LOCKFILE.json`.

## Checkout map

| Path (sibling of the primary) | Role | State |
|---|---|---|
| `exomem-competitive-eval` | programme worktree, branch `feat/competitive-benchmark-programme` | clean, pushed; venv has the embeddings extra |
| `exomem-cb-protocol`, `exomem-cb-direct`, `exomem-cb-epistemic` | merged lane worktrees | keep for `.task/` evidence until the PR lands, then remove |
| `basic-memory-fork` | writable fork, branch `exomem-provider-v2` | pushed; upstream PR wedge NOT yet opened |
| `memorybench` | pinned clone `118209a7…` | read-only; providers get copied in at setup |
| `basic-memory`, `basic-memory-exomem-provider`, `supermemory` | READ-ONLY reference checkouts | never modify |
| `suite-stale`, `suite-memops`, `suite-memoryagentbench`, `suite-oida-corpora` | pinned suite clones | read-only; pins in LOCKFILEs |

Runtimes: bun 1.3.14 (npm-global), node v24, ollama present but NO models
pulled (needed for meter-proxy/native rows). ~4–5 GB RAM free: run
providers strictly sequentially; benchmarks only on a quiesced machine;
`CUDA_VISIBLE_DEVICES=""`; **no latency claims from this host, ever**.

## Operating rules (non-negotiable, inherited)

1. **Fairness contract** (`docs/benchmark-fairness-contract.md`): competitor
   config is competitor-authored or it does not run; glue disclosed with
   size accounting; harness faults never contender losses; variants never
   collapse; historical-untrusted refused; comparative publication gates on
   independent adversarial review.
2. **Delegation discipline**: every nontrivial lane gets a FRESH independent
   reviewer over the ACTUAL diff (never the implementer, never the
   orchestrator's own read), then a scoped recheck of corrections, then
   acceptance quoting verdict + diff stat. This caught, live: fabricated
   manifest summaries, false-green validators, a vacuous catastrophic
   assertion, inverted canary logic. Same-family review is weaker — route
   adversarial reviews cross-family (a Claude session) where possible.
3. **No fabricated data.** A summary field reflects a real measurement or
   the run refuses. Validators recompute; a status field is a claim, not
   evidence.
4. **Red-first.** Failing output before implementation, verbatim, in
   `.task/RESULT.md`.
5. **Leakage fixtures need numeric and date-shaped golds** — word golds are
   blind to interpolation collisions.
6. Privacy gate scans every tracked file: no absolute local paths, no
   personal names/tokens. Competitor names never under `src/exomem/**`.
   Guarded paths untouched: `tests/golden/`, `tests/test_latency_gate.py`,
   `tests/test_retrieval_golden.py`, `.github/` (except the planned
   additive `benchmark-protocol` CI job).
7. Exomem `EXOMEM_DISABLE_*` flags are string-truthy (`"0"` DISABLES).
   `benchmarks/run.py` inserts sys.path — bypass for A/B runs. Lean tests
   offline, <60s, no credentials.
8. **Spend**: implementer/reviewer lanes are subscription-billed. Benchmark
   runtime API calls are founder-gated, ≤$25 for this window, enforced by
   the reservation ledger (`--budget-cap-usd` / `PROTOCOL_BUDGET_CAP_USD`);
   no key is currently exported — the founder supplies one. The official
   LongMemEval judge is OpenAI; claude-CLI plan-billed runners never
   substitute for it.

## Founder gates (⛳ — blocking, in order)

1. **Pre-registration ratification** (ledger 0.7): founder ratifies
   `benchmarks/epistemic/PREREGISTRATION.md` (gates G1–G5, §7 amendment);
   its sha256 at that commit goes into every run manifest. **No competitor
   run before this freezes.**
2. **Metered key + 25-case tier approval** (ledger 7.4): OPENAI_API_KEY for
   reader gpt-4o + official judge.
3. **Full-run escalation** (beyond the 25-case tier) — separate approval
   with pilot evidence.
4. **Public upstream filings** — founder decides before anything outward:
   the MemoryBench issue list (end of `harness-audit.md`), the bm-bench PR
   wedge (PR-1 fairness hardening with zero exomem content FIRST, then
   PR-2 cleaned-variant pin, then PR-3 the provider), the bm-bench
   supermemory-provider issue (documents-vs-memories row).
5. Housekeeping (approved-only deletions): stray `test/` scaffold at the
   primary root; stale merged worktrees (`exomem-lme-adapter`,
   `exomem-bench-{cleanup,prefrel,xlingual}`, `exomem-a4-startup-benchmark`,
   `exomem-perf-baseline`).

## Work queue (the ledger is the source of truth:
`openspec/changes/add-competitive-benchmark-programme/tasks.md`)

- **§4 W5 — MemoryBench guest lane** (4.4 transport contract now binding in
  OpenSpec design decision 11): retain paired observations, not a single
  synthetic product score. Basic Memory keeps its `bm-bench` own-harness row
  and gains a MemoryBench row through the pinned, unmodified
  `BasicMemoryLocalProvider`; Supermemory keeps its own MemoryBench row and
  receives the 4.7 direct-SDK spot-check. The Basic TS provider reconnects to
  one persistent, loopback-only Python sidecar across separate stage
  processes; that sidecar exposes exactly ingest/search/cleanup, blocks
  ingest through fresh per-session vector/fallback proof plus startup/config
  evidence from the isolated Basic log, forwards the
  exact search limit, and invalidates fallback or ambiguous output. Raw
  `_abs`/question identity is digested before the competitor renderer. An
  inert benchmark-owned Basic default project prevents accidental indexing
  of `~/basic-memory`; all project paths and final absence are proven. The
  Exomem TS provider owns one initialized vault and authenticated ephemeral
  REST service per container and proves hybrid readiness with doctor. Both
  transports use bounded retries/deadlines, sequential concurrency, secure
  atomically published descriptors, owned-process-group teardown, and
  token-free evidence. Registration is additive plus exactly three pinned
  MemoryBench edits, with pre/postimages and a regenerated canonical diff;
  materialize/verify/restore refuse all drift. 4.4 is hermetic and performs
  no provider run. Then 4.5 imports ingest/search ONLY with explicit
  `missing_fields` and owns `finally`/signal cleanup because pinned
  MemoryBench never invokes `Provider.clear()`; 4.6 runs the **25-case
  Exomem direct-vs-MemoryBench
  equivalence gate in blocking mode**, and 4.7 performs the Supermemory
  SDK/provider report-mode spot-check.
- **§5 W6b — epistemic completion**: runner glue binding scenarios to
  AssertionContext (`served_items`, `foreign_case_hits`,
  `external_edit_at`, snapshot pairs → `prior`) — ledger 5.1b; exomem
  decision-entity projection (5.1c); scenario fixtures for f01–f14 per
  PREREGISTRATION §1 with fairness packets; grep-markdown + no-memory
  controls wired into every table (5.1d); 4b.18 structural-blinding fix +
  structure-swap test (5.3 — hard-gates ALL judge use); **independent
  adversarial review of every fairness packet BEFORE competitor drivers
  run** (5.4 ⛳); operational families 10–14 (5.5); judge–human κ via
  membench `agreement.py` before any judged number (5.6).
- **§6 W7/W8 — native + ops lanes**: pull an ollama model first (meter
  proxy prerequisite); designs in the plan and
  `docs/benchmarks/competitive-eval-audit-2026-08.md` §6/§7.
- **§7 W9 — runs**: fixture tier everywhere → LongMemEval-S fetch via
  `benchmarks/lme/fetch.py` (pin sha; generate + commit the real
  `lme-s-25.json` via the selection CLI) → supermemory local binary
  (isolated data dir, pin per bm-bench's documented version) → ⛳ metered
  25-case tier → official judging via the pinned V1 checkout (`suites/
  lme_v1` LOCKFILE still to write; verify `judge_io`'s UNVERIFIED
  evaluate_qa.py flags at that pin) → gap reports for everything not run.
- **§8 W10 — suites**: LMEv2 fixture-first through THEIR harness
  (xiaowu0162/LongMemEval-V2; ≤25-case small tier only if budget);
  STALE/MemOps integration per their LOCKFILE caveats.
- **§9 W11 — reports + CI**: fairness/compat matrices, adversarial packet
  generator (assumptions from FieldDeclarations, confounds, suspicious-win
  flags, pre-registration hash), consolidate with import-closure test;
  additive `benchmark-protocol` CI job (offline only).
- **§10 W12 — strategy + external review**: evidence slots →
  `docs/strategy/exomem-competitive-strategy-2026-08.md`; gates evaluated
  against the RATIFIED thresholds; **external adversarial review of the
  packet (cross-family)**; final owner report answering the seven mission
  questions (trustworthy? clean results? below parity where? neutral
  advantage where? thesis survived? highest-leverage changes?
  continue/narrow/stop?).
- Delivery: staged conventional-commit PRs to `main`; the foundation PR is
  ready to open from this branch. No AI attribution in commits/PRs.

## Known open notes

- Two exomem product bugs found in passing (KB-filed, unfixed): `remember`
  false COMPILED_DESTINATION_MISMATCH on long titles/slugs; `severity`
  enum undocumented in the tool schema.
- The membench `test_wire_mode_smoke_search` timeout occurs only in
  sandboxed workers (no network/subprocess); it passes in full envs.
- bm-bench fork rework left untracked `.omc/` dirs in the fork — do not
  let them near the upstream PR.
- KB notes for this programme (project `exomem`, Insights/Failures):
  `competitive-benchmark-guest-lane-supersession`,
  `first-guest-lane-seat-accepted-bm-bench`,
  `leakage-scanners-partition-harness-vs-dataset-text`,
  `status-only-validators-false-green`,
  `file-canonical-memory-commoditized-epistemic-contract-open`.
