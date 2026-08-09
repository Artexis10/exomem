# Tasks

## 1. Decision record and rescope (Phase A — Claude-side)
- [ ] 1.1 This change validated strict; cross-referenced from
      `add-memory-proof-benchmark` and `expand-memory-proof-benchmark`
      scope notes
- [ ] 1.2 Docs sweep: every cross-contender figure in
      `docs/memory-proof-benchmark.md` and
      `docs/memory-proof-benchmark-v01-findings.md` marked WITHDRAWN (both
      directions) with the audit rationale and a pointer to this change;
      grep audit confirms no unwithdrawn comparative figures remain in
      `docs/`
- [ ] 1.3 Ledger triage in `expand-memory-proof-benchmark/tasks.md`:
      4b.31, 4b.33, 4b.40 and the cross-contender half of 4b.29 closed as
      retired-with-rationale; 4b.44→3.1, 4b.34→3.3, 4b.28→3.4 routing
      recorded; 4b.37/4b.39 parked with design notes
- [ ] 1.4 KB updated: split-the-ambition insight extended/superseded with
      the settled decision (permanent retirement; exomem-only public
      number; LoCoMo disqualified; MemoryAgentBench watch; corrected
      reusability — corpus+oracle do not transfer)
- [ ] 1.5 Founder word obtained → PR #390 marked ready and landed

## 2. LongMemEval-S lane (Phase B — Codex implementer, fresh review)
- [ ] 2.1 Dataset fetch + pin: HF `xiaowu0162/longmemeval-cleaned` (-S),
      checksum recorded; stratified 20-question pilot manifest committed
- [ ] 2.2 Ingester under `benchmarks/lme/`: per-question isolated vault,
      one source per session via `op_capture_source`, session timestamp in
      frontmatter+body; product defaults ON; determinism pins only
- [ ] 2.3 Reader seam: `op_ask_memory` retrieval → reader LLM
      (`judge/backends.py` OpenAICompat/ClaudeCli) → hypothesis JSONL;
      stub-reader dry run green on the pilot manifest
- [ ] 2.4 Official judge wiring: `evaluate_qa.py` protocol unmodified;
      agreement-κ blind spot-check harness over judge verdicts
- [ ] 2.5 Bounds: gold-evidence ceiling adapter (`answer_session_ids`) +
      null-abstain floor under the same reader; per-ability report, no
      aggregate
- [ ] 2.6 GATE (founder): metered API spend approved before any real
      reader/judge call
- [ ] 2.7 Pilot 20 with real reader+judge; ingest wall-time and cost
      extrapolation recorded
- [ ] 2.8 GATE (founder): full-500 run approved on pilot evidence
- [ ] 2.9 Full run; artifacts (hypotheses, judge labels, environment
      capture, bounds) preserved; per-ability results doc with published
      third-party rows cited to their owners

## 3. Internal-instrument cleanup (Phase C — cheap lane)
- [ ] 3.1 4b.44: `derive_compile_plan` emits in supersession order;
      measured superseded-note count reaches the edge ceiling; red-first
      test
- [ ] 3.2 Reporting: latency and harness-mode abstention permanently
      withheld from cross-contender surfaces; VOID markers retained
- [ ] 3.3 4b.34: clock injected into Track D journeys (pinned instant;
      date-named fixtures derived from it); suite green across a UTC
      midnight boundary
- [ ] 3.4 4b.28: J1 anchor contract settled as a deliberate decision
      (benchmark contract vs `evolution_for_path` head-resolution); test
      updated to the decided contract, never recalibrated to current
      behaviour

## 3b. Review residuals (non-blocking, from the 2026-08-09 lane reviews)
- [ ] 3b.1 LME runner: a failed question's row in question-outcomes.jsonl
      inherits the PREVIOUS question's reader token/cost metrics
      (`_reader_outcome` reads `reader.last_call_metrics` unreset on
      failure), polluting pilot cost extrapolation — null the metrics when
      status == "failed" (red-first)
- [ ] 3b.2 LME test gaps: `retrieval_clock` recorded in environment.json/
      run.json has no test; the pilot too-small-to-cover-groups refusal has
      no test; the clock-seam test asserts the module attribute rather than
      observing find_policy's computed `today`
- [ ] 3b.3 LME full-run gate exempts fixture-path datasets entirely —
      document or narrow (MUST 4(d) literally says "any non-pilot run")
- [ ] 3b.4 membench bounds table: a native contender's abstention percentage
      cell still renders against floor/ceiling values that are themselves
      withheld on that row, leaking the scale relationship the row withholds
- [ ] 3b.5 `evolution.py` path-route truncation says "raise max_versions",
      a parameter `review_memory` does not expose (topic route correctly
      says EXOMEM_EVOLUTION_MAX_VERSIONS) — unactionable remediation for
      >25-version chains queried by path; align the message with a knob the
      caller can reach
- [ ] 3b.6 Graph-value runner rescope (from the 2026-08-09 re-examination,
      verdict REJECT—WITHDRAW): `scripts/graph_value_benchmark.py` survives
      only as an exomem-only regression instrument — retire or
      internal-diagnostic-label its basic-memory adapter path, remove the
      dominance-gate framing, and keep the exomem regression invariants;
      the comparison doc and all four live citations carry withdrawal
      notes as of this change

## 4. Validation
- [ ] 4.1 `openspec validate rescope-benchmark-instrument-and-public-suite --strict`
- [ ] 4.2 Lean membench suite green (the deliberate 4b.28 red documented
      until 3.4 lands)
- [ ] 4.3 `tests/test_lme_*.py` green offline (stub reader; no credentials)
