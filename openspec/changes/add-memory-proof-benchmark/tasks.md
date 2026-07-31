# Tasks

## 1. Foundation (Milestone 1 — done in this change's authoring commit)
- [x] 1.1 Preserve Track-A provider work (custody `bed652bb` + portability `4fe506e8` on `exomem-provider` branch; local only)
- [x] 1.2 `docs/memory-proof-benchmark.md` (methodology, capability matrix, run matrix, falsification register)
- [x] 1.3 `docs/adr/0001-structured-benchmark-package.md`
- [x] 1.4 This OpenSpec change + supersession banner on `add-memory-server-comparison-benchmark`
- [x] 1.5 TUI requirements handoff (`docs/tui-requirements-handoff.md` + OpenSpec `add-terminal-review-surface`)

## 2. Corpus core (critical path)
- [x] 2.1 `wordbank.py`, `ids.py`, `clock.py` + privacy-scan test (TDD: scan test first)
- [x] 2.2 `schema.py` (pydantic v2, extra=forbid) + JSON-Schema export + malformed-fixture rejection tests + drift gate
- [x] 2.3 `oracle.py` + as-of/late-evidence/retroactive/expiry/confirm/disprove edge tests + span-justification lint
- [x] 2.4 `templates/base.py` (SymbolTable + declarative ops) + registry + `t00_mini_smoke` + `generate.py` + double-generation determinism + template-isolation tests
- [x] 2.5 `artifacts/` renderers (md/csv/png/transcript; pdf optional with recorded degradation) + logical-hash stability tests
- [x] 2.6 `native/` renderers (neutral, basic_memory, exomem_kb, graybox) + per-fact parity tests + ontology banned-vocab lint

## 3. Harness core (critical path)
- [x] 3.1 `adapters/base.py` (MemoryAdapter + Capability) + `exomem_local.py` (leaf/wire; verified isolation env; no-warming assertion; external logs) + T00 end-to-end adapter test incl. sentinel survivability (governed mode deferred to a follow-up; leaf/wire cover the CI + fairness paths)
- [x] 3.2 `runner.py` + `environment.py` (no-overwrite run dirs; failures.jsonl denominators; invalid-run semantics) + tests
- [x] 3.3 Scoring core: `answer_contract.py` (extractor adds structure only) + value/state/citations/no-leak/abstention/calibration gate goldens + deterministic extractive answerer + retrieval metrics
- [x] 3.4 GATE: T00 end-to-end run directory produced via leaf mode and via wire mode. NOTE: the historical zero-hits incident reproduced and was root-caused here — engine-side conjunctive retention gate in the lexical-degraded hybrid path (see docs/memory-proof-benchmark.md); scored honestly, harness asserts integrity via statement-form probes.

## 4. Fan-out (after 3.4 gate; Codex lanes eligible)
- [ ] 4.1 Scorer families: governance, abstention, graph, retrieval (reuse `exomem.eval_metrics`), health (3-tier), behavior
- [ ] 4.2 Templates T01..T16+ across the seven families (≥16 templates, ≥4 variants where sensible, ≥200 questions) + release manifest under `benchmarks/corpus/releases/`
- [ ] 4.3 `reporting.py` (per-dimension, no aggregate, latency separate, invalid-run rendering)
- [ ] 4.4 `judge/` backends (none/subagent/claude_cli/openai_compat) + blinding + file handshake + contract tests
- [ ] 4.5 `basic_memory_local.py` + `graybox_local.py` adapters + `track_a_bridge.py` + conformance tests
- [x] 4.6 `benchmarks/README.md` + `docs/benchmarks.md` summary pointer

