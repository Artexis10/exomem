## 1. Ruling

- [x] 1.1 Orchestrator ruled on the API shape (PR #1452): N = 500, upsert-replace, insert-only without a natural key, receipt fallback for provenance, no new idempotency argument, abort default, no dry-run.

## 2. Writer

- [ ] 2.1 Red: 24 valid rows commit in one call; a collection without a natural key runs insert-only with one audit transition; stale guard refuses whole; re-submit is all `unchanged`; changed row is `updated`; twin and duplicate-in-request rows are `rejected` (`tests/test_records_bulk_upsert.py`).
- [ ] 2.2 `records.bulk_upsert_records` beside `append_record`, sharing validation, natural-key identity, replay hash, audit and `batch_atomic_write`; one guard check; limits (`BULK_UPSERT_MAX_ROWS`, item ceiling, audit caps).
- [ ] 2.3 Red then fix: `abort` writes nothing and reports every row's would-be outcome; `skip` commits accepted rows once; publication failure rolls back.

## 3. Provenance, receipt, retry

- [ ] 3.1 Red: `source` required per row or batch; missing and withheld sources reject identically; `sources` field bound on the row; undeclared field rejects.
- [ ] 3.2 Red: one transition and one log entry, no row values, provenance refs by row index; item markers name the transition; keyless replay on a natural-keyed collection is all `unchanged`; transport-identity retry returns the recorded result.
- [ ] 3.3 Red: hidden natural-key twin is rejected without `item_keys` and leaks nothing; crash after commit replays safely.

## 4. Surface

- [ ] 4.1 `record_memory` action, `_ACTION_FIELDS` / `_REQUIRED_FIELDS`, argument rules, docstring; `commands.op_record_memory` parameters.
- [ ] 4.2 `describe` contract and generic example; scaffold leak guard passes.
- [ ] 4.3 Regenerate tool schemas, plugin tree, hosted render, v5 candidate and `docs/capabilities.md` per `CONTRIBUTING.md`; frozen v1, v2 and v3 Claude candidates are byte-identical; `tests/harness_modules.txt` if a new module imports `benchmarks/`.

## 5. Measure and deliver

- [ ] 5.1 Measure 24 rows and 500 rows against 24 and 500 serial appends on a synthetic collection; record before/after in design.md and the PR body.
- [ ] 5.2 Gates: privacy gate, `ruff --select F`, `openspec validate --all --strict`, `generate-capabilities.py --check`, scoped pytest.
- [ ] 5.3 Merge, then synchronize the delta into `records` and archive the change (orchestrator).
