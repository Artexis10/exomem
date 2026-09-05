# Tasks — add-competitive-benchmark-programme

Ledger discipline: check a box only with evidence (test output, artifact
path, or commit). Founder checkpoints are marked ⛳ and block what follows
them. Mission acceptance criteria (§14) close only from this ledger.

## 0. Governance & truth (W0)

- [x] 0.1 Phase-0 audit published — `docs/benchmarks/competitive-eval-audit-2026-08.md`
      (asset map; reusable/suspect/obsolete; validity-failure catalogue incl.
      new findings N1 leak / N2 dataset divergence / N3 supermemory-documents
      row; data-flow diagram; historical-untrusted migration; fairness
      exposure table)
- [x] 0.2 ADR accepted — `docs/adr/0002-competitive-eval-architecture.md`
- [x] 0.3 This change's proposal/design/spec deltas written (MODIFIED
      `public-suite-eval` + five ADDED capabilities)
- [x] 0.4 `openspec validate add-competitive-benchmark-programme --strict` green
- [x] 0.5 Fairness contract published — `docs/benchmark-fairness-contract.md`
- [x] 0.6 Pre-registration artifact committed —
      `benchmarks/epistemic/PREREGISTRATION.md` (14 families, assertion
      registry, acceptance predicates, decision gates G1–G5 with proposed
      thresholds) + sha256 recorded there
- [x] 0.7 ⛳ Founder ratified the unchanged pre-registration on 2026-08-11:
      `benchmarks/epistemic/PREREGISTRATION.md` sha256
      `21aa5a8815038b82358336798b10afd8d3ffbd9739c8da597955bd14d8d962e3`.
      Immutable receipt `benchmarks/epistemic/contracts/ratification.v1.json`
      sha256 `31b74c6cdd69504da31af903e8464177f35fbf525f655c49e0e92e1f9862e5c6`
      binds the founder decision and repository revision. Later changes require
      an ordered amendment receipt; no competitor run occurred during
      ratification or implementation.
- [ ] 0.8 KB updated: 2026-08-08 REJECT audit + this programme's supersession
      filed as a research note (the KB thread currently ends at the
      2026-08-06 direction-lock)
- [ ] 0.9 Housekeeping surfaced to founder (stray root `test/` scaffold;
      stale merged bench worktrees) — propose, never auto-delete

## 1. Protocol substrate (W1 — lane: codex)

- [x] §1 COMPLETE (merge 09e3fb4; lane codex/cb-protocol; review
      APPROVE-WITH-FIXES → correction → recheck NOT-CLEAR (new blocker:
      gold-vs-interpolation collisions) → final targeted correction →
      orchestrator-verified green: 68 protocol/lme/privacy tests, drift +
      fixture selftest, full membench 462 passed). 1.9 snapshot/projectors
      deliberately deferred to the W6 lane. Sub-items:
- [x] 1.1 Red leak test observed FAILING against current code
      (`benchmarks/lme/fixtures/leaky.json` +
      `tests/test_lme_normalize.py::test_ingestion_payloads_carry_no_gold_labels`)
- [ ] 1.2 `benchmarks/protocol/models.py` + committed drift-gated schemas
      (event, case-gold, run-manifest, case-trace, readiness-report,
      probe-result, budget-ledger, equivalence-diff/-exception, gap-report)
- [ ] 1.3 `events.py` normalizer (NFC, content_sha256, ordinal identity,
      hashed upstream ids, timestamp_semantics) — turns 1.1 green with
      `lme/normalize.py`
- [ ] 1.4 `leakage.py` three-scope transport-level scanner (red/green fixtures)
- [ ] 1.5 `namespace.py` + `canary.py` (presence / cross-case / never-ingested)
- [ ] 1.6 `readiness.py` + `probes.py` (lexical / zero-overlap semantic /
      update-current-state; `inconclusive-by-design` outcome)
- [ ] 1.7 `budget.py` + `pricing.yaml` (reservation ledger, STOP sentinel,
      unknown-model refusal, approval records; property tests)
- [ ] 1.8 `manifest.py` + `trace.py` + `validity.py` (manifest before first
      provider call; report refuses non-terminal)
- [ ] 1.9 `snapshot.py` + projectors (exomem_vault, basic_memory_vault,
      supermemory_api via recorded fixtures/MockTransport) with evidence-cited
      FieldDeclarations