## 5. Track A execution
- [x] 5.1 Env prep: the provider worktree's existing `benchmarks/.venv` is current; no sync needed (uv cache is read-only in-sandbox — online re-sync is a user-run command if ever required)
- [x] 5.2 Provider unit tests green offline (12/12 in the sibling venv)
- [x] 5.3 Zero-hits root-caused (engine-side: keyword mode is conjunctive substring; hybrid's lexical-degraded retention seam requires lane corroboration / literal excerpt / ALL stems). Provider fixes on `exomem-provider`: hybrid-always (0b11fab4), lease+config isolation (02096da2), skip semantic index under EXOMEM_DISABLE_EMBEDDINGS (follow-up commit). Finding + product-fix recommendation in docs/memory-proof-benchmark.md
- [x] 5.4 ADJUSTED: the planned zero-hit validity gate would misclassify the now-root-caused product behaviour as a harness fault; harness integrity is witnessed instead by the CI title-probe adapter test and the two-witness Track C design. Doc-count parity remains a fan-out candidate
- [x] 5.5 Offline smoke run complete and artifact-validated (`exomem-local` ok: recall@5 0.0; `baseline-grep` 0.75; run 3314add53aff). `bm-local` is sandbox-blocked (spawns `uv run`; read-only uv cache) — exact user-run command in the findings report
- [ ] 5.6 `supports_group_reuse` parity for exomem-local (project-per-group) — pending
- [ ] 5.7 Upstream PR branch off origin/main + title/body draft (no push, no numbers claimed) — draft in findings report; branch cut is user-run (needs network fetch)

## 6. Track C execution
- [x] 6.1 Track-C driver package + frozen 19-case control-prompt suite against isolated hook homes (observed==predeclared for all; measured gate limits recorded: length-bounded control skip, no topicality signal)
- [x] 6.2 Retrieval-injection ladder: CLI rung offline (fires + degradation floor); REST rung documented user-run stub (loopback not assumed)
- [x] 6.3 Continuation checkpoint round-trips (7/7 marker recall, 64KiB bound); cross-client restore is impossible by product contract — scored as isolation-respected + per-client recall
- [ ] 6.4 Two-witness activation join tool (server trace × transcript) — pending (needs the natural-prompt driver's transcripts)
- [ ] 6.5 `claude -p` natural-prompt driver + subagent propensity simulation — pending (network/user-run execution)

## 7. Track D execution
- [x] 7.1 Journey runner (FlowRunner-derived) over exomem CLI --json with isolated vaults
- [x] 7.2 Journeys J1 (longitudinal evolution, 11 checks) + J2 (correction propagation, 12 checks) green; wrong-order variant proves checks bite. J3/J4 pending
- [ ] 7.3 J3 weekly-review rubric JSON + blind pairwise wiring — pending
- [ ] 7.4 Basic Memory journey mapping table (doc only) — pending

## 8. Closeout
- [x] 8.1 `docs/hosted-inference-boundary.md` (candidate jobs + measurable thresholds + `reasoning: client|hosted` manifest axis)
- [x] 8.2 Founder-regression format (`membench/private_regressions.py` committed; data dir gitignored; P0 never leaves the local store) + replayer stub
- [x] 8.3 Baseline runs complete: full-corpus exomem leaf (236/240 scored, 0 harness failures), T00 wire, Track A smoke (exomem-local ok + baseline-grep; bm-local sandbox-blocked→user-run), Track C suite, Track D J1/J2, blinded judge smoke (12 real-model judgments agreeing with deterministic gates)
- [x] 8.4 Weaknesses-first report: `docs/memory-proof-benchmark-v01-findings.md` (headline: conjunctive retention gate → NL retrieval ≈0 in lexical profile, confirmed by two harnesses; + gate-limit, isolation, and robustness findings) with exact reproduction commands
- [ ] 8.5 Exomem KB write-back: benchmark results note verified by read-back — in progress at closeout

## 9. Validation
- [ ] 9.1 `npm exec --yes @fission-ai/openspec -- validate add-memory-proof-benchmark --strict`
- [ ] 9.2 `uv run --frozen python -m pytest -q` (lean suite incl. all `tests/test_membench_*.py`, each <60s, no credentials)
- [ ] 9.3 `uvx ruff check . --select F`
- [ ] 9.4 Desk-side: `uv run python benchmarks/run.py generate --seed 1 --out benchmarks/corpus/generated/s1` twice + manifest diff; T00 wire-mode run producing a complete `benchmarks/runs/<id>/`
- [ ] 9.5 `git diff --exit-code tests/fixtures/mcp_tool_schemas.json` (no tool-surface drift); guarded files untouched (`tests/golden/`, gate tests, `.github/`)
