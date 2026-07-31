# Tasks

## 1. Foundation (Milestone 1 — done in this change's authoring commit)
- [x] 1.1 Preserve Track-A provider work (custody `bed652bb` + portability `4fe506e8` on `exomem-provider` branch; local only)
- [x] 1.2 `docs/memory-proof-benchmark.md` (methodology, capability matrix, run matrix, falsification register)
- [x] 1.3 `docs/adr/0001-structured-benchmark-package.md`
- [x] 1.4 This OpenSpec change + supersession banner on `add-memory-server-comparison-benchmark`
- [x] 1.5 TUI requirements handoff (`docs/tui-requirements-handoff.md` + OpenSpec `add-terminal-review-surface`)

## 2. Corpus core (critical path)
- [ ] 2.1 `wordbank.py`, `ids.py`, `clock.py` + privacy-scan test (TDD: scan test first)
- [ ] 2.2 `schema.py` (pydantic v2, extra=forbid) + JSON-Schema export + malformed-fixture rejection tests + drift gate
- [ ] 2.3 `oracle.py` + as-of/late-evidence/retroactive/expiry/confirm/disprove edge tests + span-justification lint
- [ ] 2.4 `templates/base.py` (SymbolTable + declarative ops) + registry + `t00_mini_smoke` + `generate.py` + double-generation determinism + template-isolation tests
- [ ] 2.5 `artifacts/` renderers (md/csv/png/transcript; pdf optional with recorded degradation) + logical-hash stability tests
- [ ] 2.6 `native/` renderers (neutral, basic_memory, exomem_kb, graybox) + per-fact parity tests + ontology banned-vocab lint

## 3. Harness core (critical path)
- [ ] 3.1 `adapters/base.py` (MemoryAdapter + Capability) + `exomem_local.py` (leaf/governed/wire; verified isolation env; no-warming assertion; external logs) + T00 end-to-end adapter test incl. sentinel survivability
- [ ] 3.2 `runner.py` + `environment.py` (no-overwrite run dirs; failures.jsonl denominators; invalid-run semantics) + tests
- [ ] 3.3 Scoring core: `answer_contract.py` (extractor adds structure only) + exactness/temporal/claims/citations + gate goldens (leak, wrong date, missing citation, wrong current-state, missed abstention, forbidden disclosure)
- [ ] 3.4 GATE: T00 end-to-end run directory produced via leaf mode and via wire mode (desk-side)

## 4. Fan-out (after 3.4 gate; Codex lanes eligible)
- [ ] 4.1 Scorer families: governance, abstention, graph, retrieval (reuse `exomem.eval_metrics`), health (3-tier), behavior
- [ ] 4.2 Templates T01..T16+ across the seven families (≥16 templates, ≥4 variants where sensible, ≥200 questions) + release manifest under `benchmarks/corpus/releases/`
- [ ] 4.3 `reporting.py` (per-dimension, no aggregate, latency separate, invalid-run rendering)
- [ ] 4.4 `judge/` backends (none/subagent/claude_cli/openai_compat) + blinding + file handshake + contract tests
- [ ] 4.5 `basic_memory_local.py` + `graybox_local.py` adapters + `track_a_bridge.py` + conformance tests
- [ ] 4.6 `benchmarks/README.md` + `docs/benchmarks.md` summary row

## 5. Track A execution
- [ ] 5.1 Offline env prep in the provider worktree (`uv sync --offline`; else hand the user the online command)
- [ ] 5.2 Provider unit tests green offline
- [ ] 5.3 Zero-hits root-cause protocol (H1 lexical sidecar → H2 scope → H3 MCP payload → H4 flags); record cause + fix; no numbers before resolution
- [ ] 5.4 Sanity gate (zero-hit >50% fails) + canary query + ingested-doc-count parity in validate-artifacts path
- [ ] 5.5 Offline 3-doc smoke run (`bm-local,exomem-local,baseline-grep`, top_k=10, `--bm-local-path`) + validate-artifacts
- [ ] 5.6 `supports_group_reuse` parity for exomem-local (project-per-group)
- [ ] 5.7 Upstream PR branch off origin/main + title/body draft (4 files; no push, no numbers claimed)

## 6. Track C execution
- [ ] 6.1 Track-C driver package + frozen control-prompt suite (cp01–cp14 + hard negatives) against isolated hook homes
- [ ] 6.2 Retrieval-injection ladder tests (CLI rung offline; REST rung loopback attempt else user-run command)
- [ ] 6.3 Continuation checkpoint round-trips incl. cross-client shared hook home
- [ ] 6.4 Two-witness activation join tool (server trace × transcript/hook stdout)
- [ ] 6.5 `claude -p` natural-prompt driver + transcript parser (execution user-run) + subagent propensity simulation (labeled)

## 7. Track D execution
- [ ] 7.1 Journey runner (FlowRunner-derived, event-stream consumer)
- [ ] 7.2 Journeys J1–J4 + deterministic checks
- [ ] 7.3 J3 rubric JSON + blind pairwise wiring
- [ ] 7.4 Basic Memory journey mapping table (doc only)

## 8. Closeout
- [ ] 8.1 `docs/hosted-inference-boundary.md` (candidate jobs + measurable thresholds + `reasoning: client|hosted` manifest axis)
- [ ] 8.2 Founder-regression format under `benchmarks/private/` (gitignored + CI-excluded) + replayer stub
- [ ] 8.3 Baseline runs: full-corpus exomem (leaf + wire, lexical), Track A smoke, Track C runnable suite, Track D journeys; harness-subagent QA/judge smoke (~30–60 calls, labeled)
- [ ] 8.4 Weaknesses-first report + exact reproduction command list
- [ ] 8.5 Exomem KB write-back: benchmark decision note + Gray Box audit note, verified by read-back

## 9. Validation
- [ ] 9.1 `npm exec --yes @fission-ai/openspec -- validate add-memory-proof-benchmark --strict`
- [ ] 9.2 `uv run --frozen python -m pytest -q` (lean suite incl. all `tests/test_membench_*.py`, each <60s, no credentials)
- [ ] 9.3 `uvx ruff check . --select F`
- [ ] 9.4 Desk-side: `uv run python benchmarks/run.py generate --seed 1 --out benchmarks/corpus/generated/s1` twice + manifest diff; T00 wire-mode run producing a complete `benchmarks/runs/<id>/`
- [ ] 9.5 `git diff --exit-code tests/fixtures/mcp_tool_schemas.json` (no tool-surface drift); guarded files untouched (`tests/golden/`, gate tests, `.github/`)