- [ ] 1.10 Privacy tests extended over all new trees (before content lands)

## 2. Direct lane + equivalence (W2+W3 — lane: codex)

- [x] §2 CORE COMPLETE (merge bc51890; lane codex/cb-direct; review REJECT →
      Terra retry (production fixed, tests absent) → escalation to Claude
      executor (+62 tests) → recheck NOT-CLEAR (2 residuals: verdict-before-
      write ordering incl. pilot-evidence.json; rework-introduced
      constructible-zero on the legacy path) → surgical fixes → 139-test
      acceptance group green; 365-test integration suite green post-merge.
      Landed: providers package with runtime conformance tests; hybrid-RAG
      control (rank-bm25, ingest-time caching, sentence packing on the
      indexed tokenization, fixture-embedder variant honestly named); runner
      protocol wiring (manifest-before-provider, typed per-session traces,
      canaries as harness-authored filler events scanned by scan_ingest,
      three known-answer probes, real budget ledger with CLI/env cap);
      report honesty (one banner source, three agreeing renderers,
      INVALID renders INVALID); validate --strict refuses false-green
      manifests; equivalence differ (12 keys, per-key normalizers, tier
      table, exceptions register wired, expired→unexplained) + selection
      machinery + 12-key perturbed twin + null case; contamination
      semantics: provider-path unverifiable invalidates, legacy-path
      records honest unverifiable and is refused for comparative use.
- [x] 2.3a-residual `basic_memory_direct.py` via the §4.4 sidecar. Unblocked
      once 4.4 landed the sidecar. Because decision 1 requires the competitor's
      provider class to run under its own uv environment, this row cannot be
      in-process, and decision 14's single
      `in-process-no-post-return-background` model could not carry it;
      declaring it anyway would have been exactly the unevidenced status field
      this programme refuses. Design decision 15 + the benchmark-protocol
      requirement therefore add `owned-subprocess-terminated-at-cleanup` as a
      second admitted model, owing an additive `process-group` cleanup surface
      (canonical group ref, live count, listener bound — never raw PIDs, ports,
      or tokens) whose absence is probed rather than asserted. Landed: the
      provider (registry row, closed-envelope transport, exact-top_k
      pass-through, owned process-group teardown), the additive schema, and 12
      hermetic contract tests that need no Basic Memory checkout. Test-validity
      evidence: three production mutations (teardown never signals; top_k
      widened by 7; `_absence` process-group branch removed) each reproduced
      red, and the third exposed a genuinely weak test — `provider-state` was
      carrying process liveness, so the surviving-sidecar case passed for the
      wrong reason. `backend_active` now describes only what the provider
      holds, so a row that zeroes its own bookkeeping cannot hide a leaked
      process. NOT run against the real pinned Basic Memory environment; that
      belongs with the §7 runs.
- [ ] 2.3b-residual `supermemory_direct.py` (still deferred by scope note:
      needs the 7.3 pinned local binary, which is not installed)
- [x] 2.5-residual the REAL committed `lme-s-25.json` generates from the
      pinned dataset at fetch time (W9); the selection algorithm + fixture
      subsets are landed and tested (artifact
      `benchmarks/equivalence/subsets/lme-s-25.json`, SHA-256
      `7c46b689758901f73fe365d861d0998ecc64ec0435392df745d63d7da0ccc901`)

## 3. Guest lane: bm-bench (W4 — lane: codex + claude review)

- [ ] 3.1 Fork of upstream Basic Memory + branch `exomem-provider-v2`;
      provider single-sourced from `benchmarks/bmbench/provider/` with hash
      sync
- [ ] 3.2 Fix list red-first (no de-tuning; `exomem_flag_enabled()`;
      CLIP loaded; positive semantic verification; error-string→raise;
      config isolation; `BM_BENCH_PROTOCOL_TRACE_DIR`)
- [ ] 3.3 `sidecar.py` (their env, 3 endpoints) + `export.py` → protocol
      artifacts
- [ ] 3.4 `audit/bm-bench-audit.md` (their harness, adversarially)
- [ ] 3.5 Upstream PR-1 (fairness hardening, zero exomem content) + PR-2
      (cleaned-variant pin) opened; supermemory-provider defects filed as an
      upstream ISSUE with evidence
- [ ] 3.6 Upstream PR-3 (exomem provider) after 3.5 lands or stalls
      (fork-pinned fallback recorded)

## 4. Guest lane: MemoryBench (W5 — lane: claude executor)

- [x] 4.1 Clone + `LOCKFILE.json` (commit/tree sha, bun pin, license hash);
      `setup --verify` refuses drift (23 focused offline checks; read-only
      index proof; attached real checkout refused as required)
- [x] 4.2 `audit/supermemory-provider-audit.md` (incl. ProviderPrompts diff)
      + `audit/harness-audit.md` — committed. Headline: their provider
      hardcodes limit:30 vs shared limit:10 + include.chunks (commit
      dad6d5d, vendor-authored); questionDate never plumbed; _abs
      abstention unroutable; failed questions excluded from accuracy.
      Upstream filing = founder decision (list at harness-audit.md end)
- [x] 4.3 Recording proxy capturing their provider's actual traffic (164
      proxy checks / 187 focused offline checks; 272 cross-lane checks; exact
      pin and artifact recomputation; final independent review CLEAR)
