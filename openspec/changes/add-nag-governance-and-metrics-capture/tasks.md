## 1. Contracts and red-first acceptance

- [x] 1.1 Add the `attention-queue`, `command-surface`, and `agent-bootstrap-contract` deltas; `openspec validate add-nag-governance-and-metrics-capture --strict` passes.
- [x] 1.2 Red-first: prove today's gaps before implementing — a quiet family has no effect anywhere; a dismissal records no reason or origin; no signal carries `first_surfaced_at`; the projection file carries no emission counts; one multi-write command emits one block per write; `.review-state.json` has no compaction. Each requirement gets a mechanism-removal test that goes red when its mechanism is disabled.
- [x] 1.3 Egress red-first: a withheld signal is never written to the first-surfaced ledger, and a quiet family's items count zero on every carrier for every audience.

## 2. Review-state store

- [x] 2.1 Schema v2 with `records`, `dispositions`, `surfaced`, `stats`; v1 migration on load with rewrite on next write; older-runtime refusal unchanged; read limit raised to 16 MiB.
  - Deviation: the read limit is **64 MiB**, not 16 MiB. 16 MiB is arithmetically unreachable at 50,000 records + 150,000 ledger entries — the keys alone are 10.1 MiB and the actual encoded store is 41.3 MiB. Measurements and reasoning are in `review_state._STATE_READ_LIMIT`'s docstring and in `.task/RESULT.md`.
- [x] 2.2 Reason-token parsing (closed vocabulary, `unspecified` fallback, verbatim `why`) and `origin` on every record; migrated records carry `manual`.
- [x] 2.3 Dispositions: registry of valid families (registered attention categories ∪ write-advisory kinds), set/clear with reason requirement, read API for the filters in §3.
- [x] 2.4 First-surfaced ledger: best-effort, lock-scoped, failure-isolated recording API; never written for withheld or disposition-excluded signals; never by audit.
- [x] 2.5 Retention and compaction on write past thresholds and on reconcile, reporting what was dropped; standing dismissals, stances, and dispositions never dropped.
- [x] 2.6 Stress gate at 50,000 records and 150,000 ledger entries with budgets pinned from lane measurements and the evidence recorded in the test.

## 3. Disposition effects

- [x] 3.1 Attention: drop excluded families' reasons before fusion; annotate `disposition` on explicitly requested quiet/off families; `off` hidden except under `state="all"`; audit mode untouched.
- [x] 3.2 Due-state projection and carriers: quiet/off families contribute nothing to counts, top references, or the `categories` map on write, recall, and bootstrap.
- [x] 3.3 Write-path advisories: no advisory emitted for a quiet/off kind; failure-isolated like S2's suppression.
- [x] 3.4 Ledger population on the three served surfaces, with `first_surfaced_at` on attention items.

## 4. Command surface

- [x] 4.1 `triage_memory`: family refs with `quiet`/`off`/`normal`; item actions refused on family refs and vice versa; response shape with family, disposition, reason, origin.
- [x] 4.2 `review_memory(mode="dispositions")`: non-default families with reason, why, timestamp, origin, and per-family manual dismissal counts.
- [x] 4.3 CLI `exomem review quiet|off|normal <family> --reason <code> [--why …]` and `--reason` on `dismiss`, composing the token.
- [x] 4.4 Batch scope on every multi-write leaf (adopt_vault apply/copy, adoption_studio apply, maintain_memory fix/reconcile, multi-page compile_source, preserve_artifacts, multi-page process_media); emission ledger persisted in `.due-state.json`.
  - Deviation: `compile_source` is **not** wrapped. It is read-only (`op_propose_compilation` returns a plan; the agent writes with `remember`), so there is no batch to scope.
- [x] 4.5 Tool descriptions updated; `scripts/dump-tool-schemas.py` regenerated; packaged contract, fixture, and plugin pending digest recorded with `refresh_required: true`; the hosted-profile pin tests pass.

## 5. Teaching and bench

- [x] 5.1 Bootstrap engagement guidance: reason codes, the family route for "stop suggesting this kind of thing", quiet ≠ clean; scaffold and plugin skill regenerated; compact byte ceiling holds (re-measure, do not guess).
- [x] 5.2 `docs/epistemic-inbox.md`: dispositions, reason codes, the ledger, and compaction documented.
- [x] 5.3 Bench projector: `due_state_counters` declared `available_via:due_state_file`, reading `emissions`/`writes`; decisions projection reads schema v2.
- [x] 5.4 f23 journey driver against the installed envelope (seed, passes, dismiss, restart, prominence min/max, bulk ingest, snapshot pair); refuses without an envelope; both f23 assertions green on this runtime and the batch-scope removal turns the counter assertion red.
  - Deviation: the batch-scope removal is exercised at the level the scope operates on — twelve write
    carriers, in-process — not through the bulk CLI command the journey uses. A product command delivers
    exactly one response and the emission decision lives at that response's terminal (D9), so a bulk
    command emits at most one block with or without the scope; removing it there moves the count from one
    to zero and could never turn the assertion red. See `.task/RESULT.md`.

## 6. Gates

- [x] 6.1 Focused suites: review_state, attention, corpus_aware, write-advisory suppression, due-state (consumers, projection, carriers), mutation terminal, bootstrap compact budget, reserved admin paths, epistemic no-nudge families, hosted epistemic profile, MCP schema fidelity.
  - 730 passed, 15 skipped (all Windows-only or `sentence_transformers`-absent).
- [x] 6.2 Lean suite without early stop; retrieval and semantic-write latency gates; ruff; openspec strict validation.
  - Lean suite: 1 failed, 12,686 passed, 263 skipped. The single failure is
    `tests/test_governance_overhead.py::test_governed_receipt_append_overhead_under_budget`, the
    governance-overhead timing test on the packet's known-environmental list. Not attributed by citation:
    the two leaves it measures execute no S6 code (traced), the breach is in the p95 tail of a
    real-minus-sink difference whose noise floor reached -122 ms, and the same test passes on this tree and
    on HEAD's `src/` when the box is not running another lane's full suite. Evidence in `.task/RESULT.md`
    under Gate 4.
  - Latency gates 34 passed; `ruff check --select F` clean; `openspec validate --strict` valid.