- [x] 4.4 TS providers + exact reversible registration overlay:
      exomem via one isolated REST service per container (owned vault,
      ephemeral loopback port, random REST key, doctor readiness);
      basic-memory via one persistent shared Python sidecar exposing exactly
      `POST /v1/{ingest,search,cleanup}` and wrapping the pinned unmodified
      `BasicMemoryLocalProvider` + renderer under its uv environment.
      Ingest blocks through positive readiness; raw `_abs`/question IDs are
      neutralized before rendering; an inert benchmark-owned default project
      prevents operator-home indexing; the same warm MCP process supplies
      startup/config evidence from its isolated Basic log while every unique
      ingest gets fresh fallback and project/document count proof;
      fallback/non-JSON ambiguity invalidates.
      Search forwards the exact limit once; growing-corpus N-reindex
      asymmetry is disclosed; descriptor attach/cleanup is secure and proves
      absence. Registration changes only ProviderName, registry/export, and
      no-key config; setup materialize/verify/restore recomputes the canonical
      diff and all locked hashes. Red-first coverage includes envelopes,
      replay/collision, receipts, observation transparency, project-path
      isolation, descriptor attacks, owned-group teardown, exact acceptance
      sequence without cache writes, static provenance, and no product or
      competitor-provider modification. Acceptance: focused Python source and
      setup checks; hermetic sidecar checks under the pinned Basic benchmark
      environment; materialize → verify → pinned local TypeScript compiler and
      guest tests → restore → pristine verify; existing lean protocol/schema
      suites; fresh independent review of the actual diff. No provider,
      dataset, model, network, credential, or benchmark run in this item.
      Final evidence: 91 focused Python checks; 53 Bun checks / 260
      expectations; 260 + 137 cross-lane checks; pinned materialize → verify
      → TypeScript 5.9.3 / Basic-env checks → restore → pristine lifecycle;
      all 8 additive hashes and the canonical registration patch recomputed;
      final independent targeted recheck `CLEAR`.
- [x] 4.5 MemoryBench runner/export/cleanup glue for the two 4.4 guests
      (`basic-memory|exomem`): accept an absolute owned mode-0600 strict
      `memorybench-run-plan.v1` that binds and recomputes the local dataset
      bytes/count, existing `DatasetIdentity`, exact harness pin/lock,
      registered variant, run IDs, output, and contained guest roots before
      provider work; emit strict typed public
      `memorybench-export.v1.json` from pinned checkpoint + canonical result
      files (executed ingest/indexing/search; answer/evaluate/report excluded;
      original-order finite `{content,score}` hits; per-case phase states;
      source refs/digests; all imported timing non-publishable) and protected
      mode-0600 typed private-gold members under a mode-0700 directory. Never
      copy the raw result envelope,
      expose ground truth, invent `answer_session_ids: []`, or substitute
      constants/inference for missing evidence. `missing_fields` is a closed,
      sorted vocabulary: `question.question_date`,
      `gold.answer_session_ids`, `ingest.transmitted_payloads`,
      `search.transmitted_query`, `search.options.limit`,
      `search.options.threshold`, `search.normalized_hit_ids`,
      `search.normalized_scores`, `search.normalized_ranks`,
      `search.retry_attempts`, `search.http_status`. Add strict
      Rooted public refs use safe output paths, while MemoryBench source refs,
      case IDs, and container tags expose only domain-separated HMAC-SHA256
      pseudonyms under a random 32-byte key held solely in the mode-0600 run
      plan; private gold carries raw IDs/paths and validators recompute HMAC +
      byte digests. Plain raw-ID/path SHA-256 is forbidden as a dictionary
      oracle; Python/TypeScript must pass the frozen NUL-delimited cross-language
      HMAC vectors. Never copy/rename a source, publish the key, or leak raw identity
      through a public ref/filename. Add strict
      `guest-cleanup.v1.json` via descriptor-driven
      `benchmarks/memorybench/cleanup.ts`: absolute owned mode-0600 strict
      `guest-cleanup-plan.v1`,
      attach-only through reviewed descriptor verification, sequential calls
      to the concrete providers' existing `clear(containerTag)` paths, and no
      launch/repair/replacement during teardown. Targets are the union of
      non-pending checkpoint tags, validated guest request/response evidence,
      and secure descriptors, deduplicated and digest-sorted; attempt every
      target after individual failures. Persist token-free per-namespace plus aggregate
      descriptor/process-group/work-root absence and Basic public-cleanup call
      count. One `finally`/signal path writes partial-or-complete export before
      cleanup, cleanup proof before final manifest, and returns `VALID` only
      for complete export + `all_absent:true`; provider/export/interruption/
      cleanup/proof failures are `INVALID`, while `BLOCKED` is pre-provider
      only. Status is independent of exit: caught signal wins 130/143;
      otherwise unproved cleanup 3, pre-provider BLOCKED 2, VALID 0, remaining
      INVALID 1. Commit strict generated Draft 2020-12 schemas for the run
      plan, export, private gold, cleanup plan, and cleanup proof; schemas prove
      every standard-expressible structural invariant, while strict models +
      shared validators recompute sibling equality, lexical ordering/root
      containment, referenced digests and source facts. A schema/status pass is
      never validity evidence; separate closed named schema→model and
      model→artifact-validator registries enumerate every permitted semantic
      difference and mandatory external recomputation. No-follow source
      reads stay confined to the resolved run root; canonical result discovery
      is independent of untrusted checkpoint paths; missing/duplicate/extra/
      outside-root/non-finite or cross-source disagreement is partial INVALID,
      never precedence selection. Bind `dataset_path` to MemoryBench's exact
      native LongMemEval raw-cache path in a disposable exact-pin checkout;
      refuse any pre-existing derived question cache or run root, never
      download/delete/repair/reuse it, and reconcile fresh pinned question
      shards before validity. Bind full versus explicit ordered question IDs
      in the private plan and pass the latter through a lockfile-pinned
      additive ingest entrypoint using the upstream `questionIds` seam; never
      use `--limit` or sampling. Resolve verified Bun 1.3.14 and uv before
      provider work, invoke Bun absolutely with a minimal verified `PATH`,
      reject duplicate JSON members recursively in Python and TypeScript,
      retain cleanup targets before fallible export projection and validate
      each discovery candidate independently so malformed guest evidence or
      descriptors record stable partial-export failure without erasing valid
      siblings, run cleanup in
      an isolated process group, observe Basic's zero-or-one cleanup count at
      its real finalization seam (including honest failed proofs), and never
      infer process/config absence from a missing
      directory. Public bytes must pass the privacy scanner before persistence
      and the persisted export must pass the full artifact validator before
      terminal `VALID`. Red-first acceptance covers omission/no-fabrication, privacy,
      schema parity, corrupt/duplicate/missing source artifacts, attach-only
      teardown, target discovery, provider-clear invocation, partial export,
      cleanup failure, signals, manifest ordering, deterministic replay, and
      fresh independent review. No provider/dataset/model/network/credential/
      benchmark run in this item.
      Final evidence (2026-08-10): every implementation/correction pass retained
      verbatim red-first transcripts in `.task/RESULT-4.5.md`; root final gates
      were 329 Python passed (one existing fork deprecation warning), 80 Bun
      passed / 376 expectations, protocol schema check, fixture selftest,
      OpenSpec strict, and diff check green. The final local-only disposable
      lifecycle passed 53 materialized provider tests, 27 cleanup tests, four
      ingest-entrypoint tests, and TypeScript 5.9.3, then restored both exact
      pinned checkouts pristine. The overlay is exactly nine additive files and
      all lock hashes recompute. Final independent FEEDBACK6 recheck: `CLEAR`.
      No competitor/provider/network/model/dataset benchmark, credential,
      metered call, commit, push, or §4.6 work occurred.
- [x] 4.5a Bound Exomem guest-service residency across staged processes
      (configurable positive cap, default one); retire LRU process groups and
      descriptors before replacement; preserve restartable vault state until
      terminal per-container cleanup; clear finished searches and every
      question/indexing failure; install idempotent SIGINT/SIGTERM and uncaught
      failure cleanup that proves absence before re-raising; make absent
      `clear()` attach-only. Red-first coverage: at least five sequential tags,
      cross-stage admission, mid-run/indexing failure, both termination signals,
      in-flight retirement, and absent-clear no-spawn. Keep the existing
      descriptor/process-group/work-root proof path and full runner cleanup as
      the orphan backstop.
- [ ] 4.6a Publish the already-observed guest facts in `memorybench-export.v1`.
      Blocked discovery (2026-08-15): five of the nine BLOCKING equivalence keys
      had no source in the export — `session_normalization`,
      `ingestion_payloads`, `readiness`, `top_k`, and
      `answer_judge_prompt_model_config`. The names `search.transmitted_query`,
      `search.options.limit`, and `search.normalized_hit_ids` appear in the
      schema ONLY as labels inside the `missing_fields` enum; there is no
      search `$def`, and `readiness`/`session_normalization`/`payload_sha`
      appear nowhere. Under the differ's null-never-equals rule those five
      mismatch by construction, so the blocking gate could never go green — not
      because the paths disagree, but because one side was never asked.
      This is NOT a §4.5 defect: its export was deliberately scoped to executed
      ingest/indexing/search with a closed no-fabrication vocabulary.
      The facts are already captured and validated: the Exomem guest records
      `request`/`response` evidence for every call
      (`providers/exomem/index.ts`), builds the search body from the exact
      `{query, limit}`, refuses an over-limit response, and requires a selected
      path per hit; `export.py` already reads and validates guest evidence.
      So this is a PROJECTION extension — additive export fields sourced from
      existing evidence, no TS provider change, no `registration.patch` churn,
      no lock-hash recomputation. The no-fabrication rule holds: a field appears
      only when its evidence proves it, otherwise it stays in `missing_fields`.
      `answer_judge_prompt_model_config` is sourced from the run plan (an
      operator declaration both sides share), never from the harness, which
      excludes answer/evaluate/report by design.
      - [x] 4.6a-1 SCHEMA: additive `MemoryBenchSearchObservation`
            (`transmitted_query`, `options.limit`, `normalized_hit_ids`),
            `MemoryBenchIngestObservation` (`transmitted_payload_sha256`), and
            run-level `session_normalization` + `readiness`. The honesty
            coupling is enforced, not documented: `_OBSERVATION_LABELS` binds
            each optional block to the `missing_fields` labels it answers for,
            and a model validator refuses BOTH a published value whose label is
            still declared missing AND an absent value whose labels are not all
            declared. Hit ids may not outnumber the transmitted limit — the
            same contract the guest already enforces at `index.ts:205`.
            Schema regenerated; drift and conformance gates green. Evidence:
            958 passed / 32 skipped across membench + protocol + memorybench +
            privacy, `ruff --select F` clean, OpenSpec strict valid.
      - [x] 4.6a-2 POPULATE: `benchmarks/memorybench/guest_observations.py`
            reads one guest evidence directory in TRANSMISSION order (the
            sequence in the filename, never directory order — sequence 10 must
            not sort before 2) and publishes only what those entries prove.
            Wired into the single case-assembly site in `export.py`, which
            starts from "everything missing" and subtracts only the labels the
            evidence resolves. Scoped to the `exomem` guest; the Basic sidecar
            records a different evidence shape and keeps its labels declared.
            Absence is never a value: a request with no paired response records
            `guest_evidence_incomplete`, and a response breaking the guest's own
            limit contract (refused at `index.ts:205` before returning) records
            `guest_evidence_invalid` rather than publishing it. An empty
            directory is absence, not a fault. `normalized_scores` stays absent
            — the guest search path hard-codes `score: 0.0`.
            Evidence: 14 focused checks red-first, then 503 passed across
            memorybench + protocol with the export's recompute-and-compare
            invariant intact.
- [x] 4.6b `memorybench-export.v1` → `equivalence-input.v1` projector.
      `benchmarks/memorybench/equivalence_projection.py` mirrors the twelve
      keys `lme/runner.py::_equivalence_case` emits, sourced from the public
      export plus the private-gold mapping (the public artifact carries only
      HMAC pseudonyms; the comparison needs the real question ids). Readiness
      is narrowed to the same five fields the direct emitter compares, dropping
      `evidence` because prose would never match. A key the export could not
      source stays NULL rather than being invented — the differ treats null as
      never equal to anything, so an unsourced key becomes a difference
      demanding an explanation instead of a silent pass. A case with no private
      gold mapping is refused, never guessed. CLI writes `equivalence.json`
      into a run directory (`--export`, `--private-gold`, `--out`).
      Evidence: 13 checks, red-first. The decisive two run the REAL emitter
      rather than a hand-copy — one asserts both sides carry identical key sets
      so the differ compares like with like, and a round trip through
      `compare_runs` shows identical projections produce no blocking difference
      while a widened `top_k` (10 vs 30) is caught as blocking.
      EXPECTED at 4.6c: several BLOCKING keys will legitimately differ because
      the two paths genuinely differ, not because either is wrong —
      `session_normalization` (`lme.normalize.render_neutral_session/v1` vs
      `memorybench.longmemeval_to_corpus/v1`), `namespace` (different
      derivations), and `ingestion_payloads` (digest of a rendered neutral
      session vs of a `capture_source` body). `retrieved_ids` will differ too
      (the direct row emits positional `exomem-N` ids, the guest emits vault
      paths) but that key is REPORTED, not blocking. These are the measured
      findings 4.6c exists to surface and the exceptions register exists to
      carry, with expiry — they are not to be papered over before the gate runs.
- [ ] 4.6c 25-case Exomem direct-vs-MemoryBench equivalence gate GREEN
      (mode=blocking) — prerequisite for every comparative run
- [ ] 4.6c-CPU Enforce and record the canonical CPU profile in the direct
      adapter, guest transport and controlled export environment; prove ambient
      accelerator overrides cannot change text/CLIP device selection. Rerun
      both halves at a fresh provider pin after the repair is delivered.
- [ ] 4.6c-Lifecycle Use owned local-CLI reconciliation before guest eviction;
      verify the actual two-case staged pipeline, including cross-case
      retirement, export, projection and comparison, before the full cohort.
- [ ] 4.6c-InputParity Resolve the measured direct/guest normalization,
      ingestion-payload, namespace and readiness differences before releasing
      the full gate. The two-case real-data canary produced VALID artifacts on
      both paths but eight BLOCKING differences; artifact validity alone does
      not establish equivalence. Preserve the failing comparison and justify
      any expiring exception from evidence rather than expected behavior.
- [ ] 4.6c-ProbeParity Keep diagnostic presence-canary material out of the
      scored retrieval corpus and reader context while retaining the positive,
      cross-case and never-ingested isolation checks. The direct canary reached
      reader context in both reduced-session cases; the guest corpus contained
      only dataset sessions. Verify this boundary through both real runners
      before treating their output as a quality comparison.
- [ ] 4.7 Ratify and implement the native Supermemory vendor-hit projection
      (distinct from 4.5's flat guest-hit wire), then run the Supermemory
      direct-SDK vs MemoryBench-provider spot-check (mode=report)

## 5. Epistemic State Bench (W6 — lane: claude executor + external review)

- [x] 5.1 Engine COMPLETE (merge aff5a12; lane claude/cb-epistemic; review
      APPROVE-WITH-FIXES → correction → recheck NOT-CLEAR (R-B1b) → surgical
      fixes incl. sibling unobservable-subject guards → 227 tests green;
      §7 amendment records vacuity-fails for evidence_path_resolves)
- [x] 5.1b W6b runner glue (ledgered from review M7): scenario→AssertionContext
      binding for served_items / foreign_case_hits / external_edit_at;
      REQUIRES_SNAPSHOT_PAIR bound to AssertionContext.prior. COMPLETE:
      caller-supplied observations bind deterministically in trajectory order;
      snapshot pairs are same-row, non-aliased, deep-isolated evidence; edit
      pairs straddle a strict RFC3339 timestamp; every binding and registry
      error refuses before any assertion executes. Red-first evidence: initial
      import RED, behavioural `9 failed, 19 passed`, preflight `1 failed,
      9 passed`, reviewer correction `9 failed`, timestamp correction `1
      failed`, and RFC3339 correction `3 failed, 3 passed`. Final affected
      gates: feedback selectors `17 passed`; runner/projector `47 passed`;
      focused `161 passed`; epistemic+privacy `257 passed`; prior independent
      cross-lane gate `632 passed, 33 skipped`. Fresh independent review:
      REQUEST_CHANGES → correction → REQUEST_CHANGES → correction → CLEAR.
- [x] 5.1c exomem decision projection (review M9): entity_type: decision →
      kind "decision" in the vault projector, else f07 is unscoreable for
      the subject product. COMPLETE: normalized decision entities project as
      neutral decisions, retain their raw entity type, leave other entities as
      containers, and make f07 scoreable without folder guessing. An immutable,
      exhaustive, dereferenced competitor-evidence registry now covers every
      kind mapping and fallback. Covered by the joint 5.1b gates and final
      independent `CLEAR`; pre-registration bytes remained unchanged.
- [x] 5.1d census gaps (review) COMPLETE: comparative manifest schema v2
      carries the Git-reconstructed ratification identity and complete ordered
      amendment chain across LME, MemoryBench, and Membench; v1/unversioned
      results are historical-untrusted. Receipt validation binds the exact
      document at the run pin plus each receipt's unique full-history Git
      introduction, refusing branch, mutation/restore, delete/re-add, rename,
      substitution, and incomplete-chain attacks. Privileged endpoints use a
      closed surface × provider × variant matrix and an attested, HMAC-bound
      parent broker; untrusted driver code runs only in a fail-closed
      Bubblewrap namespace, and capability gaps become named noncomparability,
      never scores. Every product/control cohort cell has provider/variant-
      bound evidence that is reopened no-follow and deterministically replayed;
      exact `grep-markdown` + `no-memory` controls mask matching product signal
      across all five outcomes before every gate count. The sole offline table
      renderer accepts only the validated cohort, carries replayed catastrophic
      artifact paths, and suppresses the whole table when evidence cannot be
      reproduced. Red-first evidence is preserved in
      `.task/RESULT-5.1d.md`; final gates: 184 focused checks; 700 cross-lane
      checks + 2 expected absent-artifact skips; 31 Bun checks; schema,
      fixture, OpenSpec, privacy, compile, lifecycle, and diff checks green.
      Fresh independent review: two REQUEST_CHANGES rounds plus targeted
      corrections and final `CLEAR`. No §4.6, §5.2, §5.3, §5.4, provider,
      competitor, model, dataset benchmark, credential, metered, or external
      network run occurred.
- [ ] 5.2 Families 1–9 fixtures + runs vs exomem + grep-markdown + no-memory
      (controls score non-trivially)
- [ ] 5.3 4b.18 structural-blinding fix + structure-swap test (hard-gates all
      judge use)
- [ ] 5.4 Competitor drivers (basic-memory, supermemory) + ⛳ independent
      adversarial review of every fairness packet and the glue diff BEFORE
      competitor runs
- [ ] 5.5 Operational families 10–14 (external edit, engine-off 13a/13b,
      cross-agent continuation, triage invalidation, downstream impact);
      cross-case residue clean
- [ ] 5.6 Judge–human κ on a blind sample (agreement machinery) before any
      judged number

## 6. Native + operational lanes (W7+W8 — lanes: claude + codex)

- [ ] 6.1 `meter_proxy.py` + local extraction endpoint (model pulled) —
      prerequisite for any cost row
- [ ] 6.2 8 journey fixtures; B1 controlled rows with fairness-matrix
      asymmetry entries; B2 native rows (verbatim shipped skills; capped
      neutral prompt; future-blind write agent with n-gram test; fresh
      answer agent)
- [ ] 6.3 `opsq/` measurement modules + manifests; fault-injection
      transparency (incl. exomem's own 4b.30 soft-degrade); heuristics
      labeled; no-aggregate/no-dashboard tests

## 7. Runs (W9) — every metered step ⛳-gated

- [ ] 7.1 Fixture tier across all lanes, zero spend
- [ ] 7.2 LongMemEval-S fetched + sha pinned; canonical selection artifact
      committed (direct stub pilot remains open)
- [ ] 7.3 Supermemory local binary installed (isolated data dir, version
      pinned per bm-bench's documented pin)
- [ ] 7.4 ⛳ Metered 25-case tier under the recorded session cap (≤$25):
      exomem, hybrid-rag, bm-local (both harnesses),
      supermemory-local-controlled (+hosted iff key provided)
- [ ] 7.5 Official judging via pinned LongMemEval V1 checkout; labels
      ingested
- [ ] 7.6 Gap reports for everything not run (blocked rows with one-command
      paths)

## 8. Suites (W10)

- [ ] 8.1 `suites/registry.py` (LOCKFILE-or-GAP invariant) + `lme_v1`
      LOCKFILE (verifies `judge_io` UNVERIFIED flags → tested string)
- [ ] 8.2 LongMemEval-V2: pin official harness; 3-case offline fixture
      through THEIR harness with stub model; ≤25-case small tier only if
      budget allows
- [x] 8.3 STALE + MemOps licence/code recon → **both LOCKFILE** (full public
      releases verified: STALE MIT+CC-BY-4.0 incl. runnable CUP-Mem, found
      in paper Appendix G; MemOps MIT with no-adapter-interface caveat) +
      bonus pins: MemoryAgentBench (MIT, ICLR 2026, CR split only 8 rows)
      and the OIDA CC-BY-4.0 epistemic corpora. Clones at sibling
      `suite-*` checkouts; pins in `benchmarks/suites/*/LOCKFILE.json`

## 9. Reports + CI (W11)

- [ ] 9.1 `reports/` renderer (per-ability × variant; no aggregate; INVALID
      renders INVALID; refuses cross-provider latency from this host)
- [ ] 9.2 Fairness matrix + compat matrix (declared-vs-observed divergence a
      first-class finding)
- [ ] 9.3 Adversarial packet generator (assumptions, confounds,
      suspicious-win flags, challenge paths, pre-registration hash)
- [ ] 9.4 `consolidate.py` + offline guard (import-closure test)
- [ ] 9.5 Additive `benchmark-protocol` CI job (offline only; guarded
      `retrieval-eval` untouched); README lane map updated
- [ ] 9.6 One-command smoke / per-lane run / report-regeneration documented
      and exercised from a clean checkout

## 10. Strategy + external review (W12)

- [ ] 10.1 Market-map research (Supermemory, Basic Memory, Kumiho,
      Eywa/GEM/OIDA/STALE/MemOps) with shipped/documented/prototype/unverified
      vocabulary
- [ ] 10.2 `docs/strategy/exomem-competitive-strategy-2026-08.md` — thesis
      table, narrow market, flagship journeys, parity floor, what-not-to-build,
      Hosted role, derived-media policy, gates G1–G5 evaluated against
      pre-registered thresholds
- [ ] 10.3 ⛳ External adversarial review of the packet (independent
      reviewer); every material objection fixed or documented
- [ ] 10.4 Final owner report: the seven mission questions answered plainly

## Mission acceptance criteria (tracked; close only from evidence above)

| # | Criterion | Closes with |
|---|---|---|
| 1 | Existing code audited; old results labelled | 0.1 |
| 2 | Event/manifest/trace schemas tested | 1.2–1.8 |
| 3 | Gold-leakage tests pass | 1.1, 1.3, 1.4 |
| 4 | Contamination + cleanup tests pass | 1.5 |
| 5 | Exomem/BM readiness fails closed | 1.6, 2.3, 2.4 |
| 6 | SM readiness separates documents from memories/profile | 2.3 |
| 7 | 25-case direct-vs-MemoryBench equivalence green | 4.6 |
| 8 | Direct LongMemEval small subset end-to-end, official outputs | 7.2, 7.5 |
| 9 | MemoryBench runs exomem + BM + their SM on the subset | 4.4–4.6, 7.4 |
| 10 | SM SDK vs MemoryBench equivalence checked | 4.7 |
| 11 | Hybrid-RAG control on the same subset | 2.2, 7.4 |
| 12 | LMEv2 official harness calls exomem on fixtures/small tier | 8.2 |
| 13 | ≥14 ESB families specced+mapped+asserted; exomem runs; competitor runs where possible | 5.1–5.5 |
| 14 | Reports regenerate from artifacts only | 9.1, 9.4 |
| 15 | One-command smoke/run/report from clean checkout | 9.6 |
| 16 | CI runs unit + no-paid fixture suite | 9.5 |
| 17 | No secrets in artifacts/logs | 1.10 + privacy gate |
| 18 | No unrelated user work overwritten | worktree discipline (standing) |
